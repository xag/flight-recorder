// Recording. Emits tape format v1 (see spec/tape-v1.md).
//
// WHY THE BOUNDARY IS DECLARED BY WRAPPING
//
// .NET cannot patch a module's functions the way Python does, and — like an ES module — the
// references a compiled assembly already bound cannot be swapped from outside. So a boundary
// is declared by wrapping the objects the app HOLDS. `Wrap<T>` returns a transparent
// DispatchProxy that forwards every call to the real thing and records the named methods —
// not a mock, not a duplicate. The cardinal rule holds: nothing here evaluates a query,
// reimplements a client, or knows what any value means. It knows names.
//
// The clock and the RNG are the exception: the app holds no object to wrap there, so it holds
// the recorder's own `Clock` and `Random` handles instead. Under record they ask the world and
// write the answer down; under replay they answer from the tape and never touch the world. The
// app cannot tell the difference — which is what makes replay a resurrection of the original
// execution rather than a re-enactment of it.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;

namespace FlightRecorder
{
    public enum Mode { Record, Replay }

    /// <summary>A tape sink for hosts with no durable filesystem: handed the WHOLE session text
    /// each time, so an overwriting sink is enough and a tape is never half-published.</summary>
    public interface ISink
    {
        void Publish(string name, string text);
    }

    /// <summary>The per-call event buffer plus its span-id counter. Flows across every await via
    /// <see cref="Recorder"/>'s AsyncLocal, so concurrent calls never interleave their events.</summary>
    internal sealed class CallBuffer
    {
        public readonly List<Dictionary<string, object?>> Events = new List<Dictionary<string, object?>>();
        public int Sid;
    }

    /// <summary>The one piece of shared state between recording and replay: the mode, the feed
    /// replay answers from, and the buffer the replayed code's own sem calls land in.</summary>
    internal static class Hook
    {
        // AsyncLocal, not ThreadStatic: an async replay hops threads across awaits, and the mode,
        // the feed, and the sem-capture buffer must follow the execution the way a contextvar does.
        private static readonly AsyncLocal<Mode?> _mode = new AsyncLocal<Mode?>();
        private static readonly AsyncLocal<Feed?> _feed = new AsyncLocal<Feed?>();
        private static readonly AsyncLocal<CallBuffer?> _sems = new AsyncLocal<CallBuffer?>();

        public static Mode? Mode { get => _mode.Value; set => _mode.Value = value; }
        public static Feed? Feed { get => _feed.Value; set => _feed.Value = value; }
        public static CallBuffer? Sems { get => _sems.Value; set => _sems.Value = value; }
    }

    public static partial class Recorder
    {
        public const int FormatVersion = 1;

        private static readonly AsyncLocal<CallBuffer?> Active = new AsyncLocal<CallBuffer?>();
        private static readonly AsyncLocal<bool> InsideEffect = new AsyncLocal<bool>();

        private static readonly string[] PayloadKeys = { "args", "kwargs", "res", "result", "data" };
        private static readonly Stopwatch Mono = Stopwatch.StartNew();

        private static TapeWriter? _writer;
        private static Boundary? _boundary;
        private static Func<string, object?, bool>? _gate;

        /// <summary>The wall clock and the monotonic clock, as handles the app holds and calls.</summary>
        public static readonly ClockHandle Clock = new ClockHandle();

        /// <summary>Every door randomness comes through, as a handle the app holds and calls.</summary>
        public static readonly RandomHandle Random = new RandomHandle();

        // --- install --------------------------------------------------------------------

        /// <summary>Turn recording on. Returns the tape path (or its name, for a sink-only tape),
        /// or null when disabled. `gate(fn, kwargs)` decides per call, so production can record
        /// only the calls that matter.</summary>
        public static string? Install(Boundary boundary, string? directory = ".flight",
            bool enabled = true, Func<string, object?, bool>? gate = null, ISink? sink = null)
        {
            if (!enabled) return null;
            _boundary = boundary;
            _gate = gate;
            _writer = new TapeWriter(directory, boundary, sink);
            return _writer.Path ?? _writer.Name;
        }

        public static void Uninstall()
        {
            _writer = null;
            _boundary = null;
            _gate = null;
        }

