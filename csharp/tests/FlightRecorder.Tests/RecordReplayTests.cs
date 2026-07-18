using System.Collections.Generic;
using FlightRecorder;
using FlightRecorder.Spec;
using FlightRecorder.Toy;
using Xunit;

namespace FlightRecorder.Tests
{
    public class RecordReplayTests
    {
        [Fact]
        public void RecordedTapeIsConformant()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Greet(store, "alice"));
            var violations = Validate.ValidateTape(System.IO.File.ReadAllText(path));
            Assert.True(violations.Count == 0, string.Join("\n", violations));
        }

        [Fact]
        public void GreetReplaysBitForBit()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Greet(store, "alice"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "greet");

            var store = TestSupport.WrapStore();
            var report = Replay.Call(call, kw => ToyTools.Greet(store, (string)kw["user"]!), b);

            Assert.True(report.Ok, Replay.FormatReport(0, report));
            Assert.Null(report.Divergence);
        }

        [Fact]
        public void SignupReplaysWithRedactionOnBothSides()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Signup(store, "a@b.c", "hunter2"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "signup");

            var store = TestSupport.WrapStore();
            var report = Replay.Call(call, kw =>
                ToyTools.Signup(store, (string)kw["email"]!, (string)kw["password"]!), b);

            Assert.True(report.Ok, Replay.FormatReport(0, report));
        }

        [Fact]
        public void ARaisedEffectErrorReplaysAsTheSameError()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store =>
            {
                try { ToyTools.Explode(store, "ghost"); } catch (ToyError) { }
            });
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "explode");

            var store = TestSupport.WrapStore();
            // The tool raises; Replay.Call catches it as the call's error, exactly as recording did.
            var report = Replay.Call(call, kw => ToyTools.Explode(store, (string)kw["user"]!), b);

            Assert.True(report.ErrorMatch, Replay.FormatReport(0, report));
            Assert.Equal("ToyError: no such key: ghost", report.Error);
            Assert.Equal("ToyError: no such key: ghost", call["error"]);
        }

        // A tool whose secret has no field name anywhere: the token is a positional argument to the
        // store, it is interpolated into the key, and it is quoted mid-sentence in a value and in the
        // result. `MaskFields` cannot see any of that. Only a value-level sweep can.
        private const string Token = "sk-live-4242";

        private static object? Fetch(IStore store, string user, string token) =>
            Recorder.Record("fetch", new { user }, () =>
            {
                var doc = store.Get(user);
                store.Set($"seen:{user}:{token}", new Dictionary<string, object?>
                {
                    ["note"] = $"authorized with {token}",
                });
                return new Dictionary<string, object?> { ["name"] = doc.Name, ["trace"] = $"used {token}" };
            });

        private static Boundary ScrubbingBoundary() =>
            TestSupport.ToyBoundary().Scrubbing("sk-live-[A-Za-z0-9]+");

        [Fact]
        public void ScrubMasksASecretNoFieldNameCouldReach()
        {
            var b = ScrubbingBoundary();
            var path = TestSupport.RecordToTape(b, store => Fetch(store, "alice", Token));
            var text = System.IO.File.ReadAllText(path);

            Assert.DoesNotContain(Token, text);
            Assert.Contains(Serial.Redacted, text);
            // And the tape is still a tape — masked, not mangled.
            var violations = Validate.ValidateTape(text);
            Assert.True(violations.Count == 0, string.Join("\n", violations));
        }

        [Fact]
        public void AScrubbedRecordingStillReplays()
        {
            // The idempotence claim, exercised rather than asserted in prose: the tape holds masked
            // arguments, replay re-derives the real ones and scrubs them the same way, and the two
            // sides meet. A scrub applied on only one side would diverge here.
            var b = ScrubbingBoundary();
            var path = TestSupport.RecordToTape(b, store => Fetch(store, "alice", Token));
            var call = Replay.PickCall(Replay.LoadTape(path), fn: "fetch");

            var store = TestSupport.WrapStore();
            var report = Replay.Call(call, kw => Fetch(store, (string)kw["user"]!, Token), b);

            Assert.True(report.Ok, Replay.FormatReport(0, report));
            Assert.Null(report.Divergence);
        }

        [Fact]
        public void AThrowingScrubMasksAndTheCallStillRuns()
        {
            // Recording must never be the reason a call fails, and a mask that crashes must fail
            // towards masked, never towards leaked.
            var b = TestSupport.ToyBoundary();
            b.Scrub = _ => throw new System.InvalidOperationException("boom");

            object? result = null;
            var path = TestSupport.RecordToTape(b, store => result = Fetch(store, "alice", Token));

            var returned = Assert.IsType<Dictionary<string, object?>>(result);
            Assert.Equal($"used {Token}", returned["trace"]); // the app got its real value
            var text = System.IO.File.ReadAllText(path);
            Assert.DoesNotContain(Token, text);               // the tape did not
        }

        [Fact]
        public void ScrubComposesWithRedactRatherThanReplacingIt()
        {
            // signup masks `password` by name; the scrub sweeps the token by value. Both survive the
            // round trip, so adding one layer did not quietly disable the other.
            var b = ScrubbingBoundary();
            var path = TestSupport.RecordToTape(b, store =>
            {
                ToyTools.Signup(store, "a@b.c", "hunter2");
                Fetch(store, "alice", Token);
            });
            var text = System.IO.File.ReadAllText(path);
            Assert.DoesNotContain("hunter2", text);
            Assert.DoesNotContain(Token, text);

            var tape = Replay.LoadTape(path);
            var store2 = TestSupport.WrapStore();
            var signup = Replay.Call(Replay.PickCall(tape, fn: "signup"),
                kw => ToyTools.Signup(store2, (string)kw["email"]!, "hunter2"), b);
            Assert.True(signup.Ok, Replay.FormatReport(0, signup));
        }

        [Fact]
        public void AskingADifferentQuestionDiverges()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Greet(store, "alice"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "greet");

            var store = TestSupport.WrapStore();
            // The recording answered store.Get("alice"); asking for "bob" is a different question.
            var report = Replay.Call(call, _ => ToyTools.Greet(store, "bob"), b);

            Assert.NotNull(report.Divergence);
            Assert.False(report.Ok);
        }

        [Fact]
        public void StoppingEarlyIsCaughtAsUnconsumed()
        {
            // A hand-built call with two effects; a body that only asks the first stopped early.
            var b = new Boundary();
            var call = new Dictionary<string, object?>
            {
                ["ev"] = "call", ["seq"] = 1L, ["fn"] = "twostep", ["kwargs"] = new Dictionary<string, object?>(),
                ["events"] = new List<object?>
                {
                    new Dictionary<string, object?> { ["k"] = "fx", ["fn"] = "s.a", ["args"] = new List<object?>(), ["kwargs"] = new Dictionary<string, object?>(), ["res"] = 1L },
                    new Dictionary<string, object?> { ["k"] = "fx", ["fn"] = "s.b", ["args"] = new List<object?>(), ["kwargs"] = new Dictionary<string, object?>(), ["res"] = 2L },
                },
                ["result"] = null, ["error"] = null,
                ["ts"] = "2026-07-17T10:00:00+02:00", ["ms"] = 1.0,
            };
            var report = Replay.Call(call, _ =>
            {
                Recorder.Effect("s.a", System.Array.Empty<object?>(), () => 1L); // asks only the first
                return (object?)null;
            }, b);

            Assert.NotNull(report.Divergence);
            Assert.Contains("stopped asking", report.Divergence!.Message);
        }
    }
}
