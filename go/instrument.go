package flightrecorder

// The source transform behind Go's tracer, and the machinery that runs the result.
//
// The transform is deliberately syntactic. It never type-checks, never resolves an import, and
// never asks what a name means — it only tracks which identifiers a block has DECLARED by the
// time each statement runs, and hands those to the hook. That is the whole trick, and it is why
// this works on a package it cannot build in isolation: `go build` inside the instrumented copy
// is the type checker, and it is a better one than we would write.
//
// What gets inserted, per function:
//
//	func Study(email string, level int) (out map[string]any, __fr_r1 error) {
//	    __fr0 := __frhook.Enter("Study", "app.go:12", []string{"email","level"}, email, level)
//	    defer __fr0.Leave("app.go:12", &out, &__fr_r1)
//	    __fr0.Line("app.go:13", []string{"email","level"}, email, level)
//	    corpus := load()
//	    __fr0.Line("app.go:14", []string{"email","level","corpus"}, email, level, corpus)
//	    ...
//	}
//
// Three decisions worth their reasoning:
//
// LOCATIONS ARE BAKED IN AS STRING LITERALS, read from the ORIGINAL file's position table before
// anything is inserted. Instrumentation moves every line in the file; a trace that reported the
// instrumented line numbers would point a reader at a file that does not exist on their disk.
//
// RESULTS GET NAMED, even when the source left them anonymous, so a deferred call can read them.
// The alternative — rewriting each `return` into a temporary-and-return — cannot be done
// syntactically: `return f()` where f is multi-valued has an arity the parser cannot see. The
// signature's arity is right there in the AST.
//
// OBSERVATIONS GO BEFORE STATEMENTS, NEVER AFTER. A function whose results are non-empty must end
// in a terminating statement; appending anything after the last statement of a block would break
// that rule for every function in the file. Inserting before is always legal, and it loses only
// the effect of a block's final statement — which the enclosing block's next observation, or the
// return event, picks up anyway.

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"

	"github.com/xag/flight-recorder/go/tracehook"
)

const (
	hookPkg   = "github.com/xag/flight-recorder/go/tracehook"
	hookAlias = "__frhook"
	framePfx  = "__fr"
)

// Instrument rewrites one file's source so that running it emits a trace. filename is used for
// the locations the trace reports, so pass the path a reader would recognise.
//
// It returns the source unchanged (and instrumented=false) when the file holds no function with a
// body — a file of type and const declarations has nothing to observe, and importing the hook
// into it would be an unused import, which is a compile error.
func Instrument(filename string, src []byte) (out []byte, instrumented bool, err error) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, filename, src, parser.ParseComments)
	if err != nil {
		return nil, false, err
	}

	base := filepath.Base(filename)
	at := func(p token.Pos) string {
		return fmt.Sprintf("%s:%d", base, fset.Position(p).Line)
	}

	// Collect every function body first. Instrumenting as we walk would have us descend into the
	// calls we just inserted, and a tracer that traces its own tracing has no fixed point.
	type fn struct {
		name string
		ft   *ast.FuncType
		body *ast.BlockStmt
	}
	var fns []fn
	ast.Inspect(file, func(n ast.Node) bool {
		switch d := n.(type) {
		case *ast.FuncDecl:
			if d.Body != nil {
				fns = append(fns, fn{funcName(d), d.Type, d.Body})
			}
		case *ast.FuncLit:
			// A closure is where a Go program keeps the interesting half of its state — the
			// callback handed to a boundary, the body of a span. Skipping literals would leave
			// exactly the variables a reader came for unobserved.
			fns = append(fns, fn{fmt.Sprintf("func@%s", at(d.Pos())), d.Type, d.Body})
		}
		return true
	})
	if len(fns) == 0 {
		return src, false, nil
	}

	for i, f := range fns {
		frame := fmt.Sprintf("%s%d", framePfx, i)
		params := paramNames(f.ft)
		results := nameResults(f.ft, i)
		head := at(f.body.Lbrace)

		instrumentList(&f.body.List, params, frame, at)

		enter := []ast.Stmt{
			&ast.AssignStmt{
				Lhs: []ast.Expr{ast.NewIdent(frame)},
				Tok: token.DEFINE,
				Rhs: []ast.Expr{hookCall(hookAlias, "Enter",
					append([]ast.Expr{lit(f.name), lit(head), strSlice(params)}, idents(params)...)...)},
			},
			&ast.DeferStmt{Call: hookCall(frame, "Leave",
				append([]ast.Expr{lit(head)}, addrs(results)...)...)},
		}
		f.body.List = append(enter, f.body.List...)
	}

	var buf bytes.Buffer
	if err := printer.Fprint(&buf, fset, file); err != nil {
		return nil, false, err
	}
	return spliceImport(buf.Bytes()), true, nil
}