        /// <summary>The tape being written, or null.</summary>
        public static string? TapePath => _writer?.Path;

        internal static Boundary? CurrentBoundary => _boundary;

        // --- the tape -------------------------------------------------------------------

        private sealed class TapeWriter
        {
            public readonly string? Path;
            public readonly string Name;
            private readonly ISink? _sink;
            private readonly Boundary _boundary;
            private string _text = "";
            private int _seq;

            public TapeWriter(string? directory, Boundary boundary, ISink? sink)
            {
                _sink = sink;
                _boundary = boundary;

                // A unique name — the entropy is not decoration: two processes can start within
                // the same second, and a sink that stores by name would then have one tape
                // silently overwrite the other.
                var stamp = DateTimeOffset.Now.ToString("yyyyMMddTHHmmss", CultureInfo.InvariantCulture);
                var nonce = RandomHex(4);
                var pid = Process.GetCurrentProcess().Id;
                Name = $"flight-{stamp}-{pid}-{nonce}.jsonl";

                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory!);
                    Path = System.IO.Path.Combine(directory!, Name);
                }

                var header = new Dictionary<string, object?>
                {
                    ["ev"] = "session",
                    ["version"] = (long)FormatVersion,
                    ["started"] = Serial.Iso(DateTimeOffset.Now),
                    ["dotnet"] = Environment.Version.ToString(),
                    ["constants"] = Serial.ToJsonable(boundary.Constants),
                };
                foreach (var kv in boundary.HeaderExtras) header[kv.Key] = Serial.ToJsonable(kv.Value);
                Write(header);
            }

            private void Write(Dictionary<string, object?> obj)
            {
                var line = Json.Stringify(obj) + "\n";
                var hit = Serial.ForbiddenHit(line, _boundary.Forbid);
                if (hit != null)
                    throw new ForbiddenValue(
                        $"a forbid pattern matched the line about to be written: /{hit}/ — " +
                        "nothing was recorded");
                _text += line;
                if (Path != null) File.AppendAllText(Path, line);
            }

            public void WriteCall(string fn, object? kwargs, List<Dictionary<string, object?>> events,
                object? result, string? error, double ms)
            {
                _seq += 1;
                // An effect whose slot was reserved but never settled (fired and not awaited) gave
                // no answer, so it influenced nothing — and a half-fx would be invalid anyway.
                var settled = events.Where(e => (string?)e.GetValueOrNull("k") != "fx"
                                                || e.ContainsKey("res") || e.ContainsKey("err")).ToList();
                var call = new Dictionary<string, object?>
                {
                    ["ev"] = "call",
                    ["seq"] = (long)_seq,
                    ["fn"] = fn,
                    ["kwargs"] = Serial.RedactJsonable(Serial.ToJsonable(kwargs), _boundary.RedactRules, _boundary.Scrub),
                    ["events"] = settled.Cast<object?>().ToList(),
                    ["result"] = error != null ? null
                        : Serial.RedactJsonable(Serial.ToJsonable(result), _boundary.RedactRules, _boundary.Scrub),
                    ["error"] = error,
                    ["ts"] = Serial.Iso(DateTimeOffset.Now),
                    ["ms"] = Math.Round(ms, 2),
                };
                Write(call);
                Flush();
            }

