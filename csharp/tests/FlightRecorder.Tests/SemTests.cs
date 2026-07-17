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
            var path = TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "t@example.com", "hunter2"));
            var rec = Recording.Load(path);
            var render = rec.Call(0).RenderSpans();

            // The whole point of sem: a tape you read rather than search.
            Assert.Contains("enrol  ok", render);
            Assert.Contains("load_corpus  ok  (1 db)", render);
            Assert.Contains("- corpus_read  rows=3", render);
            Assert.Contains("register  ERROR", render);
            Assert.Contains("- registration_failed", render);
        }

        [Fact]
        public void ReplayWithTheSameClaimsDoesNotDiverge()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "t@example.com", "hunter2"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "enrol");

            var store = TestSupport.WrapStore();
            var report = Replay.Call(call, kw =>
                ToyTools.Enrol(store, (string)kw["email"]!, (string)kw["password"]!), b);

            Assert.True(report.Ok, Replay.FormatReport(0, report));
            Assert.Null(report.SemDivergence);
        }

        [Fact]
        public void ChangedTestimonyIsAThirdSignal()
        {
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, store => ToyTools.Enrol(store, "t@example.com", "hunter2"));
            var tape = Replay.LoadTape(path);
            var call = Replay.PickCall(tape, fn: "enrol");

            var store = TestSupport.WrapStore();
            // Same boundary questions, but the code no longer makes the corpus_read claim.
            var report = Replay.Call(call, kw =>
                ToyTools.Enrol(store, (string)kw["email"]!, (string)kw["password"]!, note: false), b);

            Assert.NotNull(report.SemDivergence);
            // By default a sem divergence only reports — it does not fail a replay.
            Assert.True(report.Ok);

            // Under semStrict it does.
            var strict = Replay.Call(call, kw =>
                ToyTools.Enrol(store, (string)kw["email"]!, (string)kw["password"]!, note: false), b, semStrict: true);
            Assert.NotNull(strict.SemDivergence);
            Assert.False(strict.Ok);
        }
    }
}