// spliceImport adds the hook import as text rather than as an AST node. go/printer places a
// synthesised import declaration by its (absent) position, which lands it in whatever gap it
// likes; a line of text after the package clause lands where we said. Go permits any number of
// import declarations, so a second one alongside the file's own is legal.
func spliceImport(src []byte) []byte {
	lines := strings.Split(string(src), "\n")
	for i, ln := range lines {
		if strings.HasPrefix(ln, "package ") {
			decl := fmt.Sprintf("\nimport %s %q\n", hookAlias, hookPkg)
			return []byte(strings.Join(lines[:i+1], "\n") + decl + strings.Join(lines[i+1:], "\n"))
		}
	}
	return src
}

func funcName(d *ast.FuncDecl) string {
	if d.Recv == nil || len(d.Recv.List) == 0 {
		return d.Name.Name
	}
	return exprName(d.Recv.List[0].Type) + "." + d.Name.Name
}

func exprName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return exprName(t.X)
	case *ast.IndexExpr: // a generic receiver: Box[T]
		return exprName(t.X)
	case *ast.IndexListExpr:
		return exprName(t.X)
	}
	return "?"
}

func paramNames(ft *ast.FuncType) []string {
	var out []string
	if ft.Params == nil {
		return out
	}
	for _, f := range ft.Params.List {
		for _, n := range f.Names {
			if usable(n.Name) {
				out = append(out, n.Name)
			}
		}
	}
	return out
}

// nameResults gives every result a name, inventing one where the source did not, and returns the
// names in order. Anonymous and blank results become __fr_<i>_<n>: a name no source can collide
// with, because no source may contain two underscores followed by "fr" by our own convention.
func nameResults(ft *ast.FuncType, i int) []string {
	var out []string
	if ft.Results == nil {
		return out
	}
	n := 0
	gen := func() *ast.Ident {
		n++
		return ast.NewIdent(fmt.Sprintf("%s_%d_r%d", framePfx, i, n))
	}
	for _, f := range ft.Results.List {
		if len(f.Names) == 0 {
			id := gen()
			f.Names = []*ast.Ident{id}
			out = append(out, id.Name)
			continue
		}
		for j, name := range f.Names {
			if name.Name == "_" {
				id := gen()
				f.Names[j] = id
				out = append(out, id.Name)
				continue
			}
			n++
			out = append(out, name.Name)
		}
	}
	return out
}

// --- walking statements ------------------------------------------------------------------

type atFn func(token.Pos) string

// instrumentList rewrites a statement list in place, inserting an observation before each
// statement carrying the locals declared so far, and recursing into nested blocks with the scope
// those statements have built up.
func instrumentList(list *[]ast.Stmt, scope []string, frame string, at atFn) {
	cur := append([]string{}, scope...)
	out := make([]ast.Stmt, 0, len(*list)*2)
	for _, st := range *list {
		out = append(out, lineCall(frame, at(st.Pos()), cur))
		instrumentStmt(st, cur, frame, at)
		cur = extend(cur, declared(st))
		out = append(out, st)
	}
	*list = out
}

// instrumentStmt descends into whatever blocks a statement owns, with the scope those blocks
// actually see — an if's init, a for's loop variable, a type switch's bound name.
func instrumentStmt(st ast.Stmt, scope []string, frame string, at atFn) {
	switch s := st.(type) {
	case *ast.BlockStmt:
		instrumentList(&s.List, scope, frame, at)
	case *ast.IfStmt:
		inner := extend(scope, declared(s.Init))
		instrumentList(&s.Body.List, inner, frame, at)
		if s.Else != nil {
			instrumentStmt(s.Else, inner, frame, at)
		}
	case *ast.ForStmt:
		instrumentList(&s.Body.List, extend(scope, declared(s.Init)), frame, at)
	case *ast.RangeStmt:
		inner := scope
		if s.Tok == token.DEFINE {
			inner = extend(scope, identNames(s.Key, s.Value))
		}
		instrumentList(&s.Body.List, inner, frame, at)
	case *ast.SwitchStmt:
		inner := extend(scope, declared(s.Init))
		for _, c := range s.Body.List {
			if cc, ok := c.(*ast.CaseClause); ok {
				instrumentList(&cc.Body, inner, frame, at)
			}
		}
	case *ast.TypeSwitchStmt:
		inner := extend(extend(scope, declared(s.Init)), declared(s.Assign))
		for _, c := range s.Body.List {
			if cc, ok := c.(*ast.CaseClause); ok {
				instrumentList(&cc.Body, inner, frame, at)
			}
		}
	case *ast.SelectStmt:
		for _, c := range s.Body.List {
			if cc, ok := c.(*ast.CommClause); ok {
				instrumentList(&cc.Body, extend(scope, declared(cc.Comm)), frame, at)
			}
		}
	case *ast.LabeledStmt:
		instrumentStmt(s.Stmt, scope, frame, at)
	}
	// Everything else owns no block. A FuncLit nested in an expression is instrumented as a
	// function in its own right, with its own frame, by the collection pass.
}

