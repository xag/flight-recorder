// Replay — resurrection, not re-enactment.
//
// The recorded answers are fed back and the REAL code re-runs the original execution: no
// network, no database, no waiting for the bug to happen again. The same Clock/Random handles
// and Wrap-ped clients the recording used simply source their answers from the tape instead of
// the world (see Hook in Record.cs).
//
// Two jobs, equally important:
//   1. Answer — pop the recorded answers in order and hand them back.
//   2. Refuse to answer the wrong question. If the code asks a different effect, in a different
//      order, or with different arguments, that IS the finding: the exact point where behaviour
//      changed. A replay that silently answered anyway would look like it worked, which is worse
//      than useless. A THIRD signal, "the code stopped asking", is the sneaky one: everything
//      matched, the code just quietly did less work — and the unconsumed answers are the evidence.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;

namespace FlightRecorder
{
    /// <summary>A parsed tape: the session header and the call lines.</summary>
    public sealed class Tape
    {
        public Dictionary<string, object?> Header { get; }
        public IReadOnlyList<Dictionary<string, object?>> Calls { get; }

        public Tape(Dictionary<string, object?> header, IReadOnlyList<Dictionary<string, object?>> calls)
        {
            Header = header;
            Calls = calls;
        }
    }

    public sealed class ReplayReport
    {
        public bool Ok { get; internal set; }
        public object? Result { get; internal set; }
        public string? Error { get; internal set; }
        public Exception? Divergence { get; internal set; }
        public object? RecordedResult { get; internal set; }
        public object? ReplayedResult { get; internal set; }
        public bool ResultMatch { get; internal set; }
        public bool ErrorMatch { get; internal set; }
        public int Unconsumed { get; internal set; }
        public IReadOnlyList<(string Name, string Phase)> SemsRecorded { get; internal set; } = Array.Empty<(string, string)>();
        public IReadOnlyList<(string Name, string Phase)> SemsReplayed { get; internal set; } = Array.Empty<(string, string)>();
        public string? SemDivergence { get; internal set; }
        public bool SemStrict { get; internal set; }
    }

    public sealed class Feed
    {
        private readonly IReadOnlyList<Dictionary<string, object?>> _events;
        private int _i;
        private readonly bool _probe;
        private readonly Boundary _boundary;

        public Feed(IReadOnlyList<Dictionary<string, object?>> events, bool probe, Boundary boundary)
        {
            _events = events;
            _probe = probe;
            _boundary = boundary;
        }

        public int Index => _i;
        public int Count => _events.Count;
        public bool Exhausted => _i >= _events.Count;

        private ReplayDivergence Diverge(string want, string got) =>
            new ReplayDivergence(
                $"the code asked a different question than the recording holds, at event {_i} of {_events.Count}\n" +
                $"  recorded: {got}\n  replayed: {want}");

        /// <summary>Step over recorded `sem` events — they are never answers, only the app's own
        /// testimony. Advancing past them keeps "every event consumed" meaning "the code asked the
        /// recording everything it holds", so instrumenting an app never costs a false failure.</summary>
        public void SkipSems()
        {
            while (_i < _events.Count && (string?)_events[_i].GetValueOrNull("k") == "sem") _i += 1;
        }

        public Dictionary<string, object?> PopExpect(string kind, string? label = null)
        {
            SkipSems();
            if (Exhausted)
                throw Diverge($"{kind}{(label != null ? " " + label : "")}",
                    "(nothing — the recording had no more answers to give)");
            var ev = _events[_i];
            var k = (string?)ev.GetValueOrNull("k");
            if (k != kind)
                throw Diverge($"{kind}{(label != null ? " " + label : "")}",
                    $"{k}{(ev.GetValueOrNull("fn") is string efn ? " " + efn : "")}");
            _i += 1;
            return ev;
        }

