// The rewriter — how .NET gets a line hook it does not have.
//
// Python gets this for free: `sys.settrace` fires on every call, every line, every return, and
// hands you the frame's locals by name. Node has no such hook but has the V8 Inspector, which is
// where a debugger reads the same information, and that is enough.
//
// .NET has NEITHER, and the reason is structural rather than an oversight. The runtime exposes
// exactly two mechanisms that can observe a line: ICorDebug and ICorProfiler. ICorDebug is
// documented as not callable in-process — a debugger is a separate process by construction, so
// tracing your own test host means launching a child that attaches back to its parent, with
// ptrace permissions on Linux and the debuggee suspended while you read it. ICorProfiler is
// worse for a library: the CLR loads it at startup from a native COM DLL named by
// CORECLR_PROFILER_PATH, so adopting it means environment variables set before the process
// exists. Neither survives the constraint that matters here: `dotnet add package`, call an API.
//
// So the code is rewritten instead. Roslyn parses the sources under trace, this rewriter inserts
// the hook calls, the result is compiled to memory and loaded, and the traced copy runs.
//
// WHY A RECOMPILED COPY IS STILL THE SAME EXECUTION. The copy's types are distinct from the
// originals — a separate assembly means a separate `Deck`. That would be fatal for most
// instrumentation, and is harmless here, because in flight-recorder the code under replay
// reaches the world ONLY through the boundary, and the boundary lives in FlightRecorder.dll,
// which the rewritten assembly references and therefore SHARES. Same statics, same hook, same
// answers off the tape. Type identity with the original user assembly is not a channel, so
// duplicating it costs nothing.
//
// WHY SOURCE AND NOT IL. Cecil would avoid needing the sources, and would then have to undo what
// the compiler did: an async method's locals are not locals, they are fields on a generated state
// machine with mangled names, and a closure's captures live on a display class. Rewriting before
// the compiler runs means `x` is still `x` — an async tool traces exactly like a synchronous one,
// which is the common case here and the case IL rewriting is worst at.
//
// Tracing is for REPLAY. It compiles an assembly and records every local on every line; that is
// milliseconds per line, which is fine when resurrecting one recorded execution and never fine
// in a request path.

#if NET8_0_OR_GREATER

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

namespace FlightRecorder
{
    /// <summary>What went wrong turning a source file into a traced assembly.</summary>
    public sealed class TraceCompilationException : Exception
    {
        public TraceCompilationException(string message) : base(message) { }
    }

    /// <summary>
    /// Compiles a traced copy of the named sources and runs a method in it.
    ///
    /// The sources must be everything the traced code needs that is not already in a referenced
    /// assembly — typically the file under trace and its collaborators. Anything it reaches
    /// through the boundary needs no source: that is FlightRecorder's, and it is shared.
    /// </summary>
    public static class Tracer
    {
        /// <summary>
        /// Run <paramref name="methodName"/> on <paramref name="typeName"/>, traced.
        ///
        /// Returns what the method returned and the trace of how it got there. An exception
        /// thrown by the traced code propagates — with the trace up to the throw already
        /// recorded, which is the half of a failed run worth having.
        /// </summary>
        public static (object? Result, Trace Trace) Run(
            IEnumerable<string> sourcePaths, string typeName, string methodName, params object?[] args)
        {
            var asm = Compile(sourcePaths);
            var type = asm.GetType(typeName)
                ?? throw new TraceCompilationException(
                    $"the traced assembly has no type '{typeName}' — it defines: " +
                    string.Join(", ", asm.GetTypes().Select(t => t.FullName)));
            var method = type.GetMethod(methodName,
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance)
                ?? throw new TraceCompilationException($"'{typeName}' has no method '{methodName}'");

            object? instance = method.IsStatic ? null : Activator.CreateInstance(type);

            var sink = new TraceSink();
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try
            {
                object? result;
                try
                {
                    result = method.Invoke(instance, args);
                }
                catch (TargetInvocationException e) when (e.InnerException != null)
                {
                    // Reflection wraps whatever the code threw. Unwrap it: a caller asserting on
                    // the exception should see the one the code raised, not the messenger.
                    throw e.InnerException;
                }

                // An async method hands back a Task. Await it here, or the trace is a snapshot of
                // the synchronous prefix and the run is still going when we read it.
                if (result is System.Threading.Tasks.Task task)
                {
                    task.GetAwaiter().GetResult();
                    var t = task.GetType();
                    result = t.IsGenericType ? t.GetProperty("Result")?.GetValue(task) : null;
                }

                return (result, sink.Snapshot());
            }
            finally
            {
                TraceHook.Sink = previous;
            }
        }

