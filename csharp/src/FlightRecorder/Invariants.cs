// Invariants — the "right?" question, alongside replay's "same?".
//
// A recording asserts "same as before"; an invariant asserts a property that must hold on EVERY
// execution. Invariants consume the shared tape, so they are not language-bound the way record
// and replay are — but a .NET consumer wants to write them in .NET, over the replayed execution
// of its own code, so here they are. An invariant is handed a view of what the code just did on
// replay (its result, its error, the boundary events it consumed, the claims it made) and
// throws when the property is broken. The thrown message becomes the violation.
//
// Under a MUTATED (probe) recording this is exactly a property test over the boundary: the tape
// drives the real code into a world that never happened, and the invariant judges what it did.

using System;
using System.Collections.Generic;
using System.Linq;

namespace FlightRecorder
{
    /// <summary>A named property, asserted by throwing when it does not hold.</summary>
    public sealed class Invariant
    {
        public string Name { get; }
        public Action<CallView> Assert { get; }
        public Invariant(string name, Action<CallView> assert) { Name = name; Assert = assert; }
    }

    /// <summary>What the replayed code did — the surface an invariant asserts over.</summary>
    public sealed class CallView
    {
        public object? Result { get; }
        public string? Error { get; }
        public IReadOnlyDictionary<string, object?> Kwargs { get; }
        public IReadOnlyList<Dictionary<string, object?>> Events { get; }
        public IReadOnlyList<(string Name, string Phase)> Sems { get; }

        internal CallView(object? result, string? error, IReadOnlyDictionary<string, object?> kwargs,
            IReadOnlyList<Dictionary<string, object?>> events, IReadOnlyList<(string, string)> sems)
        {
            Result = result;
            Error = error;
            Kwargs = kwargs;
            Events = events;
            Sems = sems;
        }

        /// <summary>The result revived into <typeparamref name="T"/> via a JSON round-trip.</summary>
        public T ResultAs<T>() => (T)EffectProxy.Coerce(Result, typeof(T))!;
    }

    public sealed class Violation
    {
        public string Invariant { get; }
        public string Message { get; }
        public Violation(string invariant, string message) { Invariant = invariant; Message = message; }
    }

    public sealed class InvariantReport
    {
        public string Fn { get; internal set; } = "";
        public bool Ok { get; internal set; }
        public int Held { get; internal set; }
        public List<Violation> Violations { get; } = new List<Violation>();
        public ReplayReport Replay { get; internal set; } = null!;
    }

    public static class Invariants
    {
        public static Invariant Invariant(string name, Action<CallView> assert) => new Invariant(name, assert);

        public static InvariantReport CheckInvariants(string tapePath, int index,
            Func<IReadOnlyDictionary<string, object?>, object?> body, IEnumerable<Invariant> invariants,
            Boundary? boundary = null, bool probe = false)
        {
            var tape = Replay.LoadTape(tapePath);
            return CheckInvariants(tape, index, body, invariants, boundary, probe);
        }

        public static InvariantReport CheckInvariants(Tape tape, int index,
            Func<IReadOnlyDictionary<string, object?>, object?> body, IEnumerable<Invariant> invariants,
            Boundary? boundary = null, bool probe = false)
        {
            var call = tape.Calls[index];
            var report = Replay.Call(call, body, boundary, probe);

            var kwargs = Serial.FromJsonable(call.GetValueOrNull("kwargs")) as IDictionary<string, object?>
                         ?? new Dictionary<string, object?>();
            var events = (call.GetValueOrNull("events") as IEnumerable<object?> ?? Enumerable.Empty<object?>())
                .OfType<Dictionary<string, object?>>().ToList();
            var view = new CallView(report.Result, report.Error,
                new Dictionary<string, object?>(kwargs), events, report.SemsReplayed);

            var outp = new InvariantReport
            {
                Fn = call.GetValueOrNull("fn") as string ?? "",
                Replay = report,
            };

            foreach (var inv in invariants)
            {
                try { inv.Assert(view); outp.Held += 1; }
                catch (Exception e) { outp.Violations.Add(new Violation(inv.Name, e.Message)); }
            }

            // A probe's replay result is not expected to match (the world was edited), so its
            // verdict rests on the invariants alone plus whether the tape could answer at all.
            // A strict (non-probe) check also requires the replay itself to reproduce.
            outp.Ok = outp.Violations.Count == 0 && (probe ? report.Divergence == null : report.Ok);
            return outp;
        }

        public static string FormatReport(InvariantReport report)
        {
            if (report.Replay.Divergence != null)
                return $"{report.Fn}: could not check — {report.Replay.Divergence.Message}";
            if (report.Ok)
                return $"{report.Fn}: {report.Held} invariant(s) held";
            var lines = new List<string> { $"{report.Fn}: {report.Violations.Count} violation(s)" };
            foreach (var v in report.Violations) lines.Add($"  - {v.Invariant}: {v.Message}");
            return string.Join("\n", lines);
        }
    }
}
