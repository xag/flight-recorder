package io.github.xag.flightrecorder;

import com.sun.source.tree.BlockTree;
import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.LambdaExpressionTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.ReturnTree;
import com.sun.source.tree.StatementTree;
import com.sun.source.tree.Tree;
import com.sun.source.tree.VariableTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;

import javax.tools.FileObject;
import javax.tools.ForwardingJavaFileManager;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileManager;
import javax.tools.JavaFileObject;
import javax.tools.SimpleJavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Variable-level tracing for Java: every local, on every executed line, of the code you name.
 *
 * <h2>Why the code is rewritten, and what was rejected</h2>
 *
 * <p>Python gets this from {@code sys.settrace}. Node gets it from the V8 Inspector. Java has
 * neither — the JVM exposes no per-line callback a library can install — which leaves three
 * families, and we took the third:
 *
 * <ul>
 *   <li><b>JDWP / JDI, the debugger protocol.</b> The structural analogue of what Node does, and
 *       rejected for the same reasons Go rejected Delve: it needs the traced code launched under a
 *       debug agent in a separate process, adds a socket round trip per variable per line, and
 *       hands values back as the <em>debugger's</em> rendering — strings, truncated by its rules —
 *       when the entire point of trace version 2 is that values are data an invariant can do
 *       arithmetic on.
 *   <li><b>A {@code java.lang.instrument} agent rewriting bytecode.</b> Genuinely powerful, and it
 *       would sidestep definite-assignment analysis entirely (the local variable table gives you
 *       each slot's live range). But it requires {@code -javaagent} on the command line, which
 *       means the traced run cannot be started from inside a test that has already begun — and it
 *       reads locals by slot, so a value's <em>name</em> depends on the code having been compiled
 *       with {@code -g}, which is not something a library can assume about someone else's build.
 *   <li><b>Source instrumentation via {@code com.sun.source}, compiled to memory.</b> The JDK ships
 *       javac's own parser and position table as a supported API. Stdlib only, no external binary,
 *       no launch flag, cross-platform — and values are encoded <b>in process</b> from the live
 *       typed value, so a traced {@code int} is an {@code int}. This is .NET's road (Roslyn →
 *       memory → in-process) rather than Go's (rewrite → separate process), and Java can take it
 *       because, like .NET, it can compile and load an assembly without leaving the process.
 * </ul>
 *
 * <p><b>Why a recompiled copy is still the same execution.</b> The copy's classes are different
 * classes from the originals, which would be fatal for most instrumentation and is harmless here:
 * the code under replay reaches the world <em>only</em> through the boundary, and the boundary
 * lives in this jar, which the rewritten classes reference and therefore <b>share</b>. Same
 * statics, same hook, same answers off the tape.
 *
 * <h2>The definite-assignment trap</h2>
 *
 * <p>{@code int x; TraceHook.line(..., new Object[]{ x });} does not compile — "variable x might
 * not have been initialized" — and a traced copy that does not compile is not a degraded trace, it
 * is no trace at all. .NET dodges this by asking Roslyn's own {@code AnalyzeDataFlow}; javac
 * exposes no such API, so this rewriter takes Go's road instead and tracks scope
 * <b>syntactically</b>: a variable becomes observable only from the statement after a declaration
 * that <em>had an initializer</em>, and parameters are observable throughout. Conservative in the
 * safe direction — it may miss a variable, and can never emit one javac would reject.
 *
 * <p>Tracing is for REPLAY. It writes an event per changed local per executed line; performance is
 * explicitly not a goal, and nothing here belongs in a request path.
 */
public final class Tracer {

    private Tracer() {}

    private static final String HOOK = "io.github.xag.flightrecorder.TraceHook";
    private static final String PREFIX = "__fr";

    /** What a traced run produced. */
    public record Run(Object result, Trace trace) {}

    /**
     * Rewrites the named sources, compiles them to memory, and invokes
     * {@code className.methodName(args)} with the tracer armed.
     *
     * @param sourcePaths the files to instrument
     * @param className   the fully-qualified class holding the entry point
     * @param methodName  a static method on it
     */
    public static Run run(List<String> sourcePaths, String className, String methodName, Object... args)
            throws IOException, ReflectiveOperationException {
        return run(sourcePaths, className, methodName, null, args);
    }

