// Variable-level tracing: every local, on every executed line, of the code you name.
//
// This is what turns "what was `level` when it went wrong?" from an inference into a lookup. A
// tape says what the code ASKED the world and what it ANSWERED; it is silent about what the code
// believed in between. That silence is where the interesting bugs live — the ones whose output is
// perfectly self-consistent and still wrong, because some internal value was wrong and every
// number downstream of it agreed.
//
// This file is the format and the query. It holds no runtime machinery and no Roslyn, so it
// compiles on netstandard2.0 like everything else: a trace written anywhere can be read here.
// Producing one is the hard part, and that lives in Tracer.cs, on net8.0 only.
//
// The event shapes are a SHARED CONTRACT with the Python and Node tracers — a header, then one
// event per call, per line that changed a local, per return, per raise. Values are data, not
// ToString() reprs, so numbers compare, documents are inspectable, and two traces of the same
// execution are equal (reprs carrying memory addresses never were).

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;

namespace FlightRecorder
{
    /// <summary>One sighting of a named variable, at the line whose execution produced it.</summary>
    public sealed class Obs
    {
        public string At { get; }     // "File.cs:42"
        public string Fn { get; }     // the qualified name of the frame's method
        public string Name { get; }
        public object? Value { get; }

        internal Obs(string at, string fn, string name, object? value)
        { At = at; Fn = fn; Name = name; Value = value; }

        public override string ToString() => $"{Name}={Serial.RenderTraced(Value)} at {At} in {Fn}";
    }

    public sealed class TraceCall
    {
        public string At { get; }
        public string Fn { get; }
        public IReadOnlyDictionary<string, object?> Args { get; }
        internal TraceCall(string at, string fn, IReadOnlyDictionary<string, object?> args)
        { At = at; Fn = fn; Args = args; }
    }

    public sealed class TraceReturn
    {
        public string At { get; }
        public string Fn { get; }
        public object? Value { get; }
        internal TraceReturn(string at, string fn, object? value) { At = at; Fn = fn; Value = value; }
    }

    public sealed class TraceRaise
    {
        public string At { get; }
        public string Fn { get; }
        public string Type { get; }
        public string Detail { get; }
        internal TraceRaise(string at, string fn, string type, string detail)
        { At = at; Fn = fn; Type = type; Detail = detail; }
    }

    /// <summary>A traced execution's internal state, queryable.</summary>
    public sealed class Trace
    {
        /// <summary>1: values were reprs. 2: values are data (see <see cref="Serial.TraceJsonable"/>).</summary>
        public const long TraceVersion = 2;

        private readonly List<Dictionary<string, object?>> _events;

        public Trace(IEnumerable<Dictionary<string, object?>> events)
        {
            // The header is format metadata, not an observation. Dropping it here means every
            // query below can assume every event has an "e" worth reading.
            _events = events.Where(e => (e.GetValueOrNull("e") as string) != "H").ToList();
        }

        /// <summary>The raw events, in execution order — the wire form, for a consumer who wants
        /// to diff two traces or write one out.</summary>
        public IReadOnlyList<Dictionary<string, object?>> Events => _events;

        public int Count => _events.Count;

        /// <summary>Read a trace back from JSONL.
        ///
        /// A version-1 trace holds reprs, and asserting arithmetic over reprs fails confusingly
        /// rather than loudly. Traces are cheap: regenerate rather than guess.</summary>
        public static Trace Load(string path)
        {
            var objs = new List<Dictionary<string, object?>>();
            foreach (var line in File.ReadAllLines(path))
            {
                if (line.Trim().Length == 0) continue;
                objs.Add((Dictionary<string, object?>)Json.Parse(line)!);
            }
            var header = objs.FirstOrDefault(o => (o.GetValueOrNull("e") as string) == "H");
            var version = header?.GetValueOrNull("trace_version");
            if (!(version is long v && v == TraceVersion))
                throw new InvalidDataException(
                    $"{Path.GetFileName(path)} was written by an older tracer " +
                    $"(version {version ?? 1L}, need {TraceVersion}) — re-run the replay to regenerate it");
            return new Trace(objs);
        }