            private void Flush()
            {
                if (_sink == null) return;
                // A sink that throws is swallowed: recording must never be the reason a call fails.
                try { _sink.Publish(Name, _text); }
                catch (Exception e) { System.Diagnostics.Trace.WriteLine($"flight-recorder: sink publish failed — {e.Message}"); }
            }
        }

        private static string RandomHex(int n)
        {
            var bytes = new byte[n];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(bytes);
            return ToHex(bytes);
        }

        internal static string ToHex(byte[] bytes)
        {
            var c = new char[bytes.Length * 2];
            const string hex = "0123456789abcdef";
            for (var i = 0; i < bytes.Length; i++)
            {
                c[i * 2] = hex[bytes[i] >> 4];
                c[i * 2 + 1] = hex[bytes[i] & 0xF];
            }
            return new string(c);
        }

        // --- scrub / emit ---------------------------------------------------------------

        private static Dictionary<string, object?> Scrub(Dictionary<string, object?> ev)
        {
            var rules = _boundary?.RedactRules;
            var scrub = _boundary?.Scrub;
            if ((rules == null || rules.Count == 0) && scrub == null) return ev;
            var outEv = new Dictionary<string, object?>(ev);
            foreach (var k in PayloadKeys)
                if (outEv.ContainsKey(k)) outEv[k] = Serial.RedactJsonable(outEv[k], rules, scrub);
            return outEv;
        }

        private static void Emit(Dictionary<string, object?> ev)
        {
            if (InsideEffect.Value) return; // the far side of a door we already record
            var buf = Active.Value;
            buf?.Events.Add(Scrub(ev));
        }

        // --- the call boundary ----------------------------------------------------------

        /// <summary>Record one tool call. That line IS the execution, because the code is
        /// deterministic given the answers the world gave it. A no-op envelope when recording is
        /// off or the gate says no — the body just runs.</summary>
        public static T Record<T>(string fn, object? kwargs, Func<T> body)
        {
            if (_writer == null || (_gate != null && !_gate(fn, kwargs))) return body();

            var buf = new CallBuffer();
            var t0 = Mono.Elapsed.TotalMilliseconds;
            var prev = Active.Value;
            Active.Value = buf;
            T result = default!;
            string? error = null;
            try
            {
                result = body();
                return result;
            }
            catch (Exception e)
            {
                error = $"{e.GetType().Name}: {e.Message}";
                throw;
            }
            finally
            {
                Active.Value = prev;
                try { _writer.WriteCall(fn, kwargs, buf.Events, result, error, Mono.Elapsed.TotalMilliseconds - t0); }
                catch (ForbiddenValue) { throw; }
                catch (Exception e) { System.Diagnostics.Trace.WriteLine($"flight-recorder: could not write the call — {e.Message}"); }
            }
        }

        public static async Task<T> RecordAsync<T>(string fn, object? kwargs, Func<Task<T>> body)
        {
            if (_writer == null || (_gate != null && !_gate(fn, kwargs))) return await body().ConfigureAwait(false);

            var buf = new CallBuffer();
            var t0 = Mono.Elapsed.TotalMilliseconds;
            var prev = Active.Value;
            Active.Value = buf;
            T result = default!;
            string? error = null;
            try
            {
                result = await body().ConfigureAwait(false);
                return result;
            }
            catch (Exception e)
            {
                error = $"{e.GetType().Name}: {e.Message}";
                throw;
            }
            finally
            {
                Active.Value = prev;
                try { _writer.WriteCall(fn, kwargs, buf.Events, result, error, Mono.Elapsed.TotalMilliseconds - t0); }
                catch (ForbiddenValue) { throw; }
                catch (Exception e) { System.Diagnostics.Trace.WriteLine($"flight-recorder: could not write the call — {e.Message}"); }
            }
        }

        // --- effects --------------------------------------------------------------------

        /// <summary>Wrap an interface client so the named methods are recorded as `fx` events.
        /// Everything not named passes straight through, untouched and unrecorded. Under replay the
        /// named methods answer from the tape and the real client is never touched.</summary>
        public static T Wrap<T>(T target, params string[] methods) where T : class =>
            WrapAs(target, DefaultPrefix(typeof(T)), methods);

        /// <summary>As <see cref="Wrap{T}"/>, but with an explicit prefix for the recorded `fn`
        /// (e.g. "store" → "store.get"), so a tape reads the way the app talks about its clients.</summary>
        public static T WrapAs<T>(T target, string prefix, params string[] methods) where T : class
        {
            var proxy = DispatchProxyFactory.Create<T>();
            ((EffectProxy)(object)proxy).Init(target!, new HashSet<string>(methods), prefix);
            return proxy;
        }

        private static string DefaultPrefix(System.Type t)
        {
            var name = t.Name;
            // An interface named IStore reads best on the tape as "store".
            if (name.Length > 1 && name[0] == 'I' && char.IsUpper(name[1])) name = name.Substring(1);
            return char.ToLowerInvariant(name[0]) + name.Substring(1);
        }

        /// <summary>Record a synchronous effect functionally: the primitive `Wrap` is built on.</summary>
        public static object? Effect(string fn, object?[] args, Func<object?> real)
        {
            if (Hook.Mode == Mode.Replay)
                return Hook.Feed!.AnswerEffect(fn, args.Select(Serial.ToJsonable).ToList());

            var buf = Active.Value;
            if (_writer == null || buf == null) return real();

            var ev = new Dictionary<string, object?>
            {
                ["k"] = "fx",
                ["fn"] = fn,
                ["args"] = args.Select(a => Serial.ToJsonable(a)).ToList(),
                ["kwargs"] = new Dictionary<string, object?>(), // .NET has no kwargs; spec fixes {}
            };
            var slot = buf.Events.Count;
            buf.Events.Add(Scrub(ev));

            var wasInside = InsideEffect.Value;
            InsideEffect.Value = true;
            object? res;
            try { res = real(); }
            catch (Exception e)
            {
                InsideEffect.Value = wasInside;
                buf.Events[slot] = Scrub(WithErr(ev, e));
                throw;
            }
            InsideEffect.Value = wasInside;
            buf.Events[slot] = Scrub(WithRes(ev, res));
            return res;
        }

        internal static async Task<object?> EffectAsync(string fn, object?[] args, Func<Task<object?>> real)
        {
            if (Hook.Mode == Mode.Replay)
                return Hook.Feed!.AnswerEffect(fn, args.Select(Serial.ToJsonable).ToList());

            var buf = Active.Value;
            if (_writer == null || buf == null) return await real().ConfigureAwait(false);

            var ev = new Dictionary<string, object?>
            {
                ["k"] = "fx",
                ["fn"] = fn,
                ["args"] = args.Select(a => Serial.ToJsonable(a)).ToList(),
                ["kwargs"] = new Dictionary<string, object?>(),
            };
            var slot = buf.Events.Count; // reserved NOW, in issue order, before the first await
            buf.Events.Add(Scrub(ev));

            var wasInside = InsideEffect.Value;
            InsideEffect.Value = true;
            try
            {
                var res = await real().ConfigureAwait(false);
                buf.Events[slot] = Scrub(WithRes(ev, res));
                return res;
            }
            catch (Exception e)
            {
                buf.Events[slot] = Scrub(WithErr(ev, e));
                throw;
            }
            finally
            {
                InsideEffect.Value = wasInside;
            }
        }

        private static Dictionary<string, object?> WithRes(Dictionary<string, object?> ev, object? res)
        {
            var o = new Dictionary<string, object?>(ev) { ["res"] = Serial.ToJsonable(res) };
            return o;
        }

        private static Dictionary<string, object?> WithErr(Dictionary<string, object?> ev, Exception e)
        {
            var o = new Dictionary<string, object?>(ev) { ["err"] = ErrEvent(e) };
            return o;
        }

        /// <summary>Record a raised effect error. `args` carries its CONSTRUCTIVE values — what a
        /// reviver is handed to rebuild it. A .NET exception's constructive value is its message.</summary>
        internal static Dictionary<string, object?> ErrEvent(Exception e) =>
            new Dictionary<string, object?>
            {
                ["type"] = e.GetType().Name,
                ["repr"] = Trunc($"{e.GetType().Name}: {e.Message}", 300),
                ["args"] = new List<object?> { e.Message },
            };

        private static string Trunc(string s, int n) => s.Length <= n ? s : s.Substring(0, n);

        // --- chained clients: the `db` kind ---------------------------------------------

        /// <summary>Record a terminal chain READ (Firestore-style). `sig` is the rendered chain
        /// that led here; `snapshots` are the documents the read answered with. Under replay the
        /// snapshots come from the tape.</summary>
        public static IReadOnlyList<Snapshot> DbRead(string op, string sig, Func<IReadOnlyList<Snapshot>> real)
        {
            if (Hook.Mode == Mode.Replay) return Hook.Feed!.AnswerDbRead(op, sig);

            var buf = Active.Value;
            if (_writer == null || buf == null) return real();

            var wasInside = InsideEffect.Value;
            InsideEffect.Value = true;
            IReadOnlyList<Snapshot> snaps;
            try { snaps = real(); }
            finally { InsideEffect.Value = wasInside; }

            var res = snaps.Select(s => (object?)Serial.SnapshotJsonable(s.Id, s.Exists, s.Data)).ToList();
            buf.Events.Add(Scrub(new Dictionary<string, object?>
            {
                ["k"] = "db", ["op"] = op, ["sig"] = sig, ["res"] = res,
            }));
            return snaps;
        }

        /// <summary>Record a terminal chain WRITE. `args` are the write's inputs (the questions,
        /// not answers). Under replay the write is not performed; the tape already holds it.</summary>
        public static void DbWrite(string op, string sig, object?[] args, Action real)
        {
            if (Hook.Mode == Mode.Replay)
            {
                Hook.Feed!.ExpectDbWrite(op, sig);
                return;
            }
            var buf = Active.Value;
            if (_writer == null || buf == null) { real(); return; }

            var wasInside = InsideEffect.Value;
            InsideEffect.Value = true;
            try { real(); }
            finally { InsideEffect.Value = wasInside; }

            buf.Events.Add(Scrub(new Dictionary<string, object?>
            {
                ["k"] = "db", ["op"] = op, ["sig"] = sig,
                ["args"] = args.Select(a => Serial.ToJsonable(a)).ToList(),
            }));
        }

        // --- semantic events ------------------------------------------------------------

        private static CallBuffer? SemSink() => Hook.Mode == Mode.Replay ? Hook.Sems : Active.Value;

        private static int? EmitSem(string name, string phase, object? data, int? sid, string? outcome)
        {
            var sink = SemSink();
            if (sink == null) return null;
            if (sid == null) sid = ++sink.Sid;
            var ev = new Dictionary<string, object?> { ["k"] = "sem", ["name"] = name, ["phase"] = phase, ["sid"] = (long)sid.Value };
            if (outcome != null) ev["outcome"] = outcome;
            if (data != null)
            {
                // sem.data MUST be an object (spec). An anonymous type or dictionary encodes to
                // one; an empty one is omitted, exactly as the Node recorder does.
                if (Serial.ToJsonable(data) is IDictionary<string, object?> m && m.Count > 0) ev["data"] = m;
            }
            sink.Events.Add(Scrub(ev));
            return sid;
        }

        /// <summary>Mark that something meaningful happened, at a point. A strict no-op when no
        /// recording is active for this call.</summary>
        public static void Note(string name, object? data = null) => EmitSem(name, "point", data, null, null);

        /// <summary>Record that a stretch of execution constituted a domain act, enclosing the raw
        /// events it produced. The `end` carries `outcome: "error"` when the body throws, and the
        /// exception propagates untouched. A no-op envelope when recording is off.</summary>
        public static T Span<T>(string name, object? data, Func<T> body)
        {
            var sid = EmitSem(name, "begin", data, null, null);
            if (sid == null) return body();
            try
            {
                var r = body();
                EmitSem(name, "end", null, sid, "ok");
                return r;
            }
            catch
            {
                EmitSem(name, "end", null, sid, "error");
                throw;
            }
        }

        public static T Span<T>(string name, Func<T> body) => Span(name, null, body);

        public static void Span(string name, object? data, Action body) =>
            Span<object?>(name, data, () => { body(); return null; });

        public static void Span(string name, Action body) => Span(name, null, body);

        public static async Task<T> SpanAsync<T>(string name, object? data, Func<Task<T>> body)
        {
            var sid = EmitSem(name, "begin", data, null, null);
            if (sid == null) return await body().ConfigureAwait(false);
            try
            {
                var r = await body().ConfigureAwait(false);
                EmitSem(name, "end", null, sid, "ok");
                return r;
            }
            catch
            {
                EmitSem(name, "end", null, sid, "error");
                throw;
            }
        }

        public static Task<T> SpanAsync<T>(string name, Func<Task<T>> body) => SpanAsync(name, null, body);

        // --- clock and randomness: called under both record and replay ------------------

        internal static void EmitClock(Dictionary<string, object?> ev) => Emit(ev);
        internal static bool Replaying => Hook.Mode == Mode.Replay;
        internal static Feed? ReplayFeed => Hook.Feed;
        internal static double MonoMs => Mono.Elapsed.TotalMilliseconds;
    }
}
