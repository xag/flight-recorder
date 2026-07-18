// `forbid` reaches the artifacts beside the tape.
//
// The tripwire declares a property — THIS RECORDING CARRIES NO CREDENTIAL — and for a while it
// only held of the tape. Everything else the recorder writes was unguarded, so a value the tape
// correctly refused got written to a file next to it.
//
// The trace is the worst place for that hole, and these tests are built to prove it rather than
// assert it. `TracedToy.Charge` mints its credential as a LOCAL: never argued in, never returned,
// never handed to the boundary. Nothing on the tape can carry it. Only a trace can — which makes
// this exactly the case the tape's guard was structurally unable to catch.
//
// Each pair below demonstrates the hole before closing it. The first test asserts the secret IS
// there when no tripwire is declared; the second asserts it is gone when one is. A regression
// then fails the second test loudly instead of quietly passing both.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using FlightRecorder;
using FlightRecorder.Toy;
using Xunit;

namespace FlightRecorder.Tests
{
    public class ForbidSidecarTests
    {
        private const string Secret = "sk-live-";

        private static Boundary Armed() =>
            new Boundary().Forbidden(@"sk-live-\d+");

        private static string TempFile() =>
            Path.Combine(TestSupport.TempDir(), "trace.jsonl");

        /// <summary>Drive one frame through the hook the way rewritten code does.</summary>
        private static void TraceOneFrame(string local)
        {
            var frame = TraceHook.Enter("Toy.Charge", TraceHook.At("TracedToy.cs", 70),
                Array.Empty<string>(), Array.Empty<object>());
            try
            {
                TraceHook.Line(frame, "Toy.Charge", TraceHook.At("TracedToy.cs", 72),
                    new[] { "key" }, new object[] { local });
            }
            finally { TraceHook.Exit(frame); }
        }

        // --- the trace sidecar --------------------------------------------------------------

        [Fact]
        public void THE_HOLE_AnUnguardedTraceWritesTheCredentialToDisk()
        {
            var path = TempFile();
            var sink = new TraceSink(path);          // no boundary: no tripwire
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try { TraceOneFrame("sk-live-4242"); }
            finally { TraceHook.Sink = previous; sink.Close(); }

            // This is the failure mode, demonstrated. The tape never saw this value.
            Assert.Contains(Secret, File.ReadAllText(path));
        }

        [Fact]
        public void AGuardedTraceRefusesTheWriteAndTheFileDoesNotHoldIt()
        {
            var path = TempFile();
            var sink = new TraceSink(path, Armed());
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try
            {
                Assert.Throws<ForbiddenValue>(() => TraceOneFrame("sk-live-4242"));
            }
            finally { TraceHook.Sink = previous; sink.Close(); }

            // The actual claim is about the DISK, not about an exception having been raised.
            if (File.Exists(path)) Assert.DoesNotContain(Secret, File.ReadAllText(path));
        }

        [Fact]
        public void ACredentialInTheTraceHeaderMeansNoTraceFileAtAll()
        {
            var dir = TestSupport.TempDir();
            var path = Path.Combine(dir, "trace.jsonl");
            var boundary = new Boundary().Forbidden("trace_version");   // matches the header itself

            Assert.Throws<ForbiddenValue>(() => new TraceSink(path, boundary));

            // Guarded ahead of the open, not merely ahead of the write: a refusal leaves nothing.
            Assert.False(File.Exists(path), "a refused trace still created its file");
        }

        [Fact]
        public void AnInMemoryTraceIsGuardedToo()
        {
            // A trace with no path still reaches an invariant and a printed report, so "nothing
            // was written to disk" is not the same as "nothing was carried".
            var sink = new TraceSink(null, Armed());
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try
            {
                Assert.Throws<ForbiddenValue>(() => TraceOneFrame("sk-live-4242"));
            }
            finally { TraceHook.Sink = previous; }

            Assert.DoesNotContain(sink.Snapshot().Names(), n => n == "key");
        }

        [Fact]
        public void ACleanTraceIsUntouchedByADeclaredTripwire()
        {
            var path = TempFile();
            var sink = new TraceSink(path, Armed());
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try { TraceOneFrame("nothing secret here"); }
            finally { TraceHook.Sink = previous; sink.Close(); }

            var text = File.ReadAllText(path);
            Assert.Contains("nothing secret here", text);
            Assert.Contains("\"e\":\"H\"", text);
        }

        [Fact]
        public void ATraceThatDeclaresNoForbidPaysNothing()
        {
            var path = TempFile();
            var sink = new TraceSink(path);
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try { TraceOneFrame("sk-live-1"); }
            finally { TraceHook.Sink = previous; sink.Close(); }

            Assert.True(File.Exists(path));
            Assert.Single(sink.Snapshot().Values("key"));
        }

        // --- the re-write path: Save() ------------------------------------------------------

        private static string RecordGreet()
        {
            var b = TestSupport.ToyBoundary();
            return TestSupport.RecordToTape(b, store => ToyTools.Greet(store, "alice"));
        }

        [Fact]
        public void AMutationThatIntroducesAForbiddenValueRefusesToSave()
        {
            // Mutation exists to EDIT recorded values, so a tape that passed the tripwire when it
            // was written can have a credential put back into it by hand. The write path was
            // guarded; the re-write path was not.
            var rec = Recording.Load(RecordGreet(), Armed());
            rec.Call(0).Effect("Get").Result = new Doc { Name = "sk-live-99", X = 1 };

            var outPath = Path.Combine(TestSupport.TempDir(), "mutated.jsonl");
            Assert.Throws<ForbiddenValue>(() => rec.Save(outPath));

            Assert.False(File.Exists(outPath), "a refused save still created its file");
        }

        [Fact]
        public void AnUnmutatedTapeStillSavesWithTheTripwireArmed()
        {
            var rec = Recording.Load(RecordGreet(), Armed());
            var outPath = Path.Combine(TestSupport.TempDir(), "clean.jsonl");

            rec.Save(outPath);

            Assert.True(File.Exists(outPath));
            Assert.DoesNotContain(Secret, File.ReadAllText(outPath));
        }

        [Fact]
        public void ASaveThatDeclaresNoForbidIsCompletelyUnaffected()
        {
            var rec = Recording.Load(RecordGreet());          // no boundary, no tripwire
            rec.Call(0).Effect("Get").Result = new Doc { Name = "sk-live-99", X = 1 };

            var outPath = Path.Combine(TestSupport.TempDir(), "unguarded.jsonl");
            rec.Save(outPath);

            Assert.Contains(Secret, File.ReadAllText(outPath));
        }
    }
}