        /// <summary>Every value <paramref name="name"/> took, in execution order: the argument it
        /// arrived with, and each line that CHANGED it.
        ///
        /// Only changes, because a variable unchanged across forty lines is forty lines of noise —
        /// and a timeline you have to skim is a timeline you stop reading.</summary>
        public IReadOnlyList<Obs> Values(string name)
        {
            var outp = new List<Obs>();
            foreach (var e in _events)
            {
                var kind = e.GetValueOrNull("e") as string;
                var bag = kind == "L" ? e.GetValueOrNull("d") as IDictionary<string, object?>
                        : kind == "C" ? e.GetValueOrNull("args") as IDictionary<string, object?>
                        : null;
                if (bag == null || !bag.TryGetValue(name, out var raw)) continue;
                outp.Add(new Obs(At(e), Fn(e), name, Serial.FromTraceJsonable(raw)));
            }
            return outp;
        }

        public Obs? First(string name) => Values(name).FirstOrDefault();
        public Obs? Final(string name) => Values(name).LastOrDefault();

        /// <summary>Every distinct variable the trace ever saw, sorted.</summary>
        public IReadOnlyList<string> Names()
        {
            var seen = new SortedSet<string>(StringComparer.Ordinal);
            foreach (var e in _events)
            {
                var kind = e.GetValueOrNull("e") as string;
                var bag = kind == "L" ? e.GetValueOrNull("d") as IDictionary<string, object?>
                        : kind == "C" ? e.GetValueOrNull("args") as IDictionary<string, object?>
                        : null;
                if (bag == null) continue;
                foreach (var k in bag.Keys) seen.Add(k);
            }
            return seen.ToList();
        }

        public IReadOnlyList<TraceCall> Calls(string? fn = null) =>
            _events.Where(e => (e.GetValueOrNull("e") as string) == "C" && Matches(e, fn))
                .Select(e => new TraceCall(At(e), Fn(e), Revive(e.GetValueOrNull("args"))))
                .ToList();

        public IReadOnlyList<TraceReturn> Returns(string? fn = null) =>
            _events.Where(e => (e.GetValueOrNull("e") as string) == "R" && Matches(e, fn))
                .Select(e => new TraceReturn(At(e), Fn(e), Serial.FromTraceJsonable(e.GetValueOrNull("v"))))
                .ToList();

        public IReadOnlyList<TraceRaise> Raised() =>
            _events.Where(e => (e.GetValueOrNull("e") as string) == "X")
                .Select(e => new TraceRaise(At(e), Fn(e),
                    e.GetValueOrNull("type") as string ?? "", e.GetValueOrNull("v") as string ?? ""))
                .ToList();

        /// <summary>The timeline of one variable, for a human or a failure message.</summary>
        public string Render(string name)
        {
            var vs = Values(name);
            if (vs.Count == 0) return $"{name}: never observed";
            return string.Join("\n", vs.Select(o =>
                $"  {o.At.PadRight(28)} {name} = {Serial.RenderTraced(o.Value)}"));
        }

        /// <summary>The whole execution, top-down: calls, the lines that changed something,
        /// returns and raises. What you read when you do not yet know which variable to blame.</summary>
        public string Render()
        {
            var sb = new StringBuilder();
            foreach (var e in _events)
            {
                var at = At(e).PadRight(28);
                switch (e.GetValueOrNull("e") as string)
                {
                    case "C":
                        sb.Append($"{at} call {Fn(e)}({Bag(e.GetValueOrNull("args"))})\n");
                        break;
                    case "L":
                        sb.Append($"{at}      {Bag(e.GetValueOrNull("d"))}\n");
                        break;
                    case "R":
                        sb.Append($"{at} return {Serial.RenderTraced(e.GetValueOrNull("v"))}\n");
                        break;
                    case "X":
                        sb.Append($"{at} raise {e.GetValueOrNull("type")}: {e.GetValueOrNull("v")}\n");
                        break;
                }
            }
            return sb.ToString();
        }

        /// <summary>The trace as JSONL, header first — the form another runtime reads.</summary>
        public string ToJsonl()
        {
            var sb = new StringBuilder();
            sb.Append(Json.Stringify(new Dictionary<string, object?>
            {
                ["e"] = "H", ["trace_version"] = TraceVersion,
            })).Append('\n');
            foreach (var e in _events) sb.Append(Json.Stringify(e)).Append('\n');
            return sb.ToString();
        }

