using System;
using System.Linq;
using System.Threading;
using FlightRecorder;
using Xunit;

namespace FlightRecorder.Tests
{
    public class ClockPinTests
    {
        [Fact]
        public void ASetClockAnswersTheInstantRunningAndRecordsItAsNow()
        {
            // Clock.At makes a recorded clock read answer the instant plus the time since — as
            // local or UTC, whichever was asked — and the tape carries that answer as its `now`
            // event, exactly as if the machine's clock had said so. Running: two reads are two
            // instants, in order, so an app stamping its writes with the clock still stamps them
            // apart. The pin is lifted on dispose, nested or not.
            var at = new DateTimeOffset(2026, 8, 16, 8, 0, 0, TimeSpan.Zero);
            var b = TestSupport.ToyBoundary();
            var path = TestSupport.RecordToTape(b, _ =>
            {
                using (Recorder.Clock.At(at))
                {
                    Recorder.Record("tick", null, () => Recorder.Clock.UtcNow());
                    var first = Recorder.Clock.UtcNow();
                    Assert.Equal(DateTimeKind.Utc, first.Kind);
                    Assert.InRange(first - at.UtcDateTime, TimeSpan.Zero, TimeSpan.FromSeconds(5));
                    Assert.InRange(Recorder.Clock.Now() - at.LocalDateTime, TimeSpan.Zero, TimeSpan.FromSeconds(5));
                    using (Recorder.Clock.At(at.AddDays(1)))
                    {
                        Recorder.Record("tick", null, () => Recorder.Clock.UtcNow());
                        Assert.InRange(Recorder.Clock.UtcNow() - at.AddDays(1).UtcDateTime,
                            TimeSpan.Zero, TimeSpan.FromSeconds(5));
                    }
                    Thread.Sleep(5);
                    Recorder.Record("tick", null, () => Recorder.Clock.UtcNow());
                    var later = Recorder.Clock.UtcNow();
                    Assert.True(later > first, "a set clock runs; it does not stop");
                    Assert.InRange(later - at.UtcDateTime, TimeSpan.Zero, TimeSpan.FromSeconds(5));
                }
                Assert.True((DateTime.UtcNow - Recorder.Clock.UtcNow()).Duration() < TimeSpan.FromSeconds(5));
            });

            var tape = Replay.LoadTape(path);
            var nows = tape.Calls
                .Select(c => (string)((System.Collections.Generic.IEnumerable<object?>)c["events"]!)
                    .Cast<System.Collections.Generic.IDictionary<string, object?>>()
                    .First(e => (string?)e["k"] == "now")["v"]!)
                .ToList();
            Assert.Equal(3, nows.Count);
            Assert.StartsWith("2026-08-16T08:00:0", nows[0]);
            Assert.StartsWith("2026-08-17T08:00:0", nows[1]);
            Assert.StartsWith("2026-08-16T08:00:0", nows[2]);
            Assert.EndsWith("Z", nows[0]);
        }
    }
}