        /// <summary>Answer an effect, having first checked it is the effect being asked, with the
        /// arguments it was asked with (arguments are part of the question — except under probe,
        /// where a mutated upstream answer legitimately changes every downstream question).</summary>
        public object? AnswerEffect(string fn, IReadOnlyList<object?> args)
        {
            var ev = PopExpectFx(fn);
            if (!_probe)
            {
                var recorded = Json.Canonical(ev.GetValueOrNull("args") ?? new List<object?>());
                var replayed = Json.Canonical(Serial.RedactJsonable(args, _boundary.RedactRules));
                if (recorded != replayed)
                    throw Diverge($"fx {fn}({replayed})", $"fx {fn}({recorded})");
            }
            if (ev.ContainsKey("err") && ev["err"] is IDictionary<string, object?> err)
                throw _boundary.ReviveError(err);
            return Serial.FromJsonable(ev.GetValueOrNull("res"));
        }

        private Dictionary<string, object?> PopExpectFx(string fn)
        {
            SkipSems();
            if (Exhausted)
                throw Diverge($"fx {fn}", "(nothing — the recording had no more answers to give)");
            var ev = _events[_i];
            var k = (string?)ev.GetValueOrNull("k");
            var efn = ev.GetValueOrNull("fn") as string;
            if (k != "fx" || efn != fn)
                throw Diverge($"fx {fn}", $"{k}{(efn != null ? " " + efn : "")}");
            _i += 1;
            return ev;
        }

        /// <summary>Answer a chain READ from the tape.</summary>
        public IReadOnlyList<Snapshot> AnswerDbRead(string op, string sig)
        {
            var ev = PopExpect("db", op);
            if ((ev.GetValueOrNull("op") as string) != op)
                throw Diverge($"db {op}", $"db {ev.GetValueOrNull("op")}");
            if (!ev.ContainsKey("res"))
                throw Diverge($"db read {op}", $"db write {ev.GetValueOrNull("op")}");
            if (!_probe && (ev.GetValueOrNull("sig") as string) != sig)
                throw Diverge($"db {sig}", $"db {ev.GetValueOrNull("sig")}");
            var res = ev.GetValueOrNull("res");
            if (res is IEnumerable<object?> list)
                return list.Select(Snapshot.FromJsonable).ToList();
            return new List<Snapshot> { Snapshot.FromJsonable(res) };
        }

        /// <summary>Expect a chain WRITE at this point; the write is not performed on replay.</summary>
        public void ExpectDbWrite(string op, string sig)
        {
            var ev = PopExpect("db", op);
            if ((ev.GetValueOrNull("op") as string) != op)
                throw Diverge($"db {op}", $"db {ev.GetValueOrNull("op")}");
            if (!ev.ContainsKey("args"))
                throw Diverge($"db write {op}", $"db read {ev.GetValueOrNull("op")}");
            if (!_probe && (ev.GetValueOrNull("sig") as string) != sig)
                throw Diverge($"db {sig}", $"db {ev.GetValueOrNull("sig")}");
        }

        internal Dictionary<string, object?>? NextUnconsumed => Exhausted ? null : _events[_i];
    }

    public static class Replay
    {
        /// <summary>Read a tape. Tolerates a torn final line, the only corruption possible.</summary>
        public static Tape LoadTape(string pathOrText)
        {
            var text = File.Exists(pathOrText) ? File.ReadAllText(pathOrText) : pathOrText;
            var lines = text.Split('\n').Where(l => l.Trim().Length > 0).ToList();
            var objs = new List<Dictionary<string, object?>>();
            for (var i = 0; i < lines.Count; i++)
            {
                try { objs.Add((Dictionary<string, object?>)Json.Parse(lines[i])!); }
                catch { if (i != lines.Count - 1) throw; } // only the last line may be torn
            }
            var header = objs.FirstOrDefault(o => (string?)o.GetValueOrNull("ev") == "session")
                         ?? throw new InvalidDataException("tape has no session header");
            var version = header.GetValueOrNull("version");
            if (!(version is long v && v == 1))
                throw new InvalidDataException($"unsupported tape version {version}");
            var calls = objs.Where(o => (string?)o.GetValueOrNull("ev") == "call").ToList();
            return new Tape(header, calls);
        }

