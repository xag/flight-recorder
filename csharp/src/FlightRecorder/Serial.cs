// Boundary value (de)serialization — the .NET half of spec/tape-v1.md's "Value encoding".
//
// Everything crossing the recorded boundary is encoded into the JSON value model with
// revivable single-key markers for datetimes; anything exotic degrades to an opaque marker
// rather than breaking the recorded call. The failure direction is always "the recording is
// a bit poorer", never "the app broke because it was being recorded".

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;

namespace FlightRecorder
{
    /// <summary>A redaction rule: a transform applied to the jsonable value, or <c>null</c> to mask.</summary>
    public delegate object? RedactTransform(object? value);

    /// <summary>A value-level mask: handed every leaf string on the tape, whatever it is called and
    /// wherever it sits, and returns what should be written in its place.
    ///
    /// This exists because <see cref="RedactTransform"/> can only see what has a field name. A key
    /// built by interpolation, a positional argument, a token quoted mid-sentence in an email body —
    /// none of those have a name to key on, and before this the only recourse was
    /// <see cref="Boundary.Forbid"/>, which refuses to record the call at all. An assertion is not a
    /// remedy; this is the remedy.
    ///
    /// It MUST be idempotent. Replay re-derives the question the code is about to ask, scrubs it the
    /// same way, and compares the result against the tape — so a value that came OFF the tape is
    /// already a mask, and must scrub to itself. A scrub that masks its own output differently makes
    /// every recording it touches permanently unreplayable.</summary>
    public delegate string ScrubTransform(string value);

    /// <summary>A traced sequence that was too long to hold whole: the first
    /// <see cref="Serial.TraceMaxItems"/> items, and the length the original really had.
    ///
    /// It reports its true <see cref="Length"/> rather than the count of what survived, because
    /// an invariant asking "was the corpus empty?" must not be told yes by the truncation.</summary>
    public sealed class Truncated : IEnumerable<object?>
    {
        public IReadOnlyList<object?> Head { get; }
        public int Length { get; }

        public Truncated(IReadOnlyList<object?> head, int length) { Head = head; Length = length; }

        public IEnumerator<object?> GetEnumerator() => Head.GetEnumerator();
        IEnumerator IEnumerable.GetEnumerator() => Head.GetEnumerator();
        public override string ToString() => $"[{Head.Count} of {Length} items]";
    }

    /// <summary>A traced map that was too long to hold whole: its first
    /// <see cref="Serial.TraceMaxItems"/> entries, and the length the original really had. Like
    /// <see cref="Truncated"/>, it reports its true <see cref="Length"/>, so an invariant asking
    /// how many entries there were is not told the count of what survived.</summary>
    public sealed class TruncatedMap : Dictionary<string, object?>
    {
        public int Length { get; }

        public TruncatedMap(IEnumerable<KeyValuePair<string, object?>> head, int length)
        {
            foreach (var kv in head) this[kv.Key] = kv.Value;
            Length = length;
        }

        public override string ToString() => $"{{{Count} of {Length} entries}}";
    }

    /// <summary>A traced string cut to a prefix, still reporting its true length.</summary>
    public sealed class TruncatedText
    {
        public string Head { get; }
        public int Length { get; }

        public TruncatedText(string head, int length) { Head = head; Length = length; }

        public override string ToString() => Head;
    }

    public static class Serial
    {
        public const int MaxDepth = 16;

        /// <summary>What a masked field's value becomes under a bare (null) rule.</summary>
        public const string Redacted = "[REDACTED]";

        // Every single-key marker the value encoding uses. __undef__ exists for JavaScript,
        // which has two nothings; .NET (like Python) has one, revives it as null, and never
        // emits it. The reserved trace markers are tolerated on read, never produced here.
        private static readonly HashSet<string> Markers = new HashSet<string>
        {
            "__dt__", "__date__", "__undef__", "__opaque__",
        };

        private static readonly Regex Addr = new Regex(" at 0x[0-9A-Fa-f]+", RegexOptions.Compiled);

        // --- encode ---------------------------------------------------------------------

        /// <summary>Encode one boundary value into the tape's value model.</summary>
        public static object? ToJsonable(object? v) => ToJsonable(v, 0);

