// The syntax rewrite: where the hook calls actually get inserted.
//
// For every method with a body, this produces:
//
//     long __fr = TraceHook.Enter("Type.Method", "File.cs:12", new[]{"user"}, new object[]{user});
//     try {
//         TraceHook.Line(__fr, "Type.Method", "File.cs:13", new[]{"user"}, new object[]{user});
//         var row = Load(user);
//         TraceHook.Line(__fr, "Type.Method", "File.cs:14", new[]{"user","row"}, new object[]{user,row});
//         return TraceHook.Returned(__fr, "Type.Method", "File.cs:14", row.Name);
//     } finally { TraceHook.Exit(__fr); }
//
// The sink turns that stream of full snapshots into deltas, so a variable unchanged across forty
// lines is one observation and not forty.
//
// THE TRAP, and it is the whole engineering cost of this file: you cannot read a local before it
// is definitely assigned. `int x; TraceHook.Line(..., new object[]{x});` is CS0165 and the traced
// copy does not compile at all — the failure is total and arrives at the end, which is why the
// gating is done up front against Roslyn's own flow analysis rather than guessed at. Every
// candidate is filtered through `DefinitelyAssignedOnEntry` for the statement it precedes, which
// is the same question the compiler will ask, asked of the same engine.
//
// Three kinds of local can never be passed as `object` and are skipped rather than gated:
// `ref` locals (an alias, not a value), ref-like types — `Span<T>` and friends, which the runtime
// forbids from ever landing on the heap — and pointers. Skipping them loses those variables from
// the trace; boxing them loses the ability to compile.

#if NET8_0_OR_GREATER

using System.Collections.Generic;
using System.Linq;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using static Microsoft.CodeAnalysis.CSharp.SyntaxFactory;

namespace FlightRecorder
{
    internal sealed class TraceRewriter : CSharpSyntaxRewriter
    {
        private const string Frame = "__fr_frame";
        private const string Hook = "global::FlightRecorder.TraceHook";

        private readonly SemanticModel _model;
        private readonly string _file;
        private string _fn = "";

        internal TraceRewriter(SemanticModel model, string file)
        {
            _model = model;
            _file = System.IO.Path.GetFileName(file);
        }

        // --- the frames worth entering -----------------------------------------------------

        public override SyntaxNode? VisitMethodDeclaration(MethodDeclarationSyntax node)
        {
            if (node.Body == null) return base.VisitMethodDeclaration(node);

            var symbol = _model.GetDeclaredSymbol(node);
            var name = symbol != null ? $"{symbol.ContainingType.Name}.{symbol.Name}" : node.Identifier.Text;

            // Descend with `base.VisitMethodDeclaration`, never `base.Visit`: Visit dispatches by
            // node kind, so it would land straight back in this override and recurse until the
            // stack gives out.
            var outer = _fn;
            _fn = name;
            var visited = (MethodDeclarationSyntax)base.VisitMethodDeclaration(node)!;
            _fn = outer;

            return visited.Body == null ? visited
                : visited.WithBody(Wrap(node, visited.Body, name, node.ParameterList));
        }

        public override SyntaxNode? VisitLocalFunctionStatement(LocalFunctionStatementSyntax node)
        {
            if (node.Body == null) return base.VisitLocalFunctionStatement(node);

            var name = node.Identifier.Text;
            var outer = _fn;
            _fn = name;
            var visited = (LocalFunctionStatementSyntax)base.VisitLocalFunctionStatement(node)!;
            _fn = outer;

            return visited.Body == null ? visited
                : visited.WithBody(Wrap(node, visited.Body, name, node.ParameterList));
        }

        /// <summary>
        /// Wrap one body: announce the frame with its arguments, and close it in a `finally` so a
        /// throw still ends the frame. An expression-bodied member has no statement list to hang
        /// anything on and is left alone by the callers above.
        /// </summary>
        private BlockSyntax Wrap(SyntaxNode original, BlockSyntax body, string fn, ParameterListSyntax parameters)
        {
            var args = parameters.Parameters
                .Where(p => Traceable(_model.GetDeclaredSymbol(p)?.Type, p.Modifiers))
                .Select(p => p.Identifier.Text)
                .ToList();

            var enter = ParseStatement(
                $"long {Frame} = {Hook}.Enter({Literal(fn)}, {At(original)}, {Names(args)}, {Values(args)});");

            return Block(
                enter,
                TryStatement()
                    .WithBlock(body)
                    .WithFinally(FinallyClause(Block(ParseStatement($"{Hook}.Exit({Frame});")))));
        }

        // --- one line at a time ------------------------------------------------------------