        /// <summary>Pick one call: by seq, or the first matching fn, or the only one there is.</summary>
        public static Dictionary<string, object?> PickCall(Tape tape, long? seq = null, string? fn = null)
        {
            if (seq != null)
                return tape.Calls.FirstOrDefault(c => c.GetValueOrNull("seq") is long s && s == seq)
                       ?? throw new InvalidOperationException($"no call with seq={seq}");
            if (fn != null)
                return tape.Calls.FirstOrDefault(c => (string?)c.GetValueOrNull("fn") == fn)
                       ?? throw new InvalidOperationException($"no call to {fn}");
            if (tape.Calls.Count != 1)
                throw new InvalidOperationException($"tape has {tape.Calls.Count} calls — pass seq or fn");
            return tape.Calls[0];
        }

        /// <summary>Re-run one recorded call against the real code, synchronously.</summary>
        public static ReplayReport Call(Dictionary<string, object?> call,
            Func<IReadOnlyDictionary<string, object?>, object?> body,
            Boundary? boundary = null, bool probe = false, bool semStrict = false)
        {
            var ctx = Begin(call, boundary, probe);
            object? result = null;
            string? error = null;
            Exception? divergence = null;
            try { result = body(ctx.Kwargs); }
            catch (ReplayDivergence e) { divergence = e; }
            catch (ProbeUnanswerable e) { divergence = e; }
            catch (Exception e) { error = $"{e.GetType().Name}: {e.Message}"; }
            finally { End(); }
            return Finish(ctx, call, boundary, semStrict, result, error, divergence);
        }

        /// <summary>Re-run one recorded call against the real async code.</summary>
        public static async Task<ReplayReport> CallAsync(Dictionary<string, object?> call,
            Func<IReadOnlyDictionary<string, object?>, Task<object?>> body,
            Boundary? boundary = null, bool probe = false, bool semStrict = false)
        {
            var ctx = Begin(call, boundary, probe);
            object? result = null;
            string? error = null;
            Exception? divergence = null;
            try { result = await body(ctx.Kwargs).ConfigureAwait(false); }
            catch (ReplayDivergence e) { divergence = e; }
            catch (ProbeUnanswerable e) { divergence = e; }
            catch (Exception e) { error = $"{e.GetType().Name}: {e.Message}"; }
            finally { End(); }
            return Finish(ctx, call, boundary, semStrict, result, error, divergence);
        }

        private sealed class Ctx
        {
            public Feed Feed = null!;
            public CallBuffer Sems = null!;
            public Dictionary<string, object?> Kwargs = null!;
        }

        private static Ctx Begin(Dictionary<string, object?> call, Boundary? boundary, bool probe)
        {
            var b = boundary ?? new Boundary();
            var events = (call.GetValueOrNull("events") as IEnumerable<object?> ?? Enumerable.Empty<object?>())
                .OfType<Dictionary<string, object?>>().ToList();
            var isProbe = probe || (call.GetValueOrNull("probe") is bool p && p);
            var feed = new Feed(events, isProbe, b);
            var kwargs = Serial.FromJsonable(call.GetValueOrNull("kwargs")) as IDictionary<string, object?>
                         ?? new Dictionary<string, object?>();
            var sems = new CallBuffer();

            Hook.Mode = Mode.Replay;
            Hook.Feed = feed;
            Hook.Sems = sems;
            return new Ctx { Feed = feed, Sems = sems, Kwargs = new Dictionary<string, object?>(kwargs) };
        }

        private static void End()
        {
            Hook.Mode = null;
            Hook.Feed = null;
            Hook.Sems = null;
        }