        private static object? ToJsonable(object? v, int depth)
        {
            if (depth > MaxDepth) return Opaque(v);
            switch (v)
            {
                case null:
                    return null;
                case bool b:
                    return b;
                case string s:
                    return s;
                case DateTimeOffset dto:
                    return new Dictionary<string, object?> { ["__dt__"] = Iso(dto) };
                case DateTime dt:
                    return new Dictionary<string, object?> { ["__dt__"] = IsoNaive(dt) };
            }

            var t = v.GetType();
            if (IsIntegral(t)) return Convert.ToInt64(v, CultureInfo.InvariantCulture);
            if (t == typeof(double) || t == typeof(float) || t == typeof(decimal))
            {
                var d = Convert.ToDouble(v, CultureInfo.InvariantCulture);
                return (double.IsNaN(d) || double.IsInfinity(d)) ? Opaque(v) : (object)d;
            }

            if (v is IDictionary dict)
            {
                var outMap = new Dictionary<string, object?>();
                foreach (DictionaryEntry e in dict)
                    outMap[Convert.ToString(e.Key, CultureInfo.InvariantCulture) ?? ""] = ToJsonable(e.Value, depth + 1);
                return outMap;
            }

            if (v is IEnumerable en)
            {
                var outList = new List<object?>();
                foreach (var x in en) outList.Add(ToJsonable(x, depth + 1));
                return outList;
            }

            // A plain DTO — the ordinary shape an effect returns, and an anonymous type is the
            // ordinary shape of sem/kwargs data. Record its public properties as the object a
            // consumer reads, not an opaque repr. Recurse through ToJsonable so a DateTime property
            // still gets its __dt__ marker, and camelCase the names so field-name redaction (declared
            // in the app's own lowercase vocabulary) reaches into it.
            return PocoOrOpaque(v, depth);
        }

        private static object? PocoOrOpaque(object v, int depth)
        {
            try
            {
                var props = v.GetType().GetProperties(
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance);
                var outMap = new Dictionary<string, object?>();
                foreach (var p in props)
                {
                    if (!p.CanRead || p.GetIndexParameters().Length > 0) continue;
                    object? val;
                    try { val = p.GetValue(v); }
                    catch { continue; }
                    outMap[CamelCase(p.Name)] = ToJsonable(val, depth + 1);
                }
                if (outMap.Count > 0) return outMap;
            }
            catch { /* fall through to opaque */ }
            return Opaque(v);
        }

        private static string CamelCase(string name) =>
            name.Length == 0 ? name : char.ToLowerInvariant(name[0]) + name.Substring(1);

        private static bool IsIntegral(Type t) =>
            t == typeof(int) || t == typeof(long) || t == typeof(short) || t == typeof(byte)
            || t == typeof(sbyte) || t == typeof(uint) || t == typeof(ulong) || t == typeof(ushort);

        private static Dictionary<string, object?> Opaque(object? v)
        {
            var repr = ReprOrPlaceholder(v);
            repr = Addr.Replace(repr, "");
            if (repr.Length > 200) repr = repr.Substring(0, 200);
            return new Dictionary<string, object?> { ["__opaque__"] = repr };
        }

        private static string ReprOrPlaceholder(object? v)
        {
            try
            {
                return v?.ToString() ?? "null";
            }
            catch (Exception e)
            {
                return $"<unreprable {v?.GetType().Name}: {e.GetType().Name}>";
            }
        }

        // --- decode ---------------------------------------------------------------------

        /// <summary>Revive a boundary value. <c>__opaque__</c> is a one-way door by design.</summary>
        public static object? FromJsonable(object? v)
        {
            if (v is IDictionary<string, object?> map)
            {
                if (map.Count == 1)
                {
                    foreach (var kv in map)
                    {
                        switch (kv.Key)
                        {
                            case "__dt__":
                                return ParseIso(kv.Value as string);
                            case "__date__":
                                return ParseIso(kv.Value as string);
                            // JavaScript has two nothings; .NET has one. A JS tape distinguishes
                            // undefined from null; reading it here, both are simply null.
                            case "__undef__":
                                return null;
                            case "__opaque__":
                                return kv.Value;
                        }
                    }
                }
                var outMap = new Dictionary<string, object?>();
                foreach (var kv in map) outMap[kv.Key] = FromJsonable(kv.Value);
                return outMap;
            }
            if (v is IEnumerable<object?> list)
            {
                var outList = new List<object?>();
                foreach (var x in list) outList.Add(FromJsonable(x));
                return outList;
            }
            return v;
        }

