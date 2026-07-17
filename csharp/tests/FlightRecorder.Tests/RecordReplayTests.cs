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