        /// <summary>Rewrite the sources and compile them to an in-memory assembly.</summary>
        public static Assembly Compile(IEnumerable<string> sourcePaths)
        {
            var paths = sourcePaths.ToList();
            if (paths.Count == 0) throw new TraceCompilationException("no sources to trace");

            var trees = new List<SyntaxTree>();
            foreach (var p in paths)
            {
                if (!File.Exists(p)) throw new TraceCompilationException($"no such source file: {p}");
                var text = File.ReadAllText(p);
                trees.Add(CSharpSyntaxTree.ParseText(SourceText.From(text, System.Text.Encoding.UTF8), path: p));
            }

            // A distinct name every time. Two traced compilations of the same file are two
            // assemblies, and giving them one name makes the second silently resolve to the first.
            var name = "FlightRecorder.Traced." + Guid.NewGuid().ToString("N");

            var references = PlatformReferences();
            var compilation = CSharpCompilation.Create(name, trees, references,
                new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary,
                    optimizationLevel: OptimizationLevel.Debug,
                    // The rewriter reads locals that the original code may never read. Left as
                    // errors, an unused-variable warning-as-error in the host project would fail
                    // a compile that is not the user's fault.
                    generalDiagnosticOption: ReportDiagnostic.Suppress));

            // The rewrite is per-tree and needs that tree's SemanticModel — definite assignment
            // is a semantic question, and getting it wrong is a compile error (CS0165), not a
            // subtly wrong trace.
            var rewritten = new List<SyntaxTree>();
            foreach (var tree in compilation.SyntaxTrees)
            {
                var model = compilation.GetSemanticModel(tree);
                var rewriter = new TraceRewriter(model, tree.FilePath);
                var root = rewriter.Visit(tree.GetRoot());
                rewritten.Add(CSharpSyntaxTree.ParseText(SourceText.From(root.ToFullString(), System.Text.Encoding.UTF8), path: tree.FilePath));
            }

            var final = CSharpCompilation.Create(name, rewritten, references,
                new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary,
                    optimizationLevel: OptimizationLevel.Debug,
                    generalDiagnosticOption: ReportDiagnostic.Suppress));

            using var pe = new MemoryStream();
            using var pdb = new MemoryStream();
            var emit = final.Emit(pe, pdb);
            if (!emit.Success)
            {
                var errors = emit.Diagnostics
                    .Where(d => d.Severity == DiagnosticSeverity.Error)
                    .Select(d => $"  {d.Id} {d.GetMessage()} at {d.Location.GetLineSpan()}")
                    .Take(20);
                throw new TraceCompilationException(
                    "the traced copy did not compile — this is a bug in the rewriter, not in the " +
                    "code under trace:\n" + string.Join("\n", errors));
            }

            return Assembly.Load(pe.ToArray(), pdb.ToArray());
        }

        /// <summary>
        /// Everything the traced copy is allowed to reference: the framework, plus every assembly
        /// already loaded here — which is what makes FlightRecorder itself SHARED rather than
        /// recompiled, and therefore what makes the hook and the boundary the same objects.
        /// </summary>
        private static List<MetadataReference> PlatformReferences()
        {
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var refs = new List<MetadataReference>();

            // The trusted platform assemblies are the framework's own reference set. Walking
            // loaded assemblies alone misses anything the traced source uses but this process has
            // not happened to load yet.
            if (AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") is string tpa)
            {
                foreach (var path in tpa.Split(Path.PathSeparator))
                {
                    if (path.Length > 0 && File.Exists(path) && seen.Add(Path.GetFileName(path)))
                        refs.Add(MetadataReference.CreateFromFile(path));
                }
            }

            foreach (var a in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (a.IsDynamic) continue;
                var loc = a.Location;
                if (string.IsNullOrEmpty(loc) || !File.Exists(loc)) continue;
                if (seen.Add(Path.GetFileName(loc))) refs.Add(MetadataReference.CreateFromFile(loc));
            }

            return refs;
        }
    }
}

#endif
