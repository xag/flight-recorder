// The boundary's masks reach the trace, and the trace encoder does not fetch process state.
//
// Two holes, each closed here with the test that shows it.
//
// The first: a trace holds every local of every executed line, and until now only the boundary's
// `Forbid` read those lines. A boundary that masked a credential on the tape with `Scrub` or a
// `Redact` rule found it raw in the trace beside it. Now `Redact` and `Scrub` mask the trace's
// payload (`args`, `d`, `v`) exactly as they mask a tape event's, and never its envelope
// (`e`, `fn`, `at`). A trace's payload is keyed by local name as a tape's kwargs are keyed by
// parameter name, so a field rule reaches a local of that name.
//
// The second: a traced value is walked through its public properties. Some objects are not
// values the execution computed but handles on the process: a `ProcessStartInfo` fills its
// `EnvironmentVariables` from the whole environment the first time the property is read, and a
// `Type`, an `Assembly`, a `MethodInfo` or a delegate opens onto the reflection graph, whose walk
// does not finish. Those are written as a name reference, never opened.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using FlightRecorder;
using Xunit;

namespace FlightRecorder.Tests
{
    public class TraceMaskingTests
    {
        private const string Key = "sk-live-4242424242";

        private static string TempFile() => Path.Combine(TestSupport.TempDir(), "trace.jsonl");

        /// <summary>Drive one frame through the hook the way rewritten code does: a call with
        /// one argument, a line that sets locals, a return.</summary>
        private static void TraceOneFrame(string[] names, object?[] values, object? returned = null)
        {
            var frame = TraceHook.Enter("Toy.Charge", TraceHook.At("TracedToy.cs", 70),
                new[] { "email" }, new object?[] { "t@x.com" });
            try
            {
                TraceHook.Line(frame, "Toy.Charge", TraceHook.At("TracedToy.cs", 72), names, values);
                TraceHook.Return(frame, "Toy.Charge", TraceHook.At("TracedToy.cs", 74), returned);
            }
            finally { TraceHook.Exit(frame); }
        }

        private static (string Text, Trace Trace) Traced(Boundary boundary, string[] names, object?[] values,
            object? returned = null)
        {
            var path = TempFile();
            var sink = new TraceSink(path, boundary);
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try { TraceOneFrame(names, values, returned); }
            finally { TraceHook.Sink = previous; sink.Close(); }
            return (File.ReadAllText(path), sink.Snapshot());
        }

        private static List<Dictionary<string, object?>> Events(string text) =>
            text.Split('\n').Where(l => l.Length > 0)
                .Select(l => (Dictionary<string, object?>)Json.Parse(l)!).ToList();

        // --- the boundary's masks reach the trace ------------------------------------------

        [Fact]
        public void TheScrubMasksTheTraceAsItMasksTheTape()
        {
            var boundary = new Boundary { Scrub = s => s.Replace(Key, "[KEY]") };
            var (text, trace) = Traced(boundary,
                new[] { "header" }, new object?[] { "Bearer " + Key },
                returned: new Dictionary<string, object?> { ["sent"] = Key });

            Assert.Contains("[KEY]", text);
            Assert.DoesNotContain(Key, text);
            // The in-memory trace is what an invariant and a printed report read.
            Assert.DoesNotContain(Key, trace.ToJsonl());
        }

        [Fact]
        public void ARedactedFieldNameMasksTheLocalOfThatName()
        {
            var boundary = new Boundary().MaskFields("secret");
            var (text, _) = Traced(boundary,
                new[] { "secret", "n" }, new object?[] { Key, 3 });

            var delta = Events(text).Single(e => (string?)e["e"] == "L")["d"] as Dictionary<string, object?>;
            Assert.Equal(Serial.Redacted, delta!["secret"]);
            Assert.Equal(3L, delta["n"]);
            Assert.DoesNotContain(Key, text);
        }

        [Fact]
        public void TheTraceEnvelopeIsNotRedacted()
        {
            // As on the tape, rules rewrite payloads only: a rule named like an envelope key
            // cannot take the structure of the trace apart.
            var boundary = new Boundary().MaskFields("e", "fn", "at");
            var (text, _) = Traced(boundary, new[] { "n" }, new object?[] { 1 }, returned: 2);

            var events = Events(text);
            Assert.Equal(new[] { "H", "C", "L", "R" }, events.Select(e => (string?)e["e"]).ToArray());
            Assert.All(events.Skip(1), e => Assert.Equal("Toy.Charge", e["fn"]));
            Assert.All(events.Skip(1), e => Assert.StartsWith("TracedToy.cs:", (string)e["at"]!));
        }

