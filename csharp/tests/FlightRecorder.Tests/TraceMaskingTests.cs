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
    }
}