// declared is the names a statement brings into the scope that FOLLOWS it.
func declared(st ast.Stmt) []string {
	switch s := st.(type) {
	case nil:
		return nil
	case *ast.AssignStmt:
		if s.Tok != token.DEFINE {
			return nil
		}
		return identNames(s.Lhs...)
	case *ast.DeclStmt:
		gd, ok := s.Decl.(*ast.GenDecl)
		if !ok || (gd.Tok != token.VAR && gd.Tok != token.CONST) {
			return nil
		}
		var out []string
		for _, spec := range gd.Specs {
			if vs, ok := spec.(*ast.ValueSpec); ok {
				for _, n := range vs.Names {
					if usable(n.Name) {
						out = append(out, n.Name)
					}
				}
			}
		}
		return out
	case *ast.LabeledStmt:
		return declared(s.Stmt)
	}
	return nil
}

func identNames(exprs ...ast.Expr) []string {
	var out []string
	for _, e := range exprs {
		if id, ok := e.(*ast.Ident); ok && usable(id.Name) {
			out = append(out, id.Name)
		}
	}
	return out
}

// usable rejects the blank identifier (it holds nothing) and anything wearing our own prefix (a
// frame handle is not a local of the program under observation).
func usable(name string) bool {
	return name != "_" && name != "" && !strings.HasPrefix(name, framePfx)
}

// extend appends names not already present. A redeclaration — the `err` in `v, err := f()` when
// err already exists — must not make the variable appear twice in one observation.
func extend(scope []string, names []string) []string {
	out := append([]string{}, scope...)
	for _, n := range names {
		found := false
		for _, e := range out {
			if e == n {
				found = true
				break
			}
		}
		if !found {
			out = append(out, n)
		}
	}
	return out
}

// --- building the inserted calls -----------------------------------------------------------

func lineCall(frame, at string, names []string) ast.Stmt {
	return &ast.ExprStmt{X: hookCall(frame, "Line",
		append([]ast.Expr{lit(at), strSlice(names)}, idents(names)...)...)}
}

func hookCall(recv, method string, args ...ast.Expr) *ast.CallExpr {
	return &ast.CallExpr{
		Fun:  &ast.SelectorExpr{X: ast.NewIdent(recv), Sel: ast.NewIdent(method)},
		Args: args,
	}
}

func lit(s string) ast.Expr { return &ast.BasicLit{Kind: token.STRING, Value: strconv.Quote(s)} }

func strSlice(names []string) ast.Expr {
	elts := make([]ast.Expr, len(names))
	for i, n := range names {
		elts[i] = lit(n)
	}
	return &ast.CompositeLit{
		Type: &ast.ArrayType{Elt: ast.NewIdent("string")},
		Elts: elts,
	}
}

func idents(names []string) []ast.Expr {
	out := make([]ast.Expr, len(names))
	for i, n := range names {
		out[i] = ast.NewIdent(n)
	}
	return out
}

func addrs(names []string) []ast.Expr {
	out := make([]ast.Expr, len(names))
	for i, n := range names {
		out[i] = &ast.UnaryExpr{Op: token.AND, X: ast.NewIdent(n)}
	}
	return out
}

// --- running the instrumented copy -----------------------------------------------------------

// TraceSpec says what to instrument and how to run it.
type TraceSpec struct {
	// Dir is the module root to copy. Empty means the module containing the working directory.
	Dir string
	// Include names the files to instrument, matched as path fragments against each file's path
	// relative to the module root. Trace what you are investigating, not the world.
	Include []string
	// Command is the `go` subcommand to run inside the copy, e.g.
	// {"test", "-run", "^TestThing$", "-count=1", "."}. It runs with the tracer armed.
	Command []string
	// Package is the directory, relative to the module root, the command runs in. Empty means the
	// module root.
	Package string
	// Env adds "K=V" entries to the child's environment.
	Env []string
}

// TracedRun is what came back: the trace, and the child's own account of itself.
type TracedRun struct {
	Trace    *Trace
	Output   string // the child's combined stdout and stderr
	ExitErr  error  // non-nil if the child failed — which a test asserting on a panic will want
	Files    []string
	TraceDir string
}