        // --- redaction ------------------------------------------------------------------

        /// <summary>Apply field-name redaction rules (see <see cref="Boundary.Redact"/>) and the
        /// value-level <paramref name="scrub"/> (see <see cref="Boundary.Scrub"/>) to a jsonable tree.
        ///
        /// The two are complements, not alternatives: rules replace what a name can find, the scrub
        /// sweeps every leaf string for what no name can. A rule's own OUTPUT is swept too, so a
        /// transform that shortens rather than masks cannot smuggle the secret past the sweep.
        ///
        /// Either failing degrades to <see cref="Redacted"/>: the failure direction is masked, never
        /// leaked and never breaks the recorded call. A null scrub means no sweep at all.</summary>
        public static object? RedactJsonable(object? v, IReadOnlyDictionary<string, RedactTransform?>? rules,
            ScrubTransform? scrub = null)
        {
            var hasRules = rules != null && rules.Count > 0;
            if (!hasRules && scrub == null) return v;

            if (v is IDictionary<string, object?> map)
            {
                var outMap = new Dictionary<string, object?>();
                foreach (var kv in map)
                {
                    if (hasRules && rules!.TryGetValue(kv.Key, out var rule))
                    {
                        if (rule == null)
                        {
                            outMap[kv.Key] = Redacted;
                        }
                        else
                        {
                            try { outMap[kv.Key] = Sweep(rule(kv.Value), scrub); }
                            catch { outMap[kv.Key] = Redacted; }
                        }
                    }
                    else
                    {
                        outMap[kv.Key] = RedactJsonable(kv.Value, rules, scrub);
                    }
                }
                return outMap;
            }
            if (v is IEnumerable<object?> list)
            {
                var outList = new List<object?>();
                foreach (var x in list) outList.Add(RedactJsonable(x, rules, scrub));
                return outList;
            }
            return Sweep(v, scrub);
        }

        /// <summary>Hand one leaf to the scrub. A scrub that throws masks the value outright rather
        /// than passing it through — the one thing worse than a poorer recording is a recording that
        /// leaks because the mask crashed.</summary>
        private static object? Sweep(object? v, ScrubTransform? scrub)
        {
            if (scrub == null || !(v is string s)) return v;
            try { return scrub(s); }
            catch { return Redacted; }
        }

        /// <summary>The first forbid pattern that matches the serialized line, or null if clean.
        /// Returns the PATTERN, never the match — a tripwire that quoted the credential it caught
        /// would be the leak it exists to prevent.</summary>
        public static string? ForbiddenHit(string text, IReadOnlyList<Regex> patterns)
        {
            foreach (var p in patterns)
                if (p.IsMatch(text)) return p.ToString();
            return null;
        }

        // --- snapshots ------------------------------------------------------------------

        /// <summary>A document snapshot in the shape a well-behaved consumer reads: identity,
        /// existence, data.</summary>
        public static Dictionary<string, object?> SnapshotJsonable(string? id, bool exists, object? data) =>
            new Dictionary<string, object?>
            {
                ["id"] = id,
                ["exists"] = exists,
                ["data"] = exists ? ToJsonable(data) : null,
            };

        // --- rendering (chain signatures) -----------------------------------------------

        /// <summary>Compact stable rendering of a chain-call argument for signatures.</summary>
        public static string Short(object? v, int limit = 60)
        {
            string s;
            try { s = Json.Stringify(ToJsonable(v)); }
            catch { s = ReprOrPlaceholder(v); }
            return s.Length <= limit ? s : s.Substring(0, limit - 1) + "…";
        }

        // --- traced internal values -----------------------------------------------------
        //
        // A different problem from boundary values, and it gets a different encoder. A boundary
        // value is an INPUT to be revived faithfully; a traced value is a CLAIM to be asserted
        // against. It is captured on every executed line, and it is whatever object the code
        // happened to be holding — so it is recorded as data (you cannot do arithmetic on
        // "3", and `FlightRecorder.Toy.Doc` tells you nothing), document snapshots are
        // unwrapped, and anything long is cut to a prefix that still reports its true length.