        // A trailing-segment match, so `Calls("StudyStatus")` finds `Tools.StudyStatus` — you
        // should not have to spell a qualified name to ask about a method you can see.
        private static bool Matches(IDictionary<string, object?> e, string? fn)
        {
            if (fn == null) return true;
            var name = e.GetValueOrNull("fn") as string ?? "";
            return name == fn || name.EndsWith("." + fn, StringComparison.Ordinal);
        }

        private static string At(IDictionary<string, object?> e) => e.GetValueOrNull("at") as string ?? "";
        private static string Fn(IDictionary<string, object?> e) => e.GetValueOrNull("fn") as string ?? "";

        private static IReadOnlyDictionary<string, object?> Revive(object? bag)
        {
            var outp = new Dictionary<string, object?>();
            if (bag is IDictionary<string, object?> map)
                foreach (var kv in map) outp[kv.Key] = Serial.FromTraceJsonable(kv.Value);
            return outp;
        }

        private static string Bag(object? bag)
        {
            if (!(bag is IDictionary<string, object?> map)) return "";
            return string.Join(", ", map.Select(kv => $"{kv.Key}={Serial.RenderTraced(kv.Value, 40)}"));
        }
    }

    /// <summary>Where the trace hook writes. One sink per traced run.
    ///
    /// The sink owns change detection: the hook hands it the whole readable scope on every line,
    /// and the sink emits only what moved. Doing it here rather than in the rewritten code keeps
    /// the injected call sites trivial, which is what makes them safe to inject.</summary>
    public sealed class TraceSink
    {
        private readonly object _lock = new object();
        private readonly List<Dictionary<string, object?>> _events = new List<Dictionary<string, object?>>();

        // Keyed by frame, not by method: a recursive call has its own locals, and comparing the
        // inner frame's `n` against the outer frame's would report changes that never happened.
        private readonly Dictionary<long, Dictionary<string, string>> _prev =
            new Dictionary<long, Dictionary<string, string>>();

        /// <summary>Optional: mirror every event to a JSONL file as it happens, so a run that
        /// dies mid-flight still leaves the tape it had written.</summary>
        public string? Path { get; }

        private readonly TextWriter? _writer;

        // The recording's forbid patterns, captured once. A trace is the WORST place for a
        // credential to land: it holds every local on every executed line, including values as
        // they were BEFORE they reached any masking, and tracing is exactly what you switch on
        // when debugging the request that went wrong. The tape beside it is masked and asserted
        // clean; without this the trace was neither.
        private readonly IReadOnlyList<Regex> _forbid;

        private static readonly Regex[] NoPatterns = new Regex[0];

        /// <param name="boundary">Whose <see cref="Boundary.Forbid"/> patterns this trace must
        /// pass. Defaults to the boundary recording declared, so a trace taken during a recorded
        /// run inherits the tripwire without every call site having to remember to pass it.</param>
        public TraceSink(string? path = null, Boundary? boundary = null)
        {
            Path = path;
            _forbid = (boundary ?? Recorder.CurrentBoundary)?.Forbid ?? (IReadOnlyList<Regex>)NoPatterns;
            if (path != null)
            {
                var header = new Dictionary<string, object?>
                { ["e"] = "H", ["trace_version"] = Trace.TraceVersion };
                // Ahead of the open, not merely ahead of the write: a refused trace must leave no
                // file at all, rather than an empty one that reads like a run which traced nothing.
                if (_forbid.Count > 0) Tripwire.Guard(Json.Stringify(header), _forbid, "the trace header");
                _writer = new StreamWriter(path, append: false, encoding: new UTF8Encoding(false));
                WriteRaw(header);
            }
        }

        public int Count { get { lock (_lock) return _events.Count; } }

        /// <summary>The events collected so far, as a queryable trace.</summary>
        public Trace Snapshot()
        {
            lock (_lock) return new Trace(_events.ToList());
        }

