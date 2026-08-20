using System;
using System.Collections.Generic;
using FlightRecorder;
using FlightRecorder.Toy;
using Xunit;

namespace FlightRecorder.Tests
{
    public class SemTests
    {
        [Fact]
        public void RenderReadsTopDown()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "alice", "hunter2"));
            var rec = Recording.Load(path);
            var render = rec.Call(0).RenderSpans();

            // The whole point of sem: a tape you read rather than search.
            Assert.Contains("enrol  ok", render);
            Assert.Contains("load_corpus  ok  (1 db)", render);
            Assert.Contains("- corpus_read  found=true", render);
            Assert.Contains("register  ERROR", render);
            Assert.Contains("- registration_failed", render);
        }

        [Fact]
        public void ReplayWithTheSameClaimsDoesNotDiverge()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "alice", "hunter2"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "enrol");

            var store = TestSupport.WrapStore();
            var report = Replay.Call(call, kw =>
                ToyTools.Enrol(store, (string)kw["user"]!, (string)kw["password"]!), b);

            Assert.True(report.Ok, Replay.FormatReport(0, report));
            Assert.Null(report.SemDivergence);
        }

        [Fact]
        public void ChangedTestimonyIsAThirdSignal()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "alice", "hunter2"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "enrol");

            var store = TestSupport.WrapStore();
            // Same boundary questions, but the code no longer makes the corpus_read claim.
            var report = Replay.Call(call, kw =>
                ToyTools.Enrol(store, (string)kw["user"]!, (string)kw["password"]!, note: false), b);

            Assert.NotNull(report.SemDivergence);
            // By default a sem divergence only reports — it does not fail a replay.
            Assert.True(report.Ok);

            // Under semStrict it does.
            var strict = Replay.Call(call, kw =>
                ToyTools.Enrol(store, (string)kw["user"]!, (string)kw["password"]!, note: false), b, semStrict: true);
            Assert.NotNull(strict.SemDivergence);
            Assert.False(strict.Ok);
        }

        [Fact]
        public void ADeclaredAlphabetRefusesAnUndeclaredActWhileRecording()
        {
            // The app states its acts once (Declare), the same table a model generates its
            // declarations from; a span the table does not know is refused where it is written -
            // only while a tape is being made. The recorder learns no vocabulary: the list is the app's.
            try
            {
                Recorder.Declare(new Dictionary<string, object?> { ["only-this"] = new Dictionary<string, object?>() });
                Recorder.Note("anything_at_all", new { n = 1 });   // off: no throw, no failure mode

                var b = TestSupport.ToyBoundary();
                Dictionary<string, object?> All(params string[] names)
                {
                    var d = new Dictionary<string, object?>();
                    foreach (var n in names) d[n] = new Dictionary<string, object?>();
                    return d;
                }
                var all = All("enrol", "load_corpus", "corpus_read", "register", "registration_failed");
                Recorder.Declare(all);
                TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "alice", "hunter2"));

                Recorder.Declare(All("enrol"));
                var undeclared = Assert.ThrowsAny<Exception>(() =>
                    TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "alice", "hunter2")));
                Assert.Contains("'load_corpus' is not an act this app declared", undeclared.ToString());

                all["enrol"] = new Dictionary<string, object?> { ["args"] = new List<object?> { "user", "tenant" } };
                Recorder.Declare(all);
                var underbound = Assert.ThrowsAny<Exception>(() =>
                    TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "alice", "hunter2")));
                Assert.Contains("lacks [tenant]", underbound.ToString());
            }
            finally
            {
                Recorder.Declare(null);
            }
        }
    }
}