        /// <summary>Caps for traced values. A local can be a 10k-row list, and the tracer
        /// snapshots it on every line that touches its frame.</summary>
        public const int TraceMaxItems = 100;
        public const int TraceMaxChars = 512;

        /// <summary>Encode one traced internal value.
        ///
        /// This must NEVER throw. It runs inside the trace hook, on a line of the code under
        /// replay — an exception here would be raised *in the frame being observed*, corrupting
        /// the very execution the trace exists to watch. Anything hostile (a property getter
        /// that throws, a cyclic graph, a ToString that dies) degrades to an opaque marker.</summary>
        public static object? TraceJsonable(object? v)
        {
            try { return TraceEncode(v, 0, new TraceWalk()); }
            catch { return Opaque(v); }
        }

        /// <summary>The nodes one traced value may encode to, over every level. Each level is capped
        /// at <see cref="TraceMaxItems"/>, but a hundred of a hundred of a hundred is a million, and
        /// an object graph fans out faster than a depth cap can see: one budget over the whole value
        /// bounds the cost of a local on an executed line to a constant, whatever it holds. Past
        /// it, a value is <c>{"__opaque__": "&lt;Type (beyond the trace budget)&gt;"}</c>.</summary>
        public const int TraceMaxNodes = 2000;

        /// <summary>The state of encoding one top-level traced value: the nodes it has left, and the
        /// objects on the path being encoded (a cycle guard). One per call, never shared, so frames
        /// traced on two threads at once do not spend each other's budget.</summary>
        private sealed class TraceWalk
        {
            public int Budget = TraceMaxNodes;
            public readonly HashSet<object> Path = new HashSet<object>(SameObject.Instance);
        }

        private sealed class SameObject : IEqualityComparer<object>
        {
            public static readonly SameObject Instance = new SameObject();
            public new bool Equals(object? x, object? y) => ReferenceEquals(x, y);
            public int GetHashCode(object o) => System.Runtime.CompilerServices.RuntimeHelpers.GetHashCode(o);
        }

        private static Dictionary<string, object?> TraceMarker(object? v, string why) =>
            new Dictionary<string, object?> { ["__opaque__"] = $"<{(v == null ? "null" : ArgName(v.GetType()))} ({why})>" };

        private static object? TraceEncode(object? v, int depth, TraceWalk walk)
        {
            if (depth > MaxDepth) return Opaque(v);
            if (--walk.Budget < 0) return TraceMarker(v, "beyond the trace budget");
            switch (v)
            {
                case null:
                    return null;
                case bool b:
                    return b;
                case string s:
                    return s.Length <= TraceMaxChars
                        ? (object)s
                        : new Dictionary<string, object?>
                        {
                            ["__str__"] = new Dictionary<string, object?> { ["len"] = (long)s.Length, ["head"] = s.Substring(0, TraceMaxChars) },
                        };
                case DateTimeOffset dto:
                    return new Dictionary<string, object?> { ["__dt__"] = Iso(dto) };
                case DateTime dt:
                    return new Dictionary<string, object?> { ["__dt__"] = IsoNaive(dt) };
                case Snapshot snap:
                    return new Dictionary<string, object?>
                    {
                        ["__snap__"] = SnapshotJsonable(snap.Id, snap.Exists, snap.Data),
                    };
            }

            var reference = ProcessReference(v) ?? AwaitableReference(v);
            if (reference != null) return reference;

            var t = v.GetType();
            if (IsIntegral(t)) return Convert.ToInt64(v, CultureInfo.InvariantCulture);
            if (t == typeof(double) || t == typeof(float) || t == typeof(decimal))
            {
                var d = Convert.ToDouble(v, CultureInfo.InvariantCulture);
                return (double.IsNaN(d) || double.IsInfinity(d)) ? Opaque(v) : (object)d;
            }
            if (t.IsEnum) return v.ToString();

            // A graph that refers back to itself is written once, then as a cycle marker. The guard
            // is the path being encoded, not every object seen: a value reached twice by two routes
            // is written twice. A value type is a copy and cannot hold itself.
            if (!t.IsValueType && !walk.Path.Add(v)) return TraceMarker(v, "cycle");
            try
            {
                return TraceContainer(v, depth, walk);
            }
            finally
            {
                if (!t.IsValueType) walk.Path.Remove(v);
            }
        }