    /** @see #run(List, String, String, Object...) */
    public static Run run(List<String> sourcePaths, String className, String methodName,
                          Boundary boundary, Object... args)
            throws IOException, ReflectiveOperationException {

        Map<String, String> rewritten = instrumentAll(sourcePaths);
        ClassLoader loader = compile(rewritten);

        TraceHook.Sink sink = new TraceHook.Sink(null, boundary);
        TraceHook.Sink prior = TraceHook.sink();
        TraceHook.setSink(sink);
        try {
            Class<?> cls = Class.forName(className, true, loader);
            java.lang.reflect.Method m = find(cls, methodName, args.length);
            m.setAccessible(true);
            Object result = m.invoke(null, args);
            if (sink.refused() != null) {
                throw new Errors.ForbiddenValue(sink.refused(), "a traced value");
            }
            return new Run(result, sink.snapshot());
        } catch (java.lang.reflect.InvocationTargetException e) {
            if (sink.refused() != null) {
                throw new Errors.ForbiddenValue(sink.refused(), "a traced value");
            }
            Throwable cause = e.getCause();
            throw Recorder.sneak(cause == null ? e : cause);
        } finally {
            TraceHook.setSink(prior);
            sink.close();
        }
    }

    private static java.lang.reflect.Method find(Class<?> cls, String name, int arity) {
        for (java.lang.reflect.Method m : cls.getDeclaredMethods()) {
            if (m.getName().equals(name) && m.getParameterCount() == arity) return m;
        }
        throw new IllegalArgumentException(
                "no method " + cls.getName() + "." + name + " taking " + arity + " argument(s)");
    }

    // ------------------------------------------------------------- rewriting

    /** Rewrites every named source. Returns fully-qualified class name → instrumented source. */
    static Map<String, String> instrumentAll(List<String> sourcePaths) throws IOException {
        Map<String, String> out = new java.util.LinkedHashMap<>();
        for (String p : sourcePaths) {
            String src = Files.readString(Paths.get(p), StandardCharsets.UTF_8);
            String file = Paths.get(p).getFileName().toString();
            String instrumented = instrument(file, src);
            out.put(topLevelName(instrumented, file), instrumented);
        }
        return out;
    }

    private static String topLevelName(String src, String file) {
        String base = file.endsWith(".java") ? file.substring(0, file.length() - 5) : file;
        java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("(?m)^\\s*package\\s+([\\w.]+)\\s*;").matcher(src);
        return m.find() ? m.group(1) + "." + base : base;
    }

