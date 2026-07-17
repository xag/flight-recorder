using System;

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

    /// <summary>The generic revival of a recorded effect error whose type the boundary did not
    /// teach replay to rebuild. Its message carries the recorded rendering.</summary>
    public sealed class ReplayedEffectError : Exception
    {
        public ReplayedEffectError(string message) : base(message) { }
    }
}
