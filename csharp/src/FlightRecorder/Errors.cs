using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;

namespace FlightRecorder
{
    /// <summary>The code asked the world a different question than the tape holds an answer to.
    ///
    /// Not an error in the ordinary sense: it is the precise point at which the code's behaviour
    /// changed. A tape is a complete record of one execution, so a replay that diverges from it
    /// has, by definition, stopped being that execution — and this says exactly where.</summary>
    public sealed class ReplayDivergence : Exception
    {
        public ReplayDivergence(string message) : base(message) { }
    }

    /// <summary>A mutated tape cannot answer the question the code now asks.
    ///
    /// Distinct from a divergence: nothing is wrong with the code. You edited the tape to visit a
    /// world that never happened, and the edit is incomplete — the recorded answer no longer fits
    /// the question. The fix is to the tape, not to the program.</summary>
    public sealed class ProbeUnanswerable : Exception
    {
        public ProbeUnanswerable(string message) : base(message) { }
    }

    /// <summary>A forbid pattern matched the line the recorder was about to write. Recording is
    /// aborted and nothing is written — not to the file, not to a sink. The message names the
    /// pattern, never the value it caught.</summary>
    public sealed class ForbiddenValue : Exception
    {
        public ForbiddenValue(string message) : base(message) { }
    }

    /// <summary>The one check every artifact this library puts on disk has to pass.
    ///
    /// <see cref="Boundary.Forbid"/> is a property of a RECORDING, not of a file: "this execution
    /// left no credential behind". The tape honoured that from the start and nothing else did —
    /// so a run whose tape was masked, asserted clean and shipped could still have dropped the
    /// same secret in the trace sidecar beside it, or had one edited back in and re-saved. Those
    /// are files on disk like any other, and a credential lands just as hard on them.
    ///
    /// A boundary that declares no tripwire pays one branch and never serializes anything on this
    /// account — which is every boundary that existed before <see cref="Boundary.Forbid"/> did.
    /// Callers check <c>Count &gt; 0</c> before rendering the line, so "free when unused" is real
    /// and not merely fast.</summary>
    internal static class Tripwire
    {
        public static void Guard(string line, IReadOnlyList<Regex>? patterns, string what)
        {
            if (patterns == null || patterns.Count == 0) return;
            var hit = Serial.ForbiddenHit(line, patterns);
            if (hit != null)
                // The message names the RULE and never the match: it ends up in logs and stack
                // traces, and a tripwire that quotes the secret it caught has become the leak it
                // was there to prevent.
                throw new ForbiddenValue(
                    $"{what} matches a forbidden pattern (/{hit}/) after redaction — nothing was " +
                    "written. A value that must never reach disk was about to: name the field in " +
                    "Boundary.Redact, or widen a rule that has stopped matching, and try again.");
        }
    }

    /// <summary>The generic revival of a recorded effect error whose type the boundary did not
    /// teach replay to rebuild. Its message carries the recorded rendering.</summary>
    public sealed class ReplayedEffectError : Exception
    {
        public ReplayedEffectError(string message) : base(message) { }
    }
}