        private static object? TraceContainer(object v, int depth, TraceWalk walk)
        {
            if (v is IDictionary dict)
            {
                // A map is cut like a sequence: a rank map of thousands of entries, encoded whole on
                // every executed line, is the cost the per-level cap exists to bound.
                var outMap = new Dictionary<string, object?>();
                var n = 0;
                foreach (DictionaryEntry e in dict)
                {
                    if (n++ >= TraceMaxItems) break;
                    outMap[Convert.ToString(e.Key, CultureInfo.InvariantCulture) ?? ""] = TraceEncode(e.Value, depth + 1, walk);
                }
                if (dict.Count > TraceMaxItems)
                    return new Dictionary<string, object?>
                    {
                        ["__map__"] = new Dictionary<string, object?> { ["len"] = (long)dict.Count, ["head"] = outMap },
                    };
                // A traced dict that happens to have exactly one key spelled like a marker would
                // revive as the thing the marker means. Escape it so it revives as itself.
                if (outMap.Count == 1)
                {
                    foreach (var k in outMap.Keys)
                        if (Markers.Contains(k) || TraceMarkers.Contains(k))
                            return new Dictionary<string, object?> { ["__esc__"] = outMap };
                }
                return outMap;
            }

            if (v is IEnumerable en)
            {
                var items = new List<object?>();
                var n = 0;
                foreach (var x in en)
                {
                    if (n < TraceMaxItems) items.Add(TraceEncode(x, depth + 1, walk));
                    n += 1;
                }
                if (n <= TraceMaxItems) return items;
                return new Dictionary<string, object?>
                {
                    ["__seq__"] = new Dictionary<string, object?> { ["len"] = (long)n, ["head"] = items },
                };
            }

            return TracePocoOrOpaque(v, depth, walk);
        }

        /// <summary>A handle on the process or on code, in a traced frame, written as its name.
        ///
        /// The trace walks an object through its public properties, and these objects are not
        /// values the execution computed. A <see cref="System.Diagnostics.ProcessStartInfo"/> fills
        /// its <c>EnvironmentVariables</c> from the whole environment the first time the property
        /// is read, so walking one would fetch every credential the process holds, which nobody
        /// handed the trace. A <see cref="Type"/>, an <see cref="System.Reflection.Assembly"/>, a
        /// <see cref="System.Reflection.MemberInfo"/> or a delegate opens onto the reflection graph,
        /// whose walk does not finish: a frame holding <c>typeof(string)</c> or a lambda stopped the
        /// traced run. The name only, never a location: an assembly's path names someone's disk.
        /// Null when the value is not one of these.</summary>
        private static Dictionary<string, object?>? ProcessReference(object v)
        {
            string? name;
            switch (v)
            {
                case Type type:
                    name = $"<type '{Named(() => type.FullName ?? type.Name)}'>";
                    break;
                case System.Reflection.MemberInfo member:
                    name = $"<{Named(() => member.MemberType.ToString().ToLowerInvariant())} '"
                        + $"{Named(() => (member.DeclaringType?.FullName ?? "?") + "." + member.Name)}'>";
                    break;
                case Delegate del:
                    name = $"<delegate '{Named(() => (del.Method.DeclaringType?.FullName ?? "?") + "." + del.Method.Name)}'>";
                    break;
                case System.Reflection.Assembly assembly:
                    name = $"<assembly '{Named(() => assembly.GetName().Name ?? "?")}'>";
                    break;
                case System.Reflection.Module module:
                    name = $"<module '{Named(() => module.ScopeName)}'>";
                    break;
                case AppDomain _:
                    name = "<AppDomain>";
                    break;
                case System.Diagnostics.Process _:
                    name = "<Process>";
                    break;
                case System.Diagnostics.ProcessStartInfo _:
                    name = "<ProcessStartInfo>";
                    break;
                case System.Threading.Thread _:
                    name = "<Thread>";
                    break;
                default:
                    return null;
            }
            return new Dictionary<string, object?> { ["__opaque__"] = name };
        }

        private static string Named(Func<string> read)
        {
            try { return read(); }
            catch { return "?"; }
        }

