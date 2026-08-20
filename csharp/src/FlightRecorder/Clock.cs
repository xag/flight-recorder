// The clock and the RNG — the doors the app cannot hand you an object for, so it holds these
// instead. Under record they ask the world and write the answer; under replay they answer from
// the tape and never touch the world. Half-shimming a door is worse than leaving it open,
// because it looks shut: code reaching for the form you skipped re-rolls on replay and the
// divergence points at a value instead of at the door it came through. So each handle covers
// every shape of the draw it stands for.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;

namespace FlightRecorder
{
    public sealed class ClockHandle
    {
        /// <summary>The wall clock, local. Recorded as a `now` event; replay hands back exactly the
        /// value the app first received — its awareness included (see the spec's `now` note).</summary>
        public DateTime Now()
        {
            if (Recorder.Replaying) return ParseNow(Recorder.ReplayFeed!.PopExpect("now"));
            var dt = Pinned()?.LocalDateTime ?? DateTime.Now;
            Recorder.EmitClock(new Dictionary<string, object?> { ["k"] = "now", ["v"] = Serial.IsoNaive(dt) });
            return dt;
        }

        /// <summary>The wall clock, UTC. Recorded as a `now` event with a trailing Z.</summary>
        public DateTime UtcNow()
        {
            if (Recorder.Replaying) return ParseNow(Recorder.ReplayFeed!.PopExpect("now"));
            var dt = Pinned()?.UtcDateTime ?? DateTime.UtcNow;
            Recorder.EmitClock(new Dictionary<string, object?> { ["k"] = "now", ["v"] = Serial.IsoNaive(dt) });
            return dt;
        }

        /// <summary>The monotonic clock — a different door: arbitrary origin, not a wall time. Its
        /// own event kind, because feeding a wall time back into it would be a category error.</summary>
        public double Mono()
        {
            if (Recorder.Replaying) return ToDouble(Recorder.ReplayFeed!.PopExpect("perf").GetValueOrNull("v"));
            var v = Recorder.MonoMs;
            Recorder.EmitClock(new Dictionary<string, object?> { ["k"] = "perf", ["v"] = v });
            return v;
        }

        /// <summary>Record with the clock SET to <paramref name="instant"/> and running: every Now()
        /// and UtcNow() read until the handle is disposed answers the instant plus the real time
        /// elapsed since it was set — in the asked kind, local or UTC — and the tape records that
        /// answer as its ordinary `now` event, faithfully, since that IS what the code was told the
        /// time was.
        ///
        /// Running, not stopped, because a stopped clock is a world no code was written for: two
        /// writes in one block would carry one timestamp, and an app whose revision stamp or "did
        /// this come after the ask" is wall time reads as broken when only the harness is. (Found
        /// the first time the pin froze: six writes, one revision, and RFC 9110's strong-validator
        /// law convicted the app for what the pin had done.)
        ///
        /// For the harness that drives a simulated week — ticks taking their moment as an argument
        /// — and then reads a board that asks the clock itself: without the pin the board is read
        /// on the machine's day, days after the week it is meant to show, and a model holding the
        /// board's statement to the simulated day convicts a disagreement the harness created. The
        /// pin is a recording affordance, not a tape feature: replay never consults it, and a tape
        /// recorded under it is indistinguishable from one recorded at that instant. Nested pins
        /// restore the outer one on dispose.</summary>
        public IDisposable At(DateTimeOffset instant)
        {
            var outer = (Hook.PinnedNow, Hook.PinnedAt);
            Hook.PinnedNow = instant;
            Hook.PinnedAt = Recorder.MonoMs;
            return new Pin(outer);
        }

        /// <summary>The set clock's current reading, or null when no pin is set. It runs on the
        /// recorder's Stopwatch, the same monotonic clock Mono() reads.</summary>
        private static DateTimeOffset? Pinned()
        {
            var pin = Hook.PinnedNow;
            if (pin == null) return null;
            return pin.Value.AddMilliseconds(Recorder.MonoMs - Hook.PinnedAt);
        }

        private sealed class Pin : IDisposable
        {
            private readonly (DateTimeOffset?, double) _outer;
            private bool _done;
            public Pin((DateTimeOffset?, double) outer) { _outer = outer; }
            public void Dispose()
            {
                if (_done) return;
                _done = true;
                (Hook.PinnedNow, Hook.PinnedAt) = _outer;
            }
        }

        private static DateTime ParseNow(IDictionary<string, object?> ev)
        {
            var s = ev.GetValueOrNull("v") as string ?? "";
            if (DateTime.TryParse(s, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var dt))
                return dt;
            return default;
        }

        private static double ToDouble(object? v) => v switch
        {
            double d => d,
            long l => l,
            int i => i,
            _ => 0.0,
        };
    }

    public sealed class RandomHandle
    {
        /// <summary>A uniform draw in [0, 1) — the JavaScript-shaped `float` draw.</summary>
        public double NextDouble()
        {
            if (Recorder.Replaying)
                return ToDouble(Recorder.ReplayFeed!.PopExpect("rand").GetValueOrNull("v"));
            var bytes = new byte[8];
            Fill(bytes);
            // 53 bits of mantissa, the standard way to a uniform double in [0, 1).
            var mantissa = BitConverter.ToUInt64(bytes, 0) >> 11;
            var v = mantissa / (double)(1UL << 53);
            Recorder.EmitClock(new Dictionary<string, object?> { ["k"] = "rand", ["m"] = "float", ["v"] = v });
            return v;
        }