        [Fact]
        public void TheForbidTripwireReadsTheMaskedLine()
        {
            // A credential the scrub masked is no longer on the line, so the tripwire that backs
            // the scrub has nothing left to refuse.
            var boundary = new Boundary { Scrub = s => s.Replace(Key, "[KEY]") }.Forbidden(@"sk-live-\d+");
            var (text, _) = Traced(boundary, new[] { "key" }, new object?[] { Key });
            Assert.Contains("[KEY]", text);
        }

        // --- what is not data is never walked ----------------------------------------------

        private const string EnvSentinel = "fr-env-sentinel-7c1e9a";
        private const string EnvName = "FR_TRACE_SENTINEL";

        private sealed class Holder
        {
            public ProcessStartInfo Start { get; } = new ProcessStartInfo("x");
            public Type Kind { get; } = typeof(string);
            public int N { get; } = 1;
        }

        private static IEnumerable<object> NotData()
        {
            Func<int, int> lambda = x => x + 1;
            yield return new ProcessStartInfo("x");
            yield return new Process();
            yield return typeof(string);
            yield return typeof(TraceMaskingTests).Assembly;
            yield return typeof(TraceMaskingTests).Module;
            yield return typeof(Serial).GetMethod(nameof(Serial.TraceJsonable))!;
            yield return lambda;
            yield return AppDomain.CurrentDomain;
            yield return System.Threading.Thread.CurrentThread;
        }

        [Fact]
        public void AProcessHandleIsTracedAsItsName()
        {
            Environment.SetEnvironmentVariable(EnvName, EnvSentinel);
            try
            {
                Assert.Equal(Ref("<ProcessStartInfo>"), Json.Stringify(Serial.TraceJsonable(new ProcessStartInfo("x"))));
                Assert.Equal(Ref("<Process>"), Json.Stringify(Serial.TraceJsonable(new Process())));
                Assert.Equal(Ref("<type 'System.String'>"), Json.Stringify(Serial.TraceJsonable(typeof(string))));
                Assert.Equal(Ref("<assembly 'FlightRecorder'>"), Json.Stringify(Serial.TraceJsonable(typeof(Serial).Assembly)));
                Assert.Equal(Ref("<method 'FlightRecorder.Serial.TraceJsonable'>"),
                    Json.Stringify(Serial.TraceJsonable(typeof(Serial).GetMethod(nameof(Serial.TraceJsonable)))));
            }
            finally { Environment.SetEnvironmentVariable(EnvName, null); }
        }

        private static string Ref(string name) => Json.Stringify(new Dictionary<string, object?> { ["__opaque__"] = name });

        [Fact]
        public void WhatIsNotDataIsNeverWalked()
        {
            Environment.SetEnvironmentVariable(EnvName, EnvSentinel);
            try
            {
                foreach (var v in NotData())
                {
                    var encoded = Serial.TraceJsonable(v);
                    var json = Json.Stringify(encoded);
                    Assert.DoesNotContain(EnvSentinel, json);
                    // One marker naming it, not a map of its properties.
                    var map = Assert.IsType<Dictionary<string, object?>>(encoded);
                    Assert.Equal(new[] { "__opaque__" }, map.Keys.ToArray());
                }
                var held = Json.Stringify(Serial.TraceJsonable(new Holder()));
                Assert.DoesNotContain(EnvSentinel, held);
                Assert.Contains(Ref("<ProcessStartInfo>"), held);
                Assert.DoesNotContain(EnvSentinel, Json.Stringify(Serial.TraceJsonable(
                    new Dictionary<string, object?> { ["starts"] = new List<object> { new ProcessStartInfo("y") } })));
            }
            finally { Environment.SetEnvironmentVariable(EnvName, null); }
        }

        [Fact]
        public void ATracedFrameHoldingAProcessStartInfoWritesNoEnvironment()
        {
            Environment.SetEnvironmentVariable(EnvName, EnvSentinel);
            try
            {
                var psi = new ProcessStartInfo("git");
                // The app read one variable; the encoder must not go and fetch the rest.
                var (text, trace) = Traced(new Boundary(),
                    new[] { "psi", "kind", "run" },
                    new object?[] { psi, typeof(string), (Action)(() => { }) });
                Assert.DoesNotContain(EnvSentinel, text);
                Assert.DoesNotContain(EnvSentinel, trace.ToJsonl());
                Assert.Contains("\"psi\":" + Ref("<ProcessStartInfo>"), text);
            }
            finally { Environment.SetEnvironmentVariable(EnvName, null); }
        }

        // --- an awaitable is never waited on -----------------------------------------------
        //
        // A traced local holding a pending Task<T> was walked through its public properties, and
        // Task<T>.Result blocks until the task completes: the trace hook, running on the traced
        // code's own line, stopped the code it was observing. An awaitable is written as its name
        // and its status, never its result, as Python writes it.