        /// <summary>An awaitable in a traced frame, written as its name and its status, never its
        /// result.
        ///
        /// Walked as an object, a pending <c>Task&lt;T&gt;</c> was read through its public
        /// <c>Result</c>, which blocks until the task completes: the trace hook, running on the
        /// traced code's own line, stopped the code it was observing, or deadlocked it. A
        /// <c>ValueTask&lt;T&gt;</c> over an <c>IValueTaskSource</c> is worse: reading its result
        /// consumes the operation, so the traced code would later await a result that was already
        /// taken. A Task is named as the Task or <c>Task&lt;T&gt;</c> it is (an async method's task
        /// is a runtime subclass) with its <see cref="System.Threading.Tasks.TaskStatus"/>; a
        /// ValueTask with the status its <c>Is*</c> flags give, or <c>Pending</c>. Any other type
        /// with a public <c>GetAwaiter()</c> is a name only: its awaiter is never asked for, since
        /// asking can start the work. Python does not trace an awaitable's result either.
        /// ValueTask is recognised by name, so the netstandard2.0 build needs no package for it.
        /// Null when the value is not awaitable.</summary>
        private static Dictionary<string, object?>? AwaitableReference(object v)
        {
            string? name = null;
            if (v is System.Threading.Tasks.Task task)
            {
                name = $"<{TaskName(task.GetType())} {Named(() => task.Status.ToString())}>";
            }
            else
            {
                var t = v.GetType();
                var def = t.IsGenericType ? t.GetGenericTypeDefinition() : t;
                if (def.FullName == "System.Threading.Tasks.ValueTask"
                    || def.FullName == "System.Threading.Tasks.ValueTask`1")
                {
                    var label = t.IsGenericType ? $"ValueTask<{ArgName(t.GetGenericArguments()[0])}>" : "ValueTask";
                    name = $"<{label} {Named(() => ValueTaskStatus(v, t))}>";
                }
                else if (HasGetAwaiter(t))
                {
                    name = $"<{ArgName(t)}>";
                }
            }
            return name == null ? null : new Dictionary<string, object?> { ["__opaque__"] = name };
        }

        /// <summary><c>Task</c>, or <c>Task&lt;T&gt;</c> for the nearest generic Task in its
        /// ancestry.</summary>
        private static string TaskName(Type t)
        {
            for (var b = t; b != null; b = b.BaseType)
                if (b.IsGenericType && b.GetGenericTypeDefinition() == typeof(System.Threading.Tasks.Task<>))
                    return $"Task<{ArgName(b.GetGenericArguments()[0])}>";
            return "Task";
        }

        private static string ArgName(Type t)
        {
            if (!t.IsGenericType) return t.Name;
            var tick = t.Name.IndexOf('`');
            var bare = tick < 0 ? t.Name : t.Name.Substring(0, tick);
            return $"{bare}<{string.Join(",", t.GetGenericArguments().Select(ArgName))}>";
        }

        /// <summary>A ValueTask's status from its <c>Is*</c> flags, which ask a source for its
        /// status and never for its result.</summary>
        private static string ValueTaskStatus(object v, Type t)
        {
            bool Flag(string property) =>
                t.GetProperty(property)?.GetValue(v) is bool b && b;
            if (Flag("IsCompletedSuccessfully")) return "RanToCompletion";
            if (Flag("IsCanceled")) return "Canceled";
            if (Flag("IsFaulted")) return "Faulted";
            return "Pending";
        }

        private static bool HasGetAwaiter(Type t)
        {
            try
            {
                return t.GetMethod("GetAwaiter", System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance,
                    null, Type.EmptyTypes, null) != null;
            }
            catch { return false; }
        }

        private static object? TracePocoOrOpaque(object v, int depth, TraceWalk walk)
        {
            try
            {
                var props = v.GetType().GetProperties(
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance);
                var outMap = new Dictionary<string, object?>();
                foreach (var p in props)
                {
                    if (!p.CanRead || p.GetIndexParameters().Length > 0) continue;
                    object? val;
                    try { val = p.GetValue(v); }
                    catch { continue; }
                    outMap[CamelCase(p.Name)] = TraceEncode(val, depth + 1, walk);
                }
                if (outMap.Count > 0) return outMap;
            }
            catch { /* fall through to opaque */ }
            return Opaque(v);
        }

        private static readonly HashSet<string> TraceMarkers = new HashSet<string>
        {
            "__str__", "__seq__", "__map__", "__snap__", "__esc__",
        };

