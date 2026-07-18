// The code under trace.
//
// Deliberately dull, and deliberately buggy in one specific way: `Deal` produces an answer that
// is entirely self-consistent — the counts agree, nothing throws, `done` is honestly reported —
// and is still wrong, because `level` went to zero and quietly excluded the whole corpus. That
// bug is invisible from the outside and invisible to a replay, which reproduces it faithfully
// forever. It is visible in exactly one place: the value `level` held on the line that used it.
//
// This file is compiled into the test assembly AND read from disk and recompiled by the tracer.
// Those are two different assemblies holding two different `TracedToy` types, which is fine and
// is the whole design — see Tracer.cs. Keep it free of xunit and of anything that would drag
// another source file along.

using System.Collections.Generic;

namespace FlightRecorder.Tests
{
    public static class TracedToy
    {
        /// <summary>Deal `want` words from a corpus, filtered by a difficulty level.</summary>
        public static Dictionary<string, object?> Deal(List<string> corpus, int want, int seen)
        {
            var level = Level(seen);
            var eligible = new List<string>();
            foreach (var word in corpus)
            {
                if (word.Length <= level) eligible.Add(word);
            }

            var deck = new List<string>();
            foreach (var word in eligible)
            {
                if (deck.Count >= want) break;
                deck.Add(word);
            }

            var done = deck.Count < want;
            return new Dictionary<string, object?>
            {
                ["deck"] = deck,
                ["corpus"] = corpus.Count,
                ["done"] = done,
            };
        }

        /// <summary>
        /// The bug. Past a hundred words seen this returns 0, and a level of 0 admits no word at
        /// all — so a well-practised user is dealt an empty deck and told, truthfully by the
        /// code's own arithmetic, that the corpus is exhausted.
        /// </summary>
        private static int Level(int seen)
        {
            if (seen > 100) return 0;
            return 8;
        }

        /// <summary>A method that throws, for the trace-up-to-the-throw case.</summary>
        public static int Boom(int n)
        {
            var doubled = n * 2;
            if (doubled > 0) throw new System.InvalidOperationException("boom at " + doubled);
            return doubled;
        }

        /// <summary>
        /// A credential that exists ONLY as a local: it is minted inside the method, never
        /// argued in, never returned, never handed to the boundary. Nothing on the tape can
        /// carry it — and a trace of this method carries it on every line after the first.
        /// </summary>
        public static int Charge(int cents)
        {
            var key = "sk-live-" + (cents * 7);
            return key.Length + cents;
        }

        /// <summary>Locals of several shapes, to prove the encoder records data and not reprs.</summary>
        public static string Shapes(int n)
        {
            var count = n + 1;
            var label = "n=" + n;
            var flag = count > 2;
            var items = new List<int> { n, count };
            return label + ":" + flag + ":" + items.Count;
        }
    }
}
