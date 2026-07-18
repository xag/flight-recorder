// Mutation: author hostile boundary states as data.
//
// Recordings make impossible states cheap to construct — an emptied corpus, a clock running
// backwards, an oversized collection are edits to a JSONL file, not database setup. A mutated
// recording replays in PROBE mode: the tape answers the code's boundary questions (matched by
// name, order-monotonic, skipping allowed) but no longer polices arguments, writes, or outputs
// — under mutation those comparisons are meaningless. The verdict belongs to invariants: a
// mutated recording plus a declared claim IS a property test over the boundary.
//
//   var rec = Recording.Load(path);
//   var call = rec.Call(0);
//   call.Read("stream").Result = new List<object?>();          // empty corpus
//   call.Effect("fetch_remote").Result = new { v = 1_000_000_000 };
//   call.Clock.Reverse();                                       // time runs backwards
//   var report = call.Check(body, invariants);                 // probe replay + invariants
//   rec.Save(dir + "/empty-corpus.jsonl");                      // pin it: a suite member now
//
// A saved mutated call carries "probe": true, so replay treats it as a probe fixture — it
// cannot be mistaken for a strict regression pin.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace FlightRecorder
{
    public sealed class Recording
    {
        public Dictionary<string, object?> Header { get; }
        public List<Dictionary<string, object?>> Calls { get; }

        // Mutation exists precisely to EDIT recorded values, so a tape that passed the tripwire
        // when it was written can have a forbidden value put back in by hand and then saved. The
        // write path was guarded; the re-write path was not, and an edit is the easiest way there
        // is to reintroduce the exact thing the boundary swore would never be on a tape.
        private readonly Boundary? _boundary;

        private Recording(Dictionary<string, object?> header, List<Dictionary<string, object?>> calls,
            Boundary? boundary)
        {
            Header = header;
            Calls = calls;
            _boundary = boundary;
        }

        /// <param name="boundary">Whose <see cref="Boundary.Forbid"/> patterns every save of this
        /// recording must pass. Defaults to the boundary recording declared — which is the right
        /// answer inside a live recorded run and null in a test process, so pass it explicitly
        /// when you are mutating a tape the recorder is no longer installed for.</param>
        public static Recording Load(string path, Boundary? boundary = null)
        {
            var tape = Replay.LoadTape(path);
            return new Recording(tape.Header, tape.Calls.ToList(), boundary ?? Recorder.CurrentBoundary);
        }

        public CallHandle Call(int index)
        {
            if (index < 0 || index >= Calls.Count)
                throw new IndexOutOfRangeException($"call {index} out of range: {Calls.Count} call(s)");
            return new CallHandle(Calls[index], this);
        }

        public IReadOnlyList<SpanNode> Spans() => Calls.Select(SpanTree.Build).ToList();

        /// <summary>The whole session, top-down: the meaning of each call, and only then — if some
        /// claim looks wrong — the raw events underneath it.</summary>
        public string RenderSpans() =>
            string.Join("\n\n", Spans().Select((tree, i) => $"call {i}:\n{SpanTree.Render(tree)}"));

        internal string Write(string path, Boundary? boundary = null)
        {
            var forbid = (boundary ?? _boundary)?.Forbid;
            var lines = new List<string> { Json.Stringify(Header) };
            foreach (var call in Calls) lines.Add(Json.Stringify(call));

            // Render and check the WHOLE tape before opening the file. A guard that fired halfway
            // through the write would truncate whatever was already at `path` — losing a good tape
            // to punish a bad edit, and leaving a half-file that replays as a mangled session.
            // Refusing costs nothing here because nothing has been opened yet.
            if (forbid != null && forbid.Count > 0)
                for (var i = 0; i < lines.Count; i++)
                    Tripwire.Guard(lines[i], forbid,
                        i == 0 ? "the header of the mutated recording" : $"mutated call {i - 1}");

            using var w = new StreamWriter(path, append: false);
            foreach (var line in lines) w.Write(line + "\n");
            return path;
        }

        /// <summary>Pin the (mutated) recording as a probe fixture.</summary>
        public string Save(string path, Boundary? boundary = null) => Write(path, boundary);
    }

    public sealed class CallHandle
    {
        public Dictionary<string, object?> Record { get; }
        private readonly Recording _recording;

        internal CallHandle(Dictionary<string, object?> record, Recording recording)
        {
            Record = record;
            _recording = recording;
        }

        internal void Dirty() => Record["probe"] = true;

        private List<Dictionary<string, object?>> Events =>
            (Record.GetValueOrNull("events") as IEnumerable<object?> ?? Enumerable.Empty<object?>())
                .OfType<Dictionary<string, object?>>().ToList();

        private Dictionary<string, object?> Pick(List<Dictionary<string, object?>> matches, string what, int occurrence)
        {
            if (matches.Count == 0)
            {
                var have = Events.Select(e => (e.GetValueOrNull("fn") ?? e.GetValueOrNull("op") ?? e.GetValueOrNull("k"))?.ToString())
                    .Where(x => x != null).Distinct().OrderBy(x => x);
                throw new KeyNotFoundException($"no {what} in this call — its events are: {string.Join(", ", have)}");
            }
            if (occurrence >= matches.Count)
                throw new KeyNotFoundException($"only {matches.Count} × {what} recorded, asked for occurrence {occurrence}");
            return matches[occurrence];
        }

        public EffectHandle Effect(string name, int occurrence = 0)
        {
            var found = Events.Where(e => (e.GetValueOrNull("k") as string) == "fx"
                && (e.GetValueOrNull("fn") as string is string fn && (fn == name || fn.EndsWith("." + name)))).ToList();
            return new EffectHandle(Pick(found, $"effect '{name}'", occurrence), this);
        }

        public ReadHandle Read(string? op = null, int occurrence = 0)
        {
            var found = Events.Where(e => (e.GetValueOrNull("k") as string) == "db" && e.ContainsKey("res")
                && (op == null || (e.GetValueOrNull("op") as string) == op)).ToList();
            return new ReadHandle(Pick(found, op != null ? $"read {op}" : "read", occurrence), this);
        }

        public RandHandle Rand(int occurrence = 0)
        {
            var found = Events.Where(e => (e.GetValueOrNull("k") as string) == "rand").ToList();
            return new RandHandle(Pick(found, "random draw", occurrence), this);
        }

        public ClockHandleEdit Clock =>
            new ClockHandleEdit(Events.Where(e => (e.GetValueOrNull("k") as string) == "now").ToList(), this);

        public IReadOnlyDictionary<string, object?> Kwargs =>
            (Record.GetValueOrNull("kwargs") as Dictionary<string, object?>) ?? new Dictionary<string, object?>();

        public void SetKwargs(string key, object? value)
        {
            if (!(Record.GetValueOrNull("kwargs") is IDictionary<string, object?> kw))
            {
                kw = new Dictionary<string, object?>();
                Record["kwargs"] = kw;
            }
            kw[key] = Serial.ToJsonable(value);
            Dirty();
        }

        public SpanNode Spans() => SpanTree.Build(Record);
        public string RenderSpans() => SpanTree.Render(Spans());

        /// <summary>Replay this (mutated) call in probe mode and assert the invariants against what
        /// the real code does in the mutated world.</summary>
        public InvariantReport Check(Func<IReadOnlyDictionary<string, object?>, object?> body,
            IEnumerable<Invariant> invariants, Boundary? boundary = null)
        {
            Record["probe"] = true;
            var index = _recording.Calls.IndexOf(Record);
            var tmp = Path.Combine(Path.GetTempPath(), $"fr-mutated-{Guid.NewGuid():N}.jsonl");
            try
            {
                // The boundary given here is the most specific one the caller has named, so it
                // decides the tripwire too: this temp file is a real tape on a real disk, and a
                // probe run is no reason for a credential to get written to one.
                _recording.Write(tmp, boundary);
                return Invariants.CheckInvariants(tmp, index, body, invariants, boundary, probe: true);
            }
            finally
            {
                try { File.Delete(tmp); } catch { /* best effort */ }
            }
        }
    }

    public sealed class EffectHandle
    {
        private readonly Dictionary<string, object?> _ev;
        private readonly CallHandle _owner;
        internal EffectHandle(Dictionary<string, object?> ev, CallHandle owner) { _ev = ev; _owner = owner; }

        public object? Result
        {
            get => _ev.GetValueOrNull("res");
            set { _ev.Remove("err"); _ev["res"] = Serial.ToJsonable(value); _owner.Dirty(); }
        }

        /// <summary>Replace the answer with a raised exception: an instance, or (type, args).</summary>
        public void SetError(string type, IReadOnlyList<object?> args)
        {
            _ev.Remove("res");
            _ev["err"] = new Dictionary<string, object?>
            {
                ["type"] = type,
                ["repr"] = $"{type}({string.Join(", ", args.Select(a => Json.Stringify(Serial.ToJsonable(a))))})",
                ["args"] = args.Select(a => Serial.ToJsonable(a)).ToList(),
            };
            _owner.Dirty();
        }

        public void SetError(Exception exc) => SetError(exc.GetType().Name, new object?[] { exc.Message });
    }

    public sealed class ReadHandle
    {
        private readonly Dictionary<string, object?> _ev;
        private readonly CallHandle _owner;
        internal ReadHandle(Dictionary<string, object?> ev, CallHandle owner) { _ev = ev; _owner = owner; }

        /// <summary>Set the read's answer. A list yields many snapshots, a single value one; plain
        /// values are understood as document DATA and wrapped in snapshot shape.</summary>
        public object? Result
        {
            get => _ev.GetValueOrNull("res");
            set
            {
                if (value is System.Collections.IEnumerable en && !(value is string) && !(value is IDictionary<string, object?>))
                {
                    var i = 0;
                    _ev["res"] = en.Cast<object?>().Select(x => (object?)SnapWrap(x, i++)).ToList();
                }
                else _ev["res"] = SnapWrap(value, 0);
                _owner.Dirty();
            }
        }

        private static Dictionary<string, object?> SnapWrap(object? item, int i)
        {
            if (item is IDictionary<string, object?> m)
            {
                var keys = new HashSet<string>(m.Keys);
                if (keys.IsSubsetOf(new[] { "id", "exists", "data" }) && m.ContainsKey("data"))
                    return Serial.SnapshotJsonable(
                        m.GetValueOrNull("id") as string ?? $"row{i}",
                        !(m.GetValueOrNull("exists") is bool b) || b,
                        m["data"]);
                return Serial.SnapshotJsonable($"row{i}", true, item);
            }
            return Serial.SnapshotJsonable($"row{i}", true, item);
        }
    }

    public sealed class RandHandle
    {
        private readonly Dictionary<string, object?> _ev;
        private readonly CallHandle _owner;
        internal RandHandle(Dictionary<string, object?> ev, CallHandle owner) { _ev = ev; _owner = owner; }

        public IReadOnlyList<long> Idx
        {
            get => (_ev.GetValueOrNull("idx") as IEnumerable<object?> ?? Enumerable.Empty<object?>())
                .Select(x => Convert.ToInt64(x)).ToList();
            set
            {
                var idx = value.Select(i => (long)i).ToList();
                if (idx.Any(i => i < 0)) throw new ArgumentException($"idx must be non-negative positions, got [{string.Join(",", idx)}]");
                _ev["idx"] = idx.Cast<object?>().ToList();
                _owner.Dirty();
            }
        }
    }

    public sealed class ClockHandleEdit
    {
        private readonly List<Dictionary<string, object?>> _evs;
        private readonly CallHandle _owner;
        internal ClockHandleEdit(List<Dictionary<string, object?>> evs, CallHandle owner) { _evs = evs; _owner = owner; }

        public IReadOnlyList<string> Times => _evs.Select(e => e.GetValueOrNull("v") as string ?? "").ToList();

        public void SetTimes(IReadOnlyList<string> values)
        {
            if (values.Count != _evs.Count)
                throw new ArgumentException($"{_evs.Count} clock read(s) recorded, {values.Count} value(s) given");
            for (var i = 0; i < _evs.Count; i++) _evs[i]["v"] = values[i];
            _owner.Dirty();
        }

        /// <summary>Time runs backwards.</summary>
        public void Reverse() => SetTimes(Times.Reverse().ToList());
    }

    // --- the span tree: a tape, read top-down ---------------------------------------------

    public sealed class SpanNode
    {
        public string? Name;
        public long? Sid;
        public string Phase = "";
        public Dictionary<string, object?> Data = new Dictionary<string, object?>();
        public string? Outcome;
        public List<SpanNode> Children = new List<SpanNode>();
        public List<Dictionary<string, object?>> Events = new List<Dictionary<string, object?>>();
    }

    internal static class SpanTree
    {
        public static SpanNode Build(Dictionary<string, object?> rec)
        {
            var root = new SpanNode
            {
                Name = rec.GetValueOrNull("fn") as string,
                Phase = "call",
                Data = rec.GetValueOrNull("kwargs") as Dictionary<string, object?> ?? new Dictionary<string, object?>(),
                Outcome = rec.GetValueOrNull("error") != null ? "error" : "ok",
            };
            var stack = new List<SpanNode> { root };

            var events = rec.GetValueOrNull("events") as IEnumerable<object?> ?? Enumerable.Empty<object?>();
            foreach (var evObj in events)
            {
                if (!(evObj is Dictionary<string, object?> ev)) continue;
                if ((ev.GetValueOrNull("k") as string) != "sem")
                {
                    stack[stack.Count - 1].Events.Add(ev);
                    continue;
                }
                var phase = ev.GetValueOrNull("phase") as string;
                var node = new SpanNode
                {
                    Name = ev.GetValueOrNull("name") as string,
                    Sid = ev.GetValueOrNull("sid") is long s ? s : (long?)null,
                    Phase = phase ?? "",
                    Data = ev.GetValueOrNull("data") as Dictionary<string, object?> ?? new Dictionary<string, object?>(),
                };
                if (phase == "point") stack[stack.Count - 1].Children.Add(node);
                else if (phase == "begin")
                {
                    node.Phase = "span";
                    stack[stack.Count - 1].Children.Add(node);
                    stack.Add(node);
                }
                else if (phase == "end" && stack.Count > 1)
                {
                    var top = stack[stack.Count - 1];
                    top.Outcome = ev.GetValueOrNull("outcome") as string;
                    stack.RemoveAt(stack.Count - 1);
                }
            }
            return root;
        }

        private static string OutcomeLabel(string? outcome) => outcome switch
        {
            "ok" => "ok",
            "error" => "ERROR",
            null => "open",
            _ => outcome,
        };

        private static string Tally(List<Dictionary<string, object?>> events)
        {
            var counts = new Dictionary<string, int>();
            foreach (var e in events)
            {
                var k = e.GetValueOrNull("k") as string ?? "?";
                counts[k] = counts.TryGetValue(k, out var c) ? c + 1 : 1;
            }
            return string.Join(", ", counts.OrderBy(kv => kv.Key, StringComparer.Ordinal).Select(kv => $"{kv.Value} {kv.Key}"));
        }

        private static string Kv(Dictionary<string, object?> data) =>
            string.Join(", ", data.Select(kv => $"{kv.Key}={Serial.Short(kv.Value)}"));

        public static string Render(SpanNode tree)
        {
            var lines = new List<string>();
            Walk(tree, 0, lines);
            return string.Join("\n", lines);
        }

        private static void Walk(SpanNode node, int depth, List<string> lines)
        {
            var pad = new string(' ', depth * 2);
            if (node.Phase == "point")
            {
                var data = Kv(node.Data);
                lines.Add($"{pad}- {node.Name}" + (data.Length > 0 ? $"  {data}" : ""));
                return;
            }
            var head = $"{pad}{node.Name}  {OutcomeLabel(node.Outcome)}";
            var tally = Tally(node.Events);
            lines.Add(head + (tally.Length > 0 ? $"  ({tally})" : ""));
            foreach (var child in node.Children) Walk(child, depth + 1, lines);
        }
    }
}