        private static ReplayReport Finish(Ctx ctx, Dictionary<string, object?> call, Boundary? boundary,
            bool semStrict, object? result, string? error, Exception? divergence)
        {
            var b = boundary ?? new Boundary();

            // Sems trailing the last boundary answer (an outermost span's end, most often) were
            // never reached by a PopExpect; read them so the path is not reported short.
            ctx.Feed.SkipSems();

            var semsRecorded = SemPairs(call.GetValueOrNull("events"));
            var semsReplayed = SemPairs(ctx.Sems.Events.Cast<object?>().ToList());
            var semDiv = SemDivergence(semsRecorded, semsReplayed);

            var report = new ReplayReport
            {
                SemsRecorded = semsRecorded,
                SemsReplayed = semsReplayed,
                SemDivergence = semDiv,
                SemStrict = semStrict,
                Result = result,
                Error = error,
            };

            if (divergence != null)
            {
                report.Divergence = divergence;
                report.Unconsumed = ctx.Feed.Count - ctx.Feed.Index;
                return report;
            }

            var unconsumed = ctx.Feed.Count - ctx.Feed.Index;
            if (unconsumed > 0)
            {
                var next = ctx.Feed.NextUnconsumed;
                report.Divergence = new ReplayDivergence(
                    $"the code stopped asking {unconsumed} question(s) the recording answered — " +
                    $"next unconsumed: {next?.GetValueOrNull("k")}" +
                    $"{(next?.GetValueOrNull("fn") is string nf ? " " + nf : "")}");
                report.Unconsumed = unconsumed;
                return report;
            }

            var replayedResult = error != null ? null
                : Serial.RedactJsonable(Serial.ToJsonable(result), b.RedactRules);
            var recordedResult = call.GetValueOrNull("result");
            report.RecordedResult = recordedResult;
            report.ReplayedResult = replayedResult;
            report.ResultMatch = Json.Canonical(replayedResult) == Json.Canonical(recordedResult);
            report.ErrorMatch = (error ?? null) == (call.GetValueOrNull("error") as string);
            report.Ok = report.ResultMatch && report.ErrorMatch && (!semStrict || semDiv == null);
            return report;
        }

        private static List<(string, string)> SemPairs(object? events)
        {
            var outp = new List<(string, string)>();
            if (events is IEnumerable<object?> list)
                foreach (var e in list)
                    if (e is IDictionary<string, object?> ev && (string?)ev.GetValueOrNull("k") == "sem")
                        outp.Add(((string)(ev.GetValueOrNull("name") ?? ""), (string)(ev.GetValueOrNull("phase") ?? "")));
            return outp;
        }

        private static string? SemDivergence(IReadOnlyList<(string Name, string Phase)> recorded,
            IReadOnlyList<(string Name, string Phase)> replayed)
        {
            string Show((string, string)? p) => p == null ? "nothing" : $"\"{p.Value.Item1}\" {p.Value.Item2}";
            var n = Math.Max(recorded.Count, replayed.Count);
            for (var k = 0; k < n; k++)
            {
                (string, string)? a = k < recorded.Count ? recorded[k] : null;
                (string, string)? b = k < replayed.Count ? replayed[k] : null;
                if (!Equals(a, b))
                    return $"semantic divergence at {k}: recorded {Show(a)}, replayed {Show(b)} — " +
                           "the code's account of what it was doing has changed";
            }
            return null;
        }

        /// <summary>A one-line-plus verdict for a replay, ready to print.</summary>
        public static string FormatReport(int index, ReplayReport report)
        {
            if (report.Divergence != null)
                return $"Replayed call {index}: DIVERGED\n  {report.Divergence.Message}";
            var lines = new List<string>();
            if (report.Ok)
                lines.Add($"Replayed call {index}: MATCH — replay reproduced the recording bit-for-bit");
            else
            {
                lines.Add($"Replayed call {index}: MISMATCH");
                if (!report.ResultMatch)
                    lines.Add($"  result: recorded {Json.Stringify(report.RecordedResult)}, " +
                              $"replayed {Json.Stringify(report.ReplayedResult)}");
                if (!report.ErrorMatch)
                    lines.Add($"  error: recorded {report.Error ?? "null"}");
            }
            if (report.SemDivergence != null)
                lines.Add($"  {report.SemDivergence}" + (report.SemStrict ? "" : " (reported, not failing)"));
            return string.Join("\n", lines);
        }
    }
}
