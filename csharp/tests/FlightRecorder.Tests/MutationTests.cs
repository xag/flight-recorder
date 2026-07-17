using System;
using System.Collections.Generic;
using FlightRecorder;
using FlightRecorder.Toy;
using Xunit;

namespace FlightRecorder.Tests
{
    public class MutationTests
    {
        private static string RecordGreet()
        {
            var b = TestSupport.ToyBoundary();
            return TestSupport.RecordToTape(b, store => ToyTools.Greet(store, "alice"));
        }

        [Fact]
        public void MutatingAnEffectAnswerAndCheckingAnInvariant()
        {
            var rec = Recording.Load(RecordGreet());
            var call = rec.Call(0);

            // The store never actually answered this — an edit to the tape, not the database.
            call.Effect("Get").Result = new Doc { Name = "Zonk", X = 9 };
            Assert.True(call.Record.ContainsKey("probe")); // a mutated call is a probe now

            var store = TestSupport.WrapStore();
            var namePresent = Invariants.Invariant("greeting has a name", v =>
            {
                var r = (IReadOnlyDictionary<string, object?>)v.Result!;
                if (string.IsNullOrEmpty(r["name"] as string)) throw new Exception("no name in the greeting");
            });

            var report = call.Check(kw => ToyTools.Greet(store, (string)kw["user"]!),
                new[] { namePresent }, TestSupport.ToyBoundary());

            Assert.True(report.Ok, Invariants.FormatReport(report));
            var result = (IReadOnlyDictionary<string, object?>)report.Replay.Result!;
            Assert.Equal("Zonk", result["name"]); // the real code ran against the edited world
        }

        [Fact]
        public void AViolatedInvariantIsReported()
        {
            var rec = Recording.Load(RecordGreet());
            var call = rec.Call(0);
            call.Effect("Get").Result = new Doc { Name = "Zonk", X = 9 };

            var store = TestSupport.WrapStore();
            var mustBeAlice = Invariants.Invariant("stored user is Alice", v =>
            {
                var r = (IReadOnlyDictionary<string, object?>)v.Result!;
                var name = r["name"] as string;
                if (name != "Alice") throw new Exception($"expected Alice, got {name}");
            });

            var report = call.Check(kw => ToyTools.Greet(store, (string)kw["user"]!),
                new[] { mustBeAlice }, TestSupport.ToyBoundary());

            Assert.False(report.Ok);
            Assert.NotEmpty(report.Violations);
            Assert.Contains("got Zonk", report.Violations[0].Message);
        }

        [Fact]
        public void SavedMutationCarriesProbeAndStillValidates()
        {
            var rec = Recording.Load(RecordGreet());
            rec.Call(0).Effect("Get").Result = new Doc { Name = "Zonk", X = 9 };

            var outPath = System.IO.Path.Combine(TestSupport.TempDir(), "empty-corpus.jsonl");
            rec.Save(outPath);

            var text = System.IO.File.ReadAllText(outPath);
            Assert.Empty(FlightRecorder.Spec.Validate.ValidateTape(text));
            Assert.Contains("\"probe\":true", text); // cannot be mistaken for a strict regression pin
        }

        [Fact]
        public void SelectingAMissingEventFailsHelpfully()
        {
            var rec = Recording.Load(RecordGreet());
            var ex = Assert.Throws<System.Collections.Generic.KeyNotFoundException>(() => rec.Call(0).Read());
            Assert.Contains("no read", ex.Message);
        }
    }
}