        /// <summary>Encode on a worker and give up after a while, so a red run fails instead of
        /// hanging the suite. <paramref name="release"/> then unblocks whatever the encoder
        /// waited on, so the worker does not outlive the test.</summary>
        private static string EncodeWithin(object? v, Action release)
        {
            var work = System.Threading.Tasks.Task.Run(() => Json.Stringify(Serial.TraceJsonable(v)));
            try
            {
                Assert.True(work.Wait(TimeSpan.FromSeconds(5)), "the trace encoder blocked on " + v?.GetType().Name);
                return work.Result;
            }
            finally { release(); }
        }

        private static async System.Threading.Tasks.Task<int> Awaiting(System.Threading.Tasks.Task<int> pending) =>
            await pending + 1;

        [Fact]
        public void APendingTaskIsTracedAsItsNameAndStatusWithoutWaiting()
        {
            var tcs = new System.Threading.Tasks.TaskCompletionSource<int>();
            Assert.Equal(Ref("<Task<Int32> WaitingForActivation>"), EncodeWithin(tcs.Task, () => tcs.TrySetResult(1)));

            // An async method's task is a runtime subclass of Task<T>; it is named as the Task<T> it is.
            var gate = new System.Threading.Tasks.TaskCompletionSource<int>();
            var running = Awaiting(gate.Task);
            Assert.Equal(Ref("<Task<Int32> WaitingForActivation>"), EncodeWithin(running, () => gate.TrySetResult(1)));

            var plain = new System.Threading.Tasks.TaskCompletionSource<bool>();
            Assert.Equal(Ref("<Task WaitingForActivation>"),
                EncodeWithin((System.Threading.Tasks.Task)plain.Task.ContinueWith(_ => { }), () => plain.TrySetResult(true)));
        }

        [Fact]
        public void ACompletedTaskIsTracedWithoutItsResult()
        {
            Assert.Equal(Ref("<Task<Int32> RanToCompletion>"), EncodeWithin(System.Threading.Tasks.Task.FromResult(4242), () => { }));
            var failed = System.Threading.Tasks.Task.FromException<int>(new InvalidOperationException("boom"));
            Assert.Equal(Ref("<Task<Int32> Faulted>"), EncodeWithin(failed, () => { }));
        }

        [Fact]
        public void AValueTaskIsNeverWaitedOnNorConsumed()
        {
            var tcs = new System.Threading.Tasks.TaskCompletionSource<int>();
            var overTask = new System.Threading.Tasks.ValueTask<int>(tcs.Task);
            Assert.Equal(Ref("<ValueTask<Int32> Pending>"), EncodeWithin(overTask, () => tcs.TrySetResult(1)));

            var source = new OneShotSource();
            var overSource = new System.Threading.Tasks.ValueTask<int>(source, 0);
            Assert.Equal(Ref("<ValueTask<Int32> Pending>"), EncodeWithin(overSource, source.Release));
            Assert.Equal(0, source.ResultsTaken);       // reading a source's result consumes it

            Assert.Equal(Ref("<ValueTask<Int32> RanToCompletion>"),
                EncodeWithin(new System.Threading.Tasks.ValueTask<int>(7), () => { }));
            Assert.Equal(Ref("<ValueTask RanToCompletion>"), EncodeWithin(default(System.Threading.Tasks.ValueTask), () => { }));
        }

        [Fact]
        public void AnyAwaitableIsTracedAsItsNameAndNeverOpened()
        {
            var cold = new ColdAwaitable();
            Assert.Equal(Ref("<ColdAwaitable>"), EncodeWithin(cold, cold.Release));
            Assert.False(cold.Started);
            // ...including where an object holds it.
            var holder = new Dictionary<string, object?> { ["job"] = cold, ["n"] = 1 };
            Assert.Equal("{\"job\":" + Ref("<ColdAwaitable>") + ",\"n\":1}", EncodeWithin(holder, cold.Release));
        }

        /// <summary>An IValueTaskSource that blocks a caller of GetResult until released, and counts them.</summary>
        private sealed class OneShotSource : System.Threading.Tasks.Sources.IValueTaskSource<int>
        {
            private readonly System.Threading.ManualResetEventSlim _released = new System.Threading.ManualResetEventSlim(false);
            public int ResultsTaken;
            public void Release() => _released.Set();

            public int GetResult(short token)
            {
                System.Threading.Interlocked.Increment(ref ResultsTaken);
                _released.Wait();
                return 1;
            }

            public System.Threading.Tasks.Sources.ValueTaskSourceStatus GetStatus(short token) =>
                _released.IsSet ? System.Threading.Tasks.Sources.ValueTaskSourceStatus.Succeeded
                    : System.Threading.Tasks.Sources.ValueTaskSourceStatus.Pending;