        /// <summary>
        /// The events collected since <paramref name="from"/> — the count taken before the stretch
        /// of execution you care about.
        ///
        /// One sink outlives one call: a replay traces the code it replays, and the sink may
        /// already hold observations from an earlier call on the same tape. Slicing is what keeps
        /// a report's trace the trace of ITS call and not of everything that ever ran.
        /// </summary>
        public Trace Snapshot(int from)
        {
            lock (_lock)
            {
                if (from <= 0) return new Trace(_events.ToList());
                return new Trace(from >= _events.Count
                    ? new List<Dictionary<string, object?>>()
                    : _events.GetRange(from, _events.Count - from));
            }
        }

        public void Close()
        {
            lock (_lock) { _writer?.Flush(); _writer?.Dispose(); }
        }

        // --- what the hook calls ---------------------------------------------------------

        internal void Call(long frame, string fn, string at, string[] names, object?[] values)
        {
            var encoded = Encode(names, values);
            lock (_lock)
            {
                _prev[frame] = Keys(encoded);
                WriteRaw(new Dictionary<string, object?> { ["e"] = "C", ["fn"] = fn, ["at"] = at, ["args"] = encoded });
            }
        }

        internal void Line(long frame, string fn, string at, string[] names, object?[] values)
        {
            var encoded = Encode(names, values);
            lock (_lock)
            {
                if (!_prev.TryGetValue(frame, out var prev)) prev = new Dictionary<string, string>();
                var delta = new Dictionary<string, object?>();
                foreach (var kv in encoded)
                {
                    // Compare CANONICAL JSON, not the objects. Plain equality would miss
                    // type-changing transitions the CLR calls equal (1 vs 1L vs 1.0), and would
                    // call two structurally identical dictionaries different because they are
                    // different references — a trace full of phantom changes is unreadable.
                    var key = Canonical(kv.Value);
                    if (prev.TryGetValue(kv.Key, out var was) && was == key) continue;
                    delta[kv.Key] = kv.Value;
                    prev[kv.Key] = key;
                }
                _prev[frame] = prev;
                if (delta.Count > 0)
                    WriteRaw(new Dictionary<string, object?> { ["e"] = "L", ["fn"] = fn, ["at"] = at, ["d"] = delta });
            }
        }

        internal void Return(long frame, string fn, string at, object? value)
        {
            lock (_lock)
            {
                WriteRaw(new Dictionary<string, object?>
                { ["e"] = "R", ["fn"] = fn, ["at"] = at, ["v"] = Serial.TraceJsonable(value) });
            }
        }

        internal void Raise(long frame, string fn, string at, Exception e)
        {
            lock (_lock)
            {
                WriteRaw(new Dictionary<string, object?>
                {
                    ["e"] = "X", ["fn"] = fn, ["at"] = at,
                    ["type"] = e.GetType().Name, ["v"] = Safe(e),
                });
            }
        }

        internal void Exit(long frame)
        {
            lock (_lock) _prev.Remove(frame);
        }

        // --- plumbing --------------------------------------------------------------------

        private static Dictionary<string, object?> Encode(string[] names, object?[] values)
        {
            var outp = new Dictionary<string, object?>();
            for (var i = 0; i < names.Length && i < values.Length; i++)
                outp[names[i]] = Serial.TraceJsonable(values[i]);
            return outp;
        }

        private static Dictionary<string, string> Keys(Dictionary<string, object?> encoded)
        {
            var outp = new Dictionary<string, string>();
            foreach (var kv in encoded) outp[kv.Key] = Canonical(kv.Value);
            return outp;
        }

        private static string Canonical(object? v)
        {
            try { return Json.Canonical(v); }
            catch { return "<unrenderable>"; }
        }

        private static string Safe(Exception e)
        {
            try { return $"{e.Message}"; }
            catch { return "<unreadable exception>"; }
        }

