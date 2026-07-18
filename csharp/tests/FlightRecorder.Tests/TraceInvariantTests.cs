// The trace, reaching an invariant.
//
// Python, Node and Go all hand an invariant the replayed execution's internals so a claim can be
// made about a variable rather than only about a result. These pin the .NET wiring for the same
// thing: what a replay report carries, what a CallView exposes, and — the part that is easy to
// get quietly wrong — that an UNtraced run yields an empty trace rather than a null one.
//
// That last property is not pedantry. A null trace makes `t.Trace.Values("level")` throw, and a
// suite that catches the throw reads it as "invariant violated" for the wrong reason. An empty
// trace makes the same claim FAIL, honestly, saying the variable was never observed — which is
// true, and actionable, and not the same as passing.

using System;
using System.Collections.Generic;
using System.Linq;
using FlightRecorder;
using FlightRecorder.Toy;
using Xunit;

namespace FlightRecorder.Tests
{
    public class TraceInvariantTests
    {
        private static string RecordGreet()
        {
            var b = TestSupport.ToyBoundary();
            return TestSupport.RecordToTape(b, store => ToyTools.Greet(store, "alice"));
        }

        [Fact]
        public void AnUntracedReplayCarriesAnEmptyTraceNotANullOne()
        {
            var rec = Recording.Load(RecordGreet());
            var call = rec.Call(0);
            var store = TestSupport.WrapStore();

            var report = call.Check(kw => ToyTools.Greet(store, (string)kw["user"]!),
                Array.Empty<Invariant>(), TestSupport.ToyBoundary());

            Assert.NotNull(report.Replay.Trace);
            Assert.Empty(report.Replay.Trace.Names());
            Assert.Empty(report.Replay.Trace.Values("level"));
        }

        [Fact]
        public void AClaimAboutAnUnobservedVariableFailsRatherThanPassingVacuously()
        {
            var rec = Recording.Load(RecordGreet());
            var call = rec.Call(0);
            var store = TestSupport.WrapStore();

            // Nothing was traced, so this claim cannot be satisfied — and it must not be silently
            // satisfied either. An invariant that passes because it looked at nothing is worse
            // than one that fails.
            var levelPositive = Invariants.Invariant("level never excludes the whole corpus", v =>
            {
                var seen = v.Trace.Values("level");
                if (seen.Count == 0) throw new Exception("level was never observed — nothing was traced");
                foreach (var o in seen)
                    if (Convert.ToInt64(o.Value) <= 0) throw new Exception($"level={o.Value} at {o.At}");
            });

            var report = call.Check(kw => ToyTools.Greet(store, (string)kw["user"]!),
                new[] { levelPositive }, TestSupport.ToyBoundary());

            Assert.False(report.Ok);
            Assert.Contains(report.Violations, x => x.Message.Contains("never observed"));
        }

        [Fact]
        public void TheTraceReachesTheInvariantWhenTheBodyRunsInstrumented()
        {
            var rec = Recording.Load(RecordGreet());
            var call = rec.Call(0);
            var store = TestSupport.WrapStore();

            // Arm the sink the way a traced run does, and record one frame from inside the body.
            // The body still answers the tape through the real tool; the traced helper only proves
            // that whatever the sink saw during THIS call reaches the invariant that judges it.
            var sink = new TraceSink();
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try
            {
                var sawTrace = Invariants.Invariant("the invariant can read the execution's internals", v =>
                {
                    if (!v.Trace.Names().Contains("doubled"))
                        throw new Exception("the trace never reached the invariant: " +
                                            string.Join(",", v.Trace.Names()));
                });

                var report = call.Check(kw =>
                {
                    var frame = TraceHook.Enter("Helper.Double", TraceHook.At("TraceInvariantTests.cs", 1),
                        Array.Empty<string>(), Array.Empty<object>());
                    try
                    {
                        var doubled = 21 * 2;
                        TraceHook.Line(frame, "Helper.Double", TraceHook.At("TraceInvariantTests.cs", 2),
                            new[] { "doubled" }, new object[] { doubled });
                    }
                    finally { TraceHook.Exit(frame); }

                    return ToyTools.Greet(store, (string)kw["user"]!);
                }, new[] { sawTrace }, TestSupport.ToyBoundary());

                Assert.True(report.Ok, Invariants.FormatReport(report));
                Assert.Contains("doubled", report.Replay.Trace.Names());
            }
            finally { TraceHook.Sink = previous; }
        }

        [Fact]
        public void OneCallsTraceIsItsOwnAndNotTheWholeSessions()
        {
            var rec = Recording.Load(RecordGreet());
            var store = TestSupport.WrapStore();

            var sink = new TraceSink();
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try
            {
                // A frame that ran BEFORE the call under judgement. Its observations are on the
                // same sink, and must not appear in that call's report.
                var stale = TraceHook.Enter("Before.Call", TraceHook.At("x.cs", 1),
                    new[] { "stale" }, new object[] { "earlier" });
                TraceHook.Exit(stale);

                var report = rec.Call(0).Check(kw => ToyTools.Greet(store, (string)kw["user"]!),
                    Array.Empty<Invariant>(), TestSupport.ToyBoundary());

                Assert.DoesNotContain("stale", report.Replay.Trace.Names());
            }
            finally { TraceHook.Sink = previous; }
        }
    }
}