            public void OnCompleted(Action<object?> continuation, object? state, short token,
                System.Threading.Tasks.Sources.ValueTaskSourceOnCompletedFlags flags) { }
        }

        /// <summary>An awaitable whose public property waits for the work it starts.</summary>
        private sealed class ColdAwaitable
        {
            private readonly System.Threading.ManualResetEventSlim _released = new System.Threading.ManualResetEventSlim(false);
            public bool Started { get; private set; }
            public void Release() => _released.Set();

            public int Value
            {
                get { Started = true; _released.Wait(); return 1; }
            }

            public System.Runtime.CompilerServices.TaskAwaiter<int> GetAwaiter() =>
                System.Threading.Tasks.Task.FromResult(Value).GetAwaiter();
        }

        // --- a deferred computation is never run -------------------------------------------
        //
        // A Lazy<T> was walked through its public Value, which runs the factory; an iterator or a
        // lazy LINQ query was enumerated to the end to count it. Either runs the app's own code
        // from inside the trace hook, once per traced line, with whatever side effects it has, and
        // an endless sequence never let the hook return. Both are written as their names, as
        // Python writes a generator. A collection, which holds its items, is still written as its
        // items, counted by its Count rather than by enumerating it to the end.

        [Fact]
        public void ALazyIsWrittenAsWhetherItHoldsAValueAndNeverForced()
        {
            var runs = 0;
            var gate = new System.Threading.ManualResetEventSlim(false);
            var lazy = new Lazy<int>(() => { runs++; gate.Wait(); return 4242; });
            Assert.Equal(Ref("<Lazy<Int32> IsValueCreated=False>"), EncodeWithin(lazy, gate.Set));
            Assert.Equal(0, runs);

            Assert.Equal(4242, lazy.Value);
            var forced = EncodeWithin(lazy, () => { });
            Assert.Equal(Ref("<Lazy<Int32> IsValueCreated=True>"), forced);
            Assert.Equal(1, runs);
        }

        private sealed class Counter { public int Steps; }

        private static IEnumerable<int> Counted(Counter c)
        {
            c.Steps++;
            yield return 1;
            c.Steps++;
            yield return 2;
        }

        private static IEnumerable<long> Naturals(System.Runtime.CompilerServices.StrongBox<bool> stop)
        {
            for (long i = 0; !stop.Value; i++) yield return i;
        }

        [Fact]
        public void AnIteratorIsWrittenAsItsNameAndNeverEnumerated()
        {
            var counter = new Counter();
            Assert.Equal(Ref("<iterator 'Counted'>"), EncodeWithin(Counted(counter), () => { }));
            Assert.Equal(0, counter.Steps);

            var calls = 0;
            var query = new[] { 1, 2, 3 }.Where(i => { calls++; return i > 1; });
            var json = EncodeWithin(query, () => { });
            Assert.StartsWith("{\"__opaque__\":\"<", json);
            Assert.Equal(0, calls);

            var stop = new System.Runtime.CompilerServices.StrongBox<bool>(false);
            Assert.Equal(Ref("<iterator 'Naturals'>"), EncodeWithin(Naturals(stop), () => stop.Value = true));
        }

        /// <summary>A collection that says how many it holds and whose enumerator never ends.</summary>
        private sealed class EndlessCollection : IReadOnlyCollection<int>
        {
            public volatile bool Stop;
            public int Count => 500;

            public IEnumerator<int> GetEnumerator()
            {
                for (var i = 0; !Stop; i++) yield return i;
            }

            System.Collections.IEnumerator System.Collections.IEnumerable.GetEnumerator() => GetEnumerator();
        }

        [Fact]
        public void ACollectionIsWrittenAsItsItemsCountedByItsCount()
        {
            Assert.Equal("[1,2,3]", EncodeWithin(new HashSet<int> { 1, 2, 3 }, () => { }));
            Assert.Equal("[1,2]", EncodeWithin(new[] { 1, 2 }, () => { }));
            Assert.Equal("[\"a\"]", EncodeWithin(new System.Collections.ObjectModel.ReadOnlyCollection<string>(new[] { "a" }), () => { }));

            var long150 = EncodeWithin(Enumerable.Range(0, 150).ToList(), () => { });
            Assert.StartsWith("{\"__seq__\":{\"len\":150,\"head\":[0,1,", long150);

            // Counted by Count, and enumerated no further than the head it keeps.
            var endless = new EndlessCollection();
            var json = EncodeWithin(endless, () => endless.Stop = true);
            Assert.StartsWith("{\"__seq__\":{\"len\":500,\"head\":[0,1,", json);
        }
    }
}