        /// <summary>Raw entropy: the draw IS the value, handed back byte-for-byte on replay.</summary>
        public byte[] Bytes(int n)
        {
            if (Recorder.Replaying) return ReplayBytes(n);
            var bytes = new byte[n];
            Fill(bytes);
            Recorder.EmitClock(new Dictionary<string, object?>
            {
                ["k"] = "rand", ["m"] = "bytes", ["n"] = (long)n, ["hex"] = Recorder.ToHex(bytes),
            });
            return bytes;
        }

        /// <summary>A uniform integer in [minInclusive, maxExclusive) — the `int` draw.</summary>
        public int NextInt(int minInclusive, int maxExclusive)
        {
            if (Recorder.Replaying)
                return (int)ToLong(Recorder.ReplayFeed!.PopExpect("rand").GetValueOrNull("v"));
            var v = RandInt(minInclusive, maxExclusive);
            Recorder.EmitClock(new Dictionary<string, object?> { ["k"] = "rand", ["m"] = "int", ["v"] = (long)v });
            return v;
        }

        public int NextInt(int maxExclusive) => NextInt(0, maxExclusive);

        /// <summary>Draw <paramref name="k"/> distinct members from a population — the `sample`
        /// draw. Records the POSITIONS, not the members, so replay picks the same positions from a
        /// (possibly mutated) population without re-rolling the RNG.</summary>
        public IReadOnlyList<T> Sample<T>(IReadOnlyList<T> population, int k)
        {
            var n = population.Count;
            if (k < 0 || k > n) throw new ArgumentOutOfRangeException(nameof(k),
                $"cannot draw {k} from a population of {n}");

            int[] idx;
            if (Recorder.Replaying)
            {
                var ev = Recorder.ReplayFeed!.PopExpect("rand");
                idx = (ev.GetValueOrNull("idx") as IEnumerable<object?> ?? Enumerable.Empty<object?>())
                    .Select(x => (int)ToLong(x)).ToArray();
                foreach (var i in idx)
                    if (i < 0 || i >= n)
                        throw new ProbeUnanswerable(
                            $"the recorded sample picks position {i} but the population now has {n} " +
                            "(edit the rand event's idx to match)");
            }
            else
            {
                idx = DrawPositions(n, k);
                Recorder.EmitClock(new Dictionary<string, object?>
                {
                    ["k"] = "rand", ["m"] = "sample", ["n"] = (long)n, ["kk"] = (long)k,
                    ["idx"] = idx.Select(i => (object?)(long)i).ToList(),
                });
            }
            return idx.Select(i => population[i]).ToList();
        }

        private static int[] DrawPositions(int n, int k)
        {
            // Partial Fisher–Yates: k distinct positions in [0, n), in draw order.
            var pool = Enumerable.Range(0, n).ToArray();
            for (var i = 0; i < k; i++)
            {
                var j = RandInt(i, n);
                (pool[i], pool[j]) = (pool[j], pool[i]);
            }
            return pool.Take(k).ToArray();
        }

        /// <summary>A uniform integer in [minInclusive, maxExclusive) — netstandard2.0 has no
        /// RandomNumberGenerator.GetInt32, so this rejection-samples from raw entropy.</summary>
        private static int RandInt(int minInclusive, int maxExclusive)
        {
            var range = (ulong)((long)maxExclusive - minInclusive);
            if (range == 0) return minInclusive;
            var limit = ulong.MaxValue - (ulong.MaxValue % range);
            var buf = new byte[8];
            while (true)
            {
                Fill(buf);
                var val = BitConverter.ToUInt64(buf, 0);
                if (val >= limit) continue; // reject the biased tail
                return (int)(minInclusive + (long)(val % range));
            }
        }

        private static byte[] ReplayBytes(int n)
        {
            var ev = Recorder.ReplayFeed!.PopExpect("rand");
            var hex = ev.GetValueOrNull("hex") as string ?? "";
            var buf = FromHex(hex);
            if (buf.Length != n)
                throw new ProbeUnanswerable(
                    $"the code asked for {n} random bytes but the tape holds {buf.Length} " +
                    "(edit the rand event's n/hex to match)");
            return buf;
        }

        private static void Fill(byte[] bytes)
        {
            using var rng = RandomNumberGenerator.Create();
            rng.GetBytes(bytes);
        }

        private static byte[] FromHex(string hex)
        {
            var bytes = new byte[hex.Length / 2];
            for (var i = 0; i < bytes.Length; i++)
                bytes[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
            return bytes;
        }

        private static double ToDouble(object? v) => v switch
        {
            double d => d,
            long l => l,
            int i => i,
            _ => 0.0,
        };

        private static long ToLong(object? v) => v switch
        {
            long l => l,
            int i => i,
            double d => (long)d,
            _ => 0L,
        };
    }
}