        public override SyntaxNode? VisitBlock(BlockSyntax node)
        {
            var visited = (BlockSyntax)base.VisitBlock(node)!;
            if (_fn.Length == 0) return visited;

            var statements = new List<StatementSyntax>();
            // Pair each ORIGINAL statement with its rewritten self: the original is the one the
            // semantic model knows about, and the only one whose line numbers are the file's.
            for (var i = 0; i < node.Statements.Count && i < visited.Statements.Count; i++)
            {
                var original = node.Statements[i];
                var rewritten = visited.Statements[i];

                // A local declaration is not yet in scope on the line that declares it, and a
                // block's opening brace is not a line anyone executes.
                var readable = ReadableAt(original);
                if (readable.Count > 0 || original is not LocalDeclarationStatementSyntax)
                {
                    statements.Add(ParseStatement(
                        $"{Hook}.Line({Frame}, {Literal(_fn)}, {At(original)}, {Names(readable)}, {Values(readable)});"));
                }
                statements.Add(rewritten);
            }
            return visited.WithStatements(List(statements));
        }

        /// <summary>
        /// A return carries the value out, so it is the last thing worth recording — and it must
        /// be recorded around the expression, not before it, because the expression is often
        /// where the value is computed.
        /// </summary>
        public override SyntaxNode? VisitReturnStatement(ReturnStatementSyntax node)
        {
            var visited = (ReturnStatementSyntax)base.VisitReturnStatement(node)!;
            if (_fn.Length == 0 || visited.Expression == null) return visited;

            // `Returned<T>` is generic and returns its argument, so the rewrite is type-preserving:
            // it cannot change an overload resolution or a conversion the original relied on.
            return visited.WithExpression(ParseExpression(
                $"{Hook}.Returned({Frame}, {Literal(_fn)}, {At(node)}, {visited.Expression.ToFullString()})"));
        }

        // --- what may be read, and where ---------------------------------------------------

        /// <summary>
        /// The locals and parameters that are in scope AND definitely assigned immediately before
        /// <paramref name="statement"/> — the exact set the compiler will permit reading there.
        /// </summary>
        private List<string> ReadableAt(StatementSyntax statement)
        {
            var outp = new List<string>();
            try
            {
                var flow = _model.AnalyzeDataFlow(statement);
                if (flow == null || !flow.Succeeded) return outp;

                var seen = new HashSet<string>();
                foreach (var s in flow.DefinitelyAssignedOnEntry)
                {
                    ITypeSymbol? type;
                    bool isRef;
                    switch (s)
                    {
                        case ILocalSymbol local:
                            type = local.Type;
                            isRef = local.IsRef;
                            break;
                        case IParameterSymbol param:
                            type = param.Type;
                            isRef = param.RefKind != RefKind.None;
                            break;
                        default:
                            continue; // fields and properties are not this frame's state
                    }

                    if (isRef || !Traceable(type)) continue;
                    // `this` and compiler-generated names are not variables anyone asked about.
                    if (s.Name.Length == 0 || s.Name.StartsWith("<")) continue;
                    if (seen.Add(s.Name)) outp.Add(s.Name);
                }
            }
            catch
            {
                // A statement the flow analysis cannot model is a statement traced without its
                // locals. Losing variables is a worse trace; throwing here is no trace at all.
                return new List<string>();
            }
            outp.Sort(System.StringComparer.Ordinal);
            return outp;
        }

        /// <summary>Can a value of this type be handed to the hook as `object` at all?</summary>
        private static bool Traceable(ITypeSymbol? type, SyntaxTokenList modifiers = default)
        {
            if (type == null) return false;
            if (type.IsRefLikeType) return false;                       // Span<T> may never be boxed
            if (type.TypeKind == TypeKind.Pointer) return false;        // nor may a pointer
            if (type.TypeKind == TypeKind.FunctionPointer) return false;
            foreach (var m in modifiers)
            {
                // `out` is not assigned on entry, and `ref`/`in` are aliases.
                if (m.IsKind(SyntaxKind.OutKeyword) || m.IsKind(SyntaxKind.RefKeyword)) return false;
            }
            return true;
        }

        // --- emitting the literals ---------------------------------------------------------

        private string At(SyntaxNode node)
        {
            // The ORIGINAL tree's line, which is the one in the file the reader will open. Read
            // it here, while the node still comes from that tree — after the rewrite every line
            // number below the first insertion is wrong.
            var line = node.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
            return $"{Hook}.At({Literal(_file)}, {line})";
        }

        private static string Literal(string s) =>
            SyntaxFactory.Literal(s).ToFullString();

        private static string Names(IReadOnlyList<string> names) =>
            names.Count == 0
                ? "global::System.Array.Empty<string>()"
                : "new string[]{" + string.Join(",", names.Select(Literal)) + "}";

        private static string Values(IReadOnlyList<string> names) =>
            names.Count == 0
                ? "global::System.Array.Empty<object>()"
                : "new object[]{" + string.Join(",", names) + "}";
    }
}

#endif
