// The cost of encoding one traced value is bounded, whatever the value holds.
//
// The tracer encodes every local on every executed line. Each level of a value is capped at
// TraceMaxItems, but a hundred of a hundred of a hundred is a million, and an object graph fans
// out faster than a depth cap can see. So one budget of TraceMaxNodes covers each top-level value,
// every level included, as in Python (TRACE_MAX_NODES); past it a value is a marker naming its
// type. A graph that refers back to itself is written once and then as a cycle marker, and a long
// map is cut like a long sequence, keeping its true length.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using FlightRecorder;
using Xunit;

namespace FlightRecorder.Tests
{
    public class TraceBudgetTests
    {
        private static string Encode(object? v) => Json.Stringify(Serial.TraceJsonable(v));

        private static string Opaque(string name) =>
            Json.Stringify(new Dictionary<string, object?> { ["__opaque__"] = name });

        public sealed class Fan
        {
            public List<Fan> Children { get; } = new List<Fan>();
            public int N { get; set; }
        }

        private static Fan Wide(int width, int levels)
        {
            var node = new Fan { N = levels };
            if (levels > 0)
                for (var i = 0; i < width; i++) node.Children.Add(Wide(width, levels - 1));
            return node;
        }

        [Fact]
        public void AWideGraphFinishesFastWithTheBudgetMarker()
        {
            var cube = Enumerable.Range(0, 100)
                .Select(_ => Enumerable.Range(0, 100).Select(__ => Enumerable.Range(0, 100).ToList()).ToList()).ToList();
            var graph = Wide(100, 3);
            foreach (var value in new object[] { cube, graph })
            {
                var watch = Stopwatch.StartNew();
                var json = Encode(value);
                watch.Stop();
                Assert.Contains("(beyond the trace budget)", json);
                Assert.True(watch.Elapsed < TimeSpan.FromMilliseconds(500), $"took {watch.ElapsedMilliseconds} ms");
                Assert.True(json.Length < 200_000, $"{json.Length} characters for one value");
            }
            Assert.Contains(Opaque("<Int32 (beyond the trace budget)>"), Encode(cube));
        }

        [Fact]
        public void TheBudgetIsPerValue()
        {
            // Two values of 1,500 nodes each fit, one after the other, and in one traced line.
            var a = Enumerable.Range(0, 15).Select(_ => Enumerable.Range(0, 99).ToList()).ToList();
            var b = Enumerable.Range(0, 15).Select(_ => Enumerable.Range(0, 99).ToList()).ToList();
            Assert.DoesNotContain("beyond the trace budget", Encode(a));
            Assert.DoesNotContain("beyond the trace budget", Encode(b));

            var sink = new TraceSink();
            var previous = TraceHook.Sink;
            TraceHook.Sink = sink;
            try
            {
                var frame = TraceHook.Enter("Toy.Two", TraceHook.At("TracedToy.cs", 1),
                    new[] { "a", "b" }, new object?[] { a, b });
                TraceHook.Exit(frame);
            }
            finally { TraceHook.Sink = previous; }
            Assert.DoesNotContain("beyond the trace budget", sink.Snapshot().ToJsonl());

            // ...and one of 3,000 does not.
            var big = Enumerable.Range(0, 30).Select(_ => Enumerable.Range(0, 99).ToList()).ToList();
            Assert.Contains("beyond the trace budget", Encode(big));
        }

        public sealed class Node
        {
            public Node? Self { get; set; }
            public Node? Other { get; set; }
            public int N { get; set; } = 1;
        }

        [Fact]
        public void ASelfReferencingObjectIsWrittenOnceAndThenAsACycle()
        {
            var node = new Node();
            node.Self = node;
            node.Other = node;
            Assert.Equal("{\"self\":" + Opaque("<Node (cycle)>") + ",\"other\":" + Opaque("<Node (cycle)>") + ",\"n\":1}",
                Encode(node));

            var list = new List<object?> { 1 };
            list.Add(list);
            Assert.Equal("[1," + Opaque("<List<Object> (cycle)>") + "]", Encode(list));

            var map = new Dictionary<string, object?> { ["n"] = 1 };
            map["me"] = map;
            Assert.Equal("{\"n\":1,\"me\":" + Opaque("<Dictionary<String,Object> (cycle)>") + "}", Encode(map));
        }

        [Fact]
        public void ASharedObjectIsNotACycle()
        {
            // The guard is the path being encoded, not every object seen: a value reached twice
            // by two routes is written twice.
            var leaf = new Node();
            var json = Encode(new Dictionary<string, object?> { ["a"] = leaf, ["b"] = leaf });
            Assert.DoesNotContain("cycle", json);
            Assert.Equal("{\"a\":{\"self\":null,\"other\":null,\"n\":1},\"b\":{\"self\":null,\"other\":null,\"n\":1}}", json);
        }

        [Fact]
        public void ALongMapIsCutAndKeepsItsTrueLength()
        {
            var map = Enumerable.Range(0, 1000).ToDictionary(i => "k" + i, i => (object?)i);
            var encoded = Assert.IsType<Dictionary<string, object?>>(Serial.TraceJsonable(map));
            var spec = Assert.IsType<Dictionary<string, object?>>(Assert.Single(encoded, kv => kv.Key == "__map__").Value);
            Assert.Equal(1000L, spec["len"]);
            Assert.Equal(Serial.TraceMaxItems, Assert.IsType<Dictionary<string, object?>>(spec["head"]).Count);

            var revived = Assert.IsType<TruncatedMap>(Serial.FromTraceJsonable(Json.Parse(Json.Stringify(encoded))));
            Assert.Equal(1000, revived.Length);
            Assert.Equal(Serial.TraceMaxItems, revived.Count);
            Assert.Equal(0L, revived["k0"]);
        }
    }
}