        private void WriteRaw(Dictionary<string, object?> ev)
        {
            string? line = null;
            if (_forbid.Count > 0)
            {
                line = Json.Stringify(ev);
                // Before the buffer, not just before the file — and so also when there is no file.
                // A pathless sink is not private: its events reach an invariant, a printed report
                // and Trace.ToJsonl(), which is a tape a caller can write anywhere. "In memory" is
                // a statement about latency, not about confinement, and the moment the guard is
                // conditional on a path the secret survives to the first consumer who saves one.
                var at = ev.GetValueOrNull("at") as string;
                Tripwire.Guard(line, _forbid, $"a traced '{ev.GetValueOrNull("e")}' record"
                    + (at != null ? $" at {at}" : ""));
            }
            _events.Add(ev);
            if (_writer != null)
            {
                _writer.Write(line ?? Json.Stringify(ev));
                _writer.Write('\n');
                _writer.Flush();
            }
        }
    }

    /// <summary>The call sites the rewritten code targets.
    ///
    /// This is why the rewrite works at all. The recompiled copy of the traced source is a
    /// DIFFERENT assembly holding DIFFERENT types from the original — normally a fatal problem.
    /// It is not one here, because in flight-recorder the code under replay reaches the world
    /// only through the boundary, and the boundary lives in this assembly, which the rewritten
    /// copy references and therefore SHARES. Same statics, same hook, same tape. Type identity
    /// with the original assembly does not matter because it is not a channel.
    ///
    /// Public because generated code has to be able to call it, and it must stay so: every
    /// signature here is a compile target, not an invitation.</summary>
    public static class TraceHook
    {
        private static long _frames;

        /// <summary>The sink for the current traced run, or null. Ambient and process-global,
        /// exactly like <c>Hook.Mode</c> — tracing is a replay-time act, and replays do not run
        /// concurrently with each other in this library.</summary>
        public static TraceSink? Sink { get; set; }

        /// <summary>Open a frame. Returns its id, or 0 when nothing is listening — the whole
        /// hook then costs one null check per line, which is what lets the rewritten assembly be
        /// loaded once and used for both traced and untraced runs.</summary>
        public static long Enter(string fn, string at, string[] names, object?[] values)
        {
            var sink = Sink;
            if (sink == null) return 0;
            var frame = System.Threading.Interlocked.Increment(ref _frames);
            sink.Call(frame, fn, at, names, values);
            return frame;
        }

        public static void Line(long frame, string fn, string at, string[] names, object?[] values)
        {
            if (frame == 0) return;
            Sink?.Line(frame, fn, at, names, values);
        }

        public static void Return(long frame, string fn, string at, object? value)
        {
            if (frame == 0) return;
            Sink?.Return(frame, fn, at, value);
        }

        /// <summary>Record a raise, then get out of the way. The rewritten code rethrows with a
        /// bare <c>throw;</c> immediately after, so the original stack survives — a tracer that
        /// truncated stacks would be paid for in every future debugging session.</summary>
        public static void Raise(long frame, string fn, string at, Exception e)
        {
            if (frame == 0) return;
            Sink?.Raise(frame, fn, at, e);
        }

        public static void Exit(long frame)
        {
            if (frame == 0) return;
            Sink?.Exit(frame);
        }

        /// <summary>Identity passthrough, used to observe a returned value without naming its
        /// type. The rewriter cannot always spell a method's return type in a local declaration
        /// (`var` fails on a lambda, an anonymous type escapes its scope), but it can always
        /// wrap the returned expression in a generic call that gives it back unchanged.</summary>
        public static T Returned<T>(long frame, string fn, string at, T value)
        {
            if (frame != 0) Sink?.Return(frame, fn, at, value);
            return value;
        }

        /// <summary>Where a line is, in the ORIGINAL file's terms.</summary>
        public static string At(string file, int line) =>
            $"{file}:{line.ToString(CultureInfo.InvariantCulture)}";

        /// <summary>How many observations have been made — the mark to slice a later trace from.</summary>
        public static int Mark() => Sink?.Count ?? 0;

        /// <summary>
        /// The trace of what ran since <paramref name="from"/>.
        ///
        /// Never null: an ordinary, uninstrumented run yields an EMPTY trace rather than nothing,
        /// so an invariant that queries it gets "never observed" instead of a crash — and a claim
        /// about a variable nobody traced fails honestly rather than passing vacuously.
        /// </summary>
        public static Trace Live(int from) =>
            Sink?.Snapshot(from) ?? new Trace(new List<Dictionary<string, object?>>());
    }
}