    /**
     * Instruments one source file.
     *
     * <p>The rewrite is text splicing guided by the parsed tree's positions, not a re-print of the
     * tree. Re-printing would reformat the whole file and could, in principle, change meaning; a
     * splice touches only what it inserts, and everything the reader did not write stays byte for
     * byte what it was.
     */
    public static String instrument(String fileName, String src) {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException(
                    "no system Java compiler — tracing needs a JDK, not a JRE");
        }
        JavaFileObject unit = new StringSource(fileName, src);
        JavacTask task = (JavacTask) compiler.getTask(null, null, d -> { }, null, null, List.of(unit));
        CompilationUnitTree cu;
        try {
            java.util.Iterator<? extends CompilationUnitTree> it = task.parse().iterator();
            if (!it.hasNext()) return src;
            cu = it.next();
        } catch (IOException e) {
            return src; // an unparseable file is the compiler's finding to report, not ours
        }
        SourcePositions pos = Trees.instance(task).getSourcePositions();
        List<Edit> edits = new ArrayList<>();
        new Instrumenter(cu, pos, fileName, edits).scan(cu, null);
        return apply(src, edits);
    }

    private record Edit(int offset, String text) {}

    private static String apply(String src, List<Edit> edits) {
        // Applied back to front so every offset still refers to the original text — which is also
        // why the location literals below are read from the ORIGINAL position table: instrumenting
        // moves every line, and reporting an instrumented line number would point the reader at a
        // file that exists nowhere on their disk.
        List<Edit> sorted = new ArrayList<>(edits);
        sorted.sort(Comparator.comparingInt(Edit::offset).reversed());
        StringBuilder b = new StringBuilder(src);
        for (Edit e : sorted) {
            if (e.offset() >= 0 && e.offset() <= b.length()) b.insert(e.offset(), e.text());
        }
        return b.toString();
    }

    /** Collects the splices for one compilation unit. */
    private static final class Instrumenter extends TreeScanner<Void, Void> {

        private final CompilationUnitTree cu;
        private final SourcePositions pos;
        private final String file;
        private final List<Edit> edits;

        /** The enclosing class names, so {@code fn} reads {@code Toy.greet}. */
        private final java.util.ArrayDeque<String> classes = new java.util.ArrayDeque<>();
        /** The enclosing frames' variable names, innermost last. */
        private final java.util.ArrayDeque<Set<String>> scopes = new java.util.ArrayDeque<>();
        private String frameVar;
        private String fnName;
        private int frameSeq;

        Instrumenter(CompilationUnitTree cu, SourcePositions pos, String file, List<Edit> edits) {
            this.cu = cu;
            this.pos = pos;
            this.file = file;
            this.edits = edits;
        }

        /** {@code Toy.greet} — outermost class first, so a nested class reads the way it is
         *  written. Matched by trailing segment on the query side, so a caller can ask for
         *  {@code greet} without knowing the qualification. */
        private String qualify(String method) {
            if (classes.isEmpty()) return method;
            List<String> outerFirst = new ArrayList<>();
            for (java.util.Iterator<String> it = classes.descendingIterator(); it.hasNext(); ) {
                outerFirst.add(it.next());
            }
            return String.join(".", outerFirst) + "." + method;
        }

        private int start(Tree t) { return (int) pos.getStartPosition(cu, t); }

        private int end(Tree t) { return (int) pos.getEndPosition(cu, t); }

        private String at(Tree t) {
            long line = cu.getLineMap().getLineNumber(pos.getStartPosition(cu, t));
            return file + ":" + line;
        }

        @Override
        public Void visitClass(ClassTree node, Void unused) {
            classes.push(node.getSimpleName().toString());
            try {
                return super.visitClass(node, unused);
            } finally {
                classes.pop();
            }
        }

        @Override
        public Void visitMethod(MethodTree node, Void unused) {
            BlockTree body = node.getBody();
            // No body (abstract, native, interface) — nothing to instrument.
            if (body == null) return super.visitMethod(node, unused);
            // Constructors are skipped: an explicit super()/this() must be the first statement, so
            // the body cannot be wrapped in a try, and splicing around that rule is more risk than
            // a constructor's locals are worth.
            if (node.getName().contentEquals("<init>")) return super.visitMethod(node, unused);

            String priorFrame = frameVar;
            String priorFn = fnName;
            frameVar = PREFIX + (frameSeq++);
            fnName = qualify(node.getName().toString());

            Set<String> scope = new LinkedHashSet<>();
            List<String> params = new ArrayList<>();
            for (VariableTree p : node.getParameters()) {
                String n = p.getName().toString();
                if (usable(n)) { scope.add(n); params.add(n); }
            }
            scopes.push(scope);

            boolean isVoid = node.getReturnType() != null
                    && node.getReturnType().toString().equals("void");

            int open = start(body);   // the '{'
            int close = end(body) - 1; // the '}'
            if (open < 0 || close < 0) {
                scopes.pop();
                frameVar = priorFrame;
                fnName = priorFn;
                return super.visitMethod(node, unused);
            }

            String entry = "\nlong " + frameVar + " = " + HOOK + ".enter(\"" + fnName + "\", \""
                    + at(node) + "\", " + names(params) + ", " + values(params) + ");\ntry {\n";
            edits.add(new Edit(open + 1, entry));

            StringBuilder tail = new StringBuilder("\n");
            if (isVoid) {
                // Covers falling off the end of a void method; an explicit `return` emits its own R
                // and never reaches here.
                tail.append(HOOK).append(".returning(").append(frameVar).append(", \"").append(fnName)
                        .append("\", \"").append(at(node)).append("\", null);\n");
            }
            tail.append("} catch (Throwable ").append(frameVar).append("_t) { ")
                    .append(HOOK).append(".raise(").append(frameVar).append(", \"").append(fnName)
                    .append("\", \"").append(at(node)).append("\", ").append(frameVar)
                    .append("_t); throw ").append(frameVar).append("_t; }")
                    .append(" finally { ").append(HOOK).append(".exit(").append(frameVar).append("); }\n");
            edits.add(new Edit(close, tail.toString()));

            try {
                scanBlock(body);
                return null;
            } finally {
                scopes.pop();
                frameVar = priorFrame;
                fnName = priorFn;
            }
        }

        /**
         * A lambda body is its own frame — closures are where the interesting state lives, and a
         * lambda's locals belong to it, not to the method that spelled it.
         */
        @Override
        public Void visitLambdaExpression(LambdaExpressionTree node, Void unused) {
            if (node.getBody() instanceof BlockTree block && frameVar != null) {
                Set<String> scope = new LinkedHashSet<>(scopes.isEmpty() ? Set.of() : scopes.peek());
                for (VariableTree p : node.getParameters()) {
                    String n = p.getName().toString();
                    if (usable(n)) scope.add(n);
                }
                scopes.push(scope);
                try {
                    scanBlock(block);
                    return null;
                } finally {
                    scopes.pop();
                }
            }
            return super.visitLambdaExpression(node, unused);
        }

        /**
         * Walks a braced block, inserting one observation before each statement.
         *
         * <p><b>Only direct children of a block are instrumented.</b> A statement that is the body
         * of an unbraced {@code if}/{@code for} has nowhere to put a sibling — splicing there would
         * silently change which statement the branch controls — so it is left alone and its effect
         * is picked up by the next observation in an enclosing block. This is the same trade Go
         * makes when it puts observations before statements and never after.
         */
        private void scanBlock(BlockTree block) {
            Set<String> scope = scopes.peek();
            Set<String> declaredHere = new LinkedHashSet<>();
            for (StatementTree st : block.getStatements()) {
                if (frameVar != null && !scope.isEmpty()) {
                    List<String> visible = new ArrayList<>(scope);
                    edits.add(new Edit(start(st), line(visible, at(st))));
                }
                instrumentReturn(st);
                // Descend for nested blocks, lambdas and inner classes.
                scan(st, null);
                // A declaration becomes observable only from the NEXT statement, and only if it had
                // an initializer — javac rejects a read of a definitely-unassigned local, and a
                // traced copy that will not compile is worse than a slightly thinner trace.
                if (st instanceof VariableTree v && v.getInitializer() != null) {
                    String n = v.getName().toString();
                    if (usable(n)) { scope.add(n); declaredHere.add(n); }
                }
            }
            // Leaving the block: its declarations go out of scope with it.
            scope.removeAll(declaredHere);
        }

        @Override
        public Void visitBlock(BlockTree node, Void unused) {
            if (frameVar == null) return super.visitBlock(node, unused);
            scanBlock(node);
            return null;
        }

        /**
         * A loop's own variable is in scope for its body, and it is usually the one you most want
         * to see. It is declared in the loop header rather than in the body block, so the ordinary
         * block walk never sees it — Go's instrumenter special-cases {@code for} inits and
         * {@code range} bindings for exactly this reason.
         */
        @Override
        public Void visitForLoop(com.sun.source.tree.ForLoopTree node, Void unused) {
            return withLoopVars(declared(node.getInitializer()), () -> super.visitForLoop(node, null));
        }

        @Override
        public Void visitEnhancedForLoop(com.sun.source.tree.EnhancedForLoopTree node, Void unused) {
            return withLoopVars(declared(List.of(node.getVariable())),
                    () -> super.visitEnhancedForLoop(node, null));
        }

        private List<String> declared(List<? extends StatementTree> stmts) {
            List<String> out = new ArrayList<>();
            if (stmts != null) for (StatementTree s : stmts) {
                // An enhanced-for's variable has no initializer in the source, but the loop assigns
                // it before the body runs, so it IS definitely assigned there.
                if (s instanceof VariableTree v && usable(v.getName().toString())) {
                    out.add(v.getName().toString());
                }
            }
            return out;
        }

        private Void withLoopVars(List<String> vars, java.util.function.Supplier<Void> body) {
            Set<String> scope = scopes.peek();
            if (scope == null || vars.isEmpty()) return body.get();
            List<String> added = new ArrayList<>();
            for (String v : vars) if (scope.add(v)) added.add(v);
            try {
                return body.get();
            } finally {
                added.forEach(scope::remove);
            }
        }

        /** {@code return expr} becomes {@code return returned(frame, fn, at, expr)} — an identity
         *  passthrough, so the rewrite cannot change a type, an overload, or a conversion. */
        private void instrumentReturn(StatementTree st) {
            if (!(st instanceof ReturnTree r) || frameVar == null) return;
            if (r.getExpression() == null) {
                edits.add(new Edit(start(r), HOOK + ".returning(" + frameVar + ", \"" + fnName
                        + "\", \"" + at(r) + "\", null);\n"));
                return;
            }
            int exprStart = start(r.getExpression());
            int exprEnd = end(r.getExpression());
            if (exprStart < 0 || exprEnd < 0) return;
            edits.add(new Edit(exprStart, HOOK + ".returned(" + frameVar + ", \"" + fnName
                    + "\", \"" + at(r) + "\", "));
            edits.add(new Edit(exprEnd, ")"));
        }

        private String line(List<String> vars, String at) {
            return HOOK + ".line(" + frameVar + ", \"" + fnName + "\", \"" + at + "\", "
                    + names(vars) + ", " + values(vars) + ");\n";
        }

        private static String names(List<String> vars) {
            if (vars.isEmpty()) return "new String[0]";
            StringBuilder b = new StringBuilder("new String[]{");
            for (int i = 0; i < vars.size(); i++) {
                if (i > 0) b.append(',');
                b.append('"').append(vars.get(i)).append('"');
            }
            return b.append('}').toString();
        }

        private static String values(List<String> vars) {
            if (vars.isEmpty()) return "new Object[0]";
            StringBuilder b = new StringBuilder("new Object[]{");
            for (int i = 0; i < vars.size(); i++) {
                if (i > 0) b.append(',');
                b.append(vars.get(i));
            }
            return b.append('}').toString();
        }

        /** {@code _} is not a name, and the rewriter's own temporaries are not the app's state. */
        private static boolean usable(String n) {
            return !n.equals("_") && !n.startsWith(PREFIX);
        }
    }

    // ------------------------------------------------------- in-memory compile

    private static final class StringSource extends SimpleJavaFileObject {
        private final String code;

        StringSource(String name, String code) {
            super(URI.create("string:///" + name), Kind.SOURCE);
            this.code = code;
        }

        @Override public CharSequence getCharContent(boolean ignoreEncodingErrors) { return code; }
    }

    private static final class ClassBytes extends SimpleJavaFileObject {
        final ByteArrayOutputStream bytes = new ByteArrayOutputStream();

        ClassBytes(String name) {
            super(URI.create("bytes:///" + name.replace('.', '/') + ".class"), Kind.CLASS);
        }

        @Override public OutputStream openOutputStream() { return bytes; }
    }

    /**
     * Compiles the rewritten sources to memory and returns a loader for them.
     *
     * <p>Warnings are not errors here: the rewriter reads locals the original never read, and a
     * host project built with warnings-as-errors would fail a compile that is not the user's fault.
     */
    static ClassLoader compile(Map<String, String> sources) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            throw new IllegalStateException("no system Java compiler — tracing needs a JDK, not a JRE");
        }
        Map<String, ClassBytes> output = new HashMap<>();
        StandardJavaFileManager std = compiler.getStandardFileManager(null, null, StandardCharsets.UTF_8);
        JavaFileManager fm = new ForwardingJavaFileManager<StandardJavaFileManager>(std) {
            @Override
            public JavaFileObject getJavaFileForOutput(Location location, String className,
                                                       JavaFileObject.Kind kind, FileObject sibling) {
                ClassBytes cb = new ClassBytes(className);
                output.put(className, cb);
                return cb;
            }
        };

        List<JavaFileObject> units = new ArrayList<>();
        for (Map.Entry<String, String> e : sources.entrySet()) {
            units.add(new StringSource(e.getKey().replace('.', '/') + ".java", e.getValue()));
        }

        // The classpath must carry this jar, so the rewritten code's TraceHook references resolve to
        // the SAME class this process is using — which is the whole reason the traced copy shares
        // our statics and therefore our tape.
        List<String> options = new ArrayList<>(List.of(
                "-classpath", System.getProperty("java.class.path"), "-nowarn", "-proc:none"));

        StringBuilder diagnostics = new StringBuilder();
        boolean ok = compiler.getTask(null, fm, d -> {
            if (d.getKind() == javax.tools.Diagnostic.Kind.ERROR) {
                diagnostics.append(d.toString()).append('\n');
            }
        }, options, null, units).call();
        fm.close();
        if (!ok) {
            throw new IllegalStateException("the instrumented copy did not compile:\n" + diagnostics);
        }

        Map<String, byte[]> defined = new HashMap<>();
        for (Map.Entry<String, ClassBytes> e : output.entrySet()) {
            defined.put(e.getKey(), e.getValue().bytes.toByteArray());
        }
        return new ClassLoader(Tracer.class.getClassLoader()) {
            @Override
            protected Class<?> findClass(String name) throws ClassNotFoundException {
                byte[] b = defined.get(name);
                if (b == null) throw new ClassNotFoundException(name);
                return defineClass(name, b, 0, b.length);
            }
        };
    }

    /** The instrumented text, for a test or a curious reader. */
    public static String preview(String path) throws IOException {
        String src = Files.readString(Paths.get(path), StandardCharsets.UTF_8);
        return instrument(Paths.get(path).getFileName().toString(), src);
    }

}