        /// <summary>Revive a traced value into something an invariant can assert on.</summary>
        public static object? FromTraceJsonable(object? v)
        {
            if (v is IDictionary<string, object?> map)
            {
                if (map.Count == 1)
                {
                    foreach (var kv in map)
                    {
                        switch (kv.Key)
                        {
                            case "__dt__":
                            case "__date__":
                                return ParseIso(kv.Value as string);
                            case "__opaque__":
                                return kv.Value;
                            case "__undef__":
                                return null;
                            case "__snap__":
                                return FromTraceJsonable(kv.Value);
                            case "__seq__":
                                {
                                    var spec = kv.Value as IDictionary<string, object?>;
                                    var head = (spec?.GetValueOrNull("head") as IEnumerable<object?>
                                                ?? Enumerable.Empty<object?>()).Select(FromTraceJsonable).ToList();
                                    return new Truncated(head, ToLen(spec?.GetValueOrNull("len")));
                                }
                            case "__map__":
                                {
                                    var spec = kv.Value as IDictionary<string, object?>;
                                    var head = spec?.GetValueOrNull("head") as IDictionary<string, object?>
                                               ?? new Dictionary<string, object?>();
                                    return new TruncatedMap(
                                        head.Select(e => new KeyValuePair<string, object?>(e.Key, FromTraceJsonable(e.Value))),
                                        ToLen(spec?.GetValueOrNull("len")));
                                }
                            case "__str__":
                                {
                                    var spec = kv.Value as IDictionary<string, object?>;
                                    return new TruncatedText(spec?.GetValueOrNull("head") as string ?? "",
                                        ToLen(spec?.GetValueOrNull("len")));
                                }
                            case "__esc__":
                                {
                                    var inner = kv.Value as IDictionary<string, object?>;
                                    var un = new Dictionary<string, object?>();
                                    if (inner != null)
                                        foreach (var e in inner) un[e.Key] = FromTraceJsonable(e.Value);
                                    return un;
                                }
                        }
                    }
                }
                var outMap = new Dictionary<string, object?>();
                foreach (var kv in map) outMap[kv.Key] = FromTraceJsonable(kv.Value);
                return outMap;
            }
            if (v is IEnumerable<object?> list)
                return list.Select(FromTraceJsonable).ToList();
            return v;
        }

        private static int ToLen(object? v) => v is long l ? (int)l : v is int i ? i : 0;

        /// <summary>One-line display of a traced value, for a timeline or a failure message.</summary>
        public static string RenderTraced(object? v, int limit = 90)
        {
            string s;
            try { s = Json.Stringify(v); }
            catch { s = ReprOrPlaceholder(v); }
            return s.Length <= limit ? s : s.Substring(0, limit - 1) + "…";
        }

        // --- timestamps -----------------------------------------------------------------

        /// <summary>ISO-8601 with the local UTC offset — what the tape wants for aware metadata.</summary>
        public static string Iso(DateTimeOffset d) =>
            d.ToString("yyyy-MM-ddTHH:mm:ss.ffffffzzz", CultureInfo.InvariantCulture);

        /// <summary>ISO-8601 preserving the value's awareness. A DateTime with an offset renders
        /// aware; an unspecified/local one renders naive — because for an app-visible clock value
        /// the awareness is part of the value (see the `now` event in the spec).</summary>
        public static string IsoNaive(DateTime d)
        {
            if (d.Kind == DateTimeKind.Utc)
                return d.ToString("yyyy-MM-ddTHH:mm:ss.ffffff", CultureInfo.InvariantCulture) + "Z";
            return d.ToString("yyyy-MM-ddTHH:mm:ss.ffffff", CultureInfo.InvariantCulture);
        }

        internal static object ParseIso(string? s)
        {
            if (s == null) return "";
            if (DateTimeOffset.TryParse(s, CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind, out var dto)
                && (s.EndsWith("Z") || s.Contains("+") || HasNegativeOffset(s)))
                return dto;
            if (DateTime.TryParse(s, CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind, out var dt))
                return dt;
            return s;
        }

        private static bool HasNegativeOffset(string s)
        {
            // A trailing -HH:MM offset (not the date's own dashes).
            var t = s.Length >= 6 ? s.Substring(s.Length - 6) : s;
            return t.Length == 6 && t[0] == '-' && t[3] == ':';
        }
    }
}