// RunTraced instruments a copy of the module and runs a command inside it with the tracer armed.
//
// The original tree is never written to. Everything happens in a temp copy that is removed on the
// way out, so a traced run leaves no trace of itself except the one it was asked for.
func RunTraced(spec TraceSpec) (*TracedRun, error) {
	root, err := moduleRoot(spec.Dir)
	if err != nil {
		return nil, err
	}
	if len(spec.Include) == 0 {
		return nil, fmt.Errorf("RunTraced: no files to instrument — name at least one in Include")
	}
	if len(spec.Command) == 0 {
		return nil, fmt.Errorf("RunTraced: no command to run")
	}

	work, err := os.MkdirTemp("", "flight-traced-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(work)

	copyRoot := filepath.Join(work, "src")
	if err := copyTree(root, copyRoot); err != nil {
		return nil, err
	}

	touched, err := instrumentTree(copyRoot, spec.Include)
	if err != nil {
		return nil, err
	}
	if len(touched) == 0 {
		// An empty trace reported as a successful one is the worst outcome available here: the
		// reader concludes the code never ran. Say which patterns matched nothing instead.
		return nil, fmt.Errorf("RunTraced: nothing matched %v under %s — no file was instrumented",
			spec.Include, root)
	}

	tracePath := filepath.Join(work, "trace.jsonl")
	goBin, err := goCommand()
	if err != nil {
		return nil, err
	}

	cmd := exec.Command(goBin, spec.Command...)
	cmd.Dir = filepath.Join(copyRoot, filepath.FromSlash(spec.Package))
	cmd.Env = append(os.Environ(), tracehook.EnvPath+"="+tracePath)
	cmd.Env = append(cmd.Env, spec.Env...)
	out, runErr := cmd.CombinedOutput()

	run := &TracedRun{Output: string(out), ExitErr: runErr, Files: touched, TraceDir: copyRoot}
	if data, err := os.ReadFile(tracePath); err == nil {
		if tr, err := ParseTrace(string(data)); err == nil {
			run.Trace = tr
		} else {
			return run, err
		}
	} else {
		run.Trace = &Trace{}
	}
	return run, nil
}

// goCommand finds the toolchain. PATH first; GOROOT second, because a `go test` run can perfectly
// well have been started by a launcher that never put go on the PATH, and failing there with
// "executable file not found" would be a mystifying way to report it.
func goCommand() (string, error) {
	if p, err := exec.LookPath("go"); err == nil {
		return p, nil
	}
	if gr := runtime.GOROOT(); gr != "" {
		p := filepath.Join(gr, "bin", "go")
		if runtime.GOOS == "windows" {
			p += ".exe"
		}
		if _, err := os.Stat(p); err == nil {
			return p, nil
		}
	}
	return "", fmt.Errorf("cannot find the go toolchain: not on PATH and not under GOROOT")
}

func moduleRoot(dir string) (string, error) {
	if dir == "" {
		wd, err := os.Getwd()
		if err != nil {
			return "", err
		}
		dir = wd
	}
	dir, err := filepath.Abs(dir)
	if err != nil {
		return "", err
	}
	for d := dir; ; {
		if _, err := os.Stat(filepath.Join(d, "go.mod")); err == nil {
			return d, nil
		}
		parent := filepath.Dir(d)
		if parent == d {
			return "", fmt.Errorf("no go.mod at or above %s — RunTraced copies a module", dir)
		}
		d = parent
	}
}

func copyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if d.IsDir() {
			// .git is large, irrelevant to a build, and copying it is the difference between a
			// traced run that takes a second and one that takes a minute.
			if d.Name() == ".git" && rel != "." {
				return fs.SkipDir
			}
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		if !d.Type().IsRegular() {
			return nil
		}
		return copyFile(path, filepath.Join(dst, rel))
	})
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

// instrumentTree rewrites, in the copy, every .go file whose path contains one of the patterns.
func instrumentTree(root string, include []string) ([]string, error) {
	var touched []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
			return err
		}
		rel, _ := filepath.Rel(root, path)
		slash := filepath.ToSlash(rel)
		matched := false
		for _, p := range include {
			if strings.Contains(slash, filepath.ToSlash(p)) {
				matched = true
				break
			}
		}
		if !matched {
			return nil
		}
		src, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		out, did, err := Instrument(slash, src)
		if err != nil {
			return fmt.Errorf("instrumenting %s: %w", slash, err)
		}
		if !did {
			return nil
		}
		if err := os.WriteFile(path, out, 0o644); err != nil {
			return err
		}
		touched = append(touched, slash)
		return nil
	})
	return touched, err
}

