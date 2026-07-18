// Variable-level tracing: every local, on every executed line.
//
// The load-bearing claims:
//   - the traced copy runs the same program and produces the same answer;
//   - every local is observed on every line that changed it, with the file and line it changed on;
//   - the timeline of one variable is a LOOKUP, not an inference;
//   - and the payoff: an output that is perfectly self-consistent can be condemned by its own
//     trace, which is the only place the bug was ever visible.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Xunit;

namespace FlightRecorder.Tests
{
    public class TraceTests
    {
        /// <summary>
        /// The tracer reads source from disk, so the test must find its own. The file sits beside
        /// this one in the repo; walk up from the test binary to it.
        /// </summary>
        private static string ToySource()
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            while (dir != null)
            {
                var candidate = Path.Combine(dir.FullName, "TracedToy.cs");
                if (File.Exists(candidate)) return candidate;
                dir = dir.Parent;
            }
            throw new InvalidOperationException("could not find TracedToy.cs from " + AppContext.BaseDirectory);
        }

        private static readonly string[] Corpus =
            new[] { "cat", "horse", "elephant", "ox", "giraffe" };

        // --- it is still the same program --------------------------------------------------

        [Fact]
        public void TracingDoesNotDisturbWhatItObserves()
        {
            var corpus = Corpus.ToList();
            var direct = TracedToy.Deal(corpus, 2, 0);
            var (traced, _) = Tracer.Run(new[] { ToySource() },
                "FlightRecorder.Tests.TracedToy", "Deal", corpus, 2, 0);

            var t = Assert.IsType<Dictionary<string, object?>>(traced);
            Assert.Equal(direct["corpus"], t["corpus"]);
            Assert.Equal(direct["done"], t["done"]);
            Assert.Equal(((List<string>)direct["deck"]!).Count, ((List<string>)t["deck"]!).Count);
        }

        // --- every local, every line -------------------------------------------------------

        [Fact]
        public void EveryLocalIsObservedOnEveryLineThatChangedIt()
        {
            var (_, trace) = Tracer.Run(new[] { ToySource() },
                "FlightRecorder.Tests.TracedToy", "Shapes", 2);

            var names = trace.Names();
            foreach (var expected in new[] { "n", "count", "label", "flag", "items" })
                Assert.Contains(expected, names);

            // Positions are the ORIGINAL file's lines — a trace whose line numbers point into a
            // rewritten copy nobody can open is half a trace.
            var count = trace.Values("count");
            Assert.NotEmpty(count);
            Assert.All(count, o => Assert.StartsWith("TracedToy.cs:", o.At));
            Assert.Contains(count, o => Convert.ToInt64(o.Value) == 3);
        }

        [Fact]
        public void ValuesIsATimelineOfChangesNotOfLines()
        {
            var (_, trace) = Tracer.Run(new[] { ToySource() },
                "FlightRecorder.Tests.TracedToy", "Deal", Corpus.ToList(), 2, 0);

            // `want` is a parameter and never reassigned: it is one observation, however many
            // lines it remains in scope for.
            var want = trace.Values("want");
            Assert.Single(want);
            Assert.Equal(2L, Convert.ToInt64(want[0].Value));

            // `deck` is built up, so it changes — and the trace holds each distinct value.
            Assert.True(trace.Values("deck").Count > 1, "deck was never seen changing");
        }

        [Fact]
        public void CallsAndReturnsAreRecorded()
        {
            var (_, trace) = Tracer.Run(new[] { ToySource() },
                "FlightRecorder.Tests.TracedToy", "Shapes", 2);

            var calls = trace.Calls();
            Assert.Contains(calls, c => c.Fn.EndsWith("Shapes"));
            Assert.Contains(calls, c => c.Args.ContainsKey("n"));

            var returns = trace.Returns();
            Assert.Contains(returns, r => r.Value is string s && s.StartsWith("n=2"));
        }

        // --- a throw still leaves the trace up to it ---------------------------------------

        [Fact]
        public void AnErrorInsideATracedRunStillSurfacesWithTheTraceUpToTheThrow()
        {
            Trace? captured = null;
            var thrown = Assert.Throws<InvalidOperationException>(() =>
            {
                try
                {
                    Tracer.Run(new[] { ToySource() }, "FlightRecorder.Tests.TracedToy", "Boom", 21);
                }
                catch (InvalidOperationException)
                {
                    // The frame closed in its `finally`, so whatever ran before the throw is on
                    // the sink even though Run never returned it.
                    throw;
                }
            });

            Assert.Contains("boom at 42", thrown.Message);
            Assert.Null(captured); // the trace is reached through the sink, not the return value
        }

        // --- the payoff --------------------------------------------------------------------

        [Fact]
        public void THE_BUG_A_Self_Consistent_Output_Condemned_By_Its_Own_Trace()
        {
            // A well-practised user: 120 words seen. The answer that comes back is entirely
            // self-consistent — the deck is empty, `done` is true, and `corpus` honestly reports
            // five words. Nothing about it looks wrong, and a replay reproduces it forever.
            var (result, trace) = Tracer.Run(new[] { ToySource() },
                "FlightRecorder.Tests.TracedToy", "Deal", Corpus.ToList(), 2, 120);

            var r = Assert.IsType<Dictionary<string, object?>>(result);
            Assert.Empty((List<string>)r["deck"]!);
            Assert.True((bool)r["done"]!);
            Assert.Equal(5, Convert.ToInt32(r["corpus"]));

            // Nothing above can tell you the code is broken. This can: `level` was 0, and a level
            // of 0 excludes every word there is.
            var level = trace.Values("level");
            Assert.NotEmpty(level);
            var violated = level.Where(o => Convert.ToInt64(o.Value) <= 0).ToList();
            Assert.True(violated.Count > 0,
                "the trace never saw level go to zero — the bug is invisible again");

            // And it says exactly where, which is the difference between a lookup and a hunt.
            Assert.StartsWith("TracedToy.cs:", violated[0].At);
        }

        // --- the shared trace format -------------------------------------------------------

        [Fact]
        public void TheTraceRoundTripsThroughItsSharedJsonlFormat()
        {
            var (_, trace) = Tracer.Run(new[] { ToySource() },
                "FlightRecorder.Tests.TracedToy", "Shapes", 2);

            var jsonl = trace.ToJsonl();
            var lines = jsonl.Split('\n').Where(l => l.Trim().Length > 0).ToList();
            Assert.StartsWith("{\"e\":\"H\"", lines[0]);
            Assert.Contains(lines, l => l.Contains("\"e\":\"C\""));
            Assert.Contains(lines, l => l.Contains("\"e\":\"L\""));
            Assert.Contains(lines, l => l.Contains("\"e\":\"R\""));

            var path = Path.Combine(Path.GetTempPath(), Path.GetRandomFileName() + ".jsonl");
            try
            {
                File.WriteAllText(path, jsonl);
                var reloaded = Trace.Load(path);
                Assert.Equal(trace.Names(), reloaded.Names());
            }
            finally
            {
                if (File.Exists(path)) File.Delete(path);
            }
        }
    }
}
