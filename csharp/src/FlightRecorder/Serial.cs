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
using System.Text.RegularExpressions;

namespace FlightRecorder
{
    /// <summary>A redaction rule: a transform applied to the jsonable value, or <c>null</c> to mask.</summary>
    public delegate object? RedactTransform(object? value);

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

        /// <summary>Apply field-name redaction rules to a jsonable tree (see <see cref="Boundary.Redact"/>).
        /// A rule that throws degrades to <see cref="Redacted"/>: the failure direction is masked,
        /// never leaked and never breaks the recorded call.</summary>
        public static object? RedactJsonable(object? v, IReadOnlyDictionary<string, RedactTransform?>? rules)
        {
            if (rules == null || rules.Count == 0) return v;

            if (v is IDictionary<string, object?> map)
            {
                var outMap = new Dictionary<string, object?>();
                foreach (var kv in map)
                {
                    if (rules.TryGetValue(kv.Key, out var rule))
                    {
                        if (rule == null)
                        {
                            outMap[kv.Key] = Redacted;
                        }
                        else
                        {
                            try { outMap[kv.Key] = rule(kv.Value); }
                            catch { outMap[kv.Key] = Redacted; }
                        }
                    }
                    else
                    {
                        outMap[kv.Key] = RedactJsonable(kv.Value, rules);
                    }
                }
                return outMap;
            }
            if (v is IEnumerable<object?> list)
            {
                var outList = new List<object?>();
                foreach (var x in list) outList.Add(RedactJsonable(x, rules));
                return outList;
            }
            return v;
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
