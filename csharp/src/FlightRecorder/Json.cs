// The JSONL substrate. A tape is JSON, so everything here speaks one internal value model:
//
//   null | bool | long | double | string | List<object?> | Dictionary<string, object?>
//
// It is deliberately the SAME model the recorder builds (see Serial.ToJsonable) and the
// replayer compares, so a value read off the tape and a value produced by the code meet as
// the same kind of thing. System.Text.Json is used only at the edges — to parse a line into
// the model and to render the model back to a line.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace FlightRecorder
{
    /// <summary>Parse and render the tape's value model. Compact output, no incidental whitespace.</summary>
    public static class Json
    {
        // --- parse ----------------------------------------------------------------------

        public static object? Parse(string text)
        {
            using var doc = JsonDocument.Parse(text);
            return FromElement(doc.RootElement);
        }

        public static object? FromElement(JsonElement el)
        {
            switch (el.ValueKind)
            {
                case JsonValueKind.Null:
                case JsonValueKind.Undefined:
                    return null;
                case JsonValueKind.True:
                    return true;
                case JsonValueKind.False:
                    return false;
                case JsonValueKind.String:
                    return el.GetString();
                case JsonValueKind.Number:
                    // Integral numbers become long, everything else double — so `seq`, `n`,
                    // `idx` read back as ints (what the validator wants) and `ms`, `perf.v`
                    // as floats. Mirrors how the Python and Node checkers see the same tape.
                    var raw = el.GetRawText();
                    if (el.TryGetInt64(out var l)
                        && raw.IndexOf('.') < 0 && raw.IndexOf('e') < 0 && raw.IndexOf('E') < 0)
                        return l;
                    return el.GetDouble();
                case JsonValueKind.Array:
                    var list = new List<object?>();
                    foreach (var item in el.EnumerateArray()) list.Add(FromElement(item));
                    return list;
                case JsonValueKind.Object:
                    var map = new Dictionary<string, object?>();
                    foreach (var prop in el.EnumerateObject()) map[prop.Name] = FromElement(prop.Value);
                    return map;
                default:
                    return null;
            }
        }

        // --- render ---------------------------------------------------------------------

        /// <summary>Compact JSON of the value model, keys in insertion order.</summary>
        public static string Stringify(object? v) => Write(v, sortKeys: false);

        /// <summary>Compact JSON with object keys sorted — for equality comparisons, where key
        /// order is not meaning. Two structurally-equal values render to the same string.</summary>
        public static string Canonical(object? v) => Write(v, sortKeys: true);

        private static string Write(object? v, bool sortKeys)
        {
            var sb = new StringBuilder();
            WriteInto(sb, v, sortKeys);
            return sb.ToString();
        }

        private static void WriteInto(StringBuilder sb, object? v, bool sortKeys)
        {
            switch (v)
            {
                case null:
                    sb.Append("null");
                    return;
                case bool b:
                    sb.Append(b ? "true" : "false");
                    return;
                case string s:
                    WriteString(sb, s);
                    return;
                case long l:
                    sb.Append(l.ToString(CultureInfo.InvariantCulture));
                    return;
                case int i:
                    sb.Append(i.ToString(CultureInfo.InvariantCulture));
                    return;
                case double d:
                    sb.Append(FormatDouble(d));
                    return;
                case float f:
                    sb.Append(FormatDouble(f));
                    return;
                case IDictionary<string, object?> map:
                    WriteObject(sb, map, sortKeys);
                    return;
                case IEnumerable<object?> arr:
                    WriteArray(sb, arr, sortKeys);
                    return;
                default:
                    // The value model should never contain anything else; if it does, its text
                    // is the honest fallback rather than a crash mid-tape.
                    WriteString(sb, v.ToString() ?? "");
                    return;
            }
        }

        private static void WriteObject(StringBuilder sb, IDictionary<string, object?> map, bool sortKeys)
        {
            sb.Append('{');
            IEnumerable<KeyValuePair<string, object?>> entries = map;
            if (sortKeys)
            {
                var sorted = new List<KeyValuePair<string, object?>>(map);
                sorted.Sort((a, b) => string.CompareOrdinal(a.Key, b.Key));
                entries = sorted;
            }
            var first = true;
            foreach (var kv in entries)
            {
                if (!first) sb.Append(',');
                first = false;
                WriteString(sb, kv.Key);
                sb.Append(':');
                WriteInto(sb, kv.Value, sortKeys);
            }
            sb.Append('}');
        }

        private static void WriteArray(StringBuilder sb, IEnumerable<object?> arr, bool sortKeys)
        {
            sb.Append('[');
            var first = true;
            foreach (var item in arr)
            {
                if (!first) sb.Append(',');
                first = false;
                WriteInto(sb, item, sortKeys);
            }
            sb.Append(']');
        }

        private static string FormatDouble(double d)
        {
            // Round-trippable, and integral doubles render without a trailing ".0" — matching
            // JSON.stringify, so a tape written here reads the same everywhere.
            if (double.IsNaN(d) || double.IsInfinity(d)) return "null";
            var r = d.ToString("R", CultureInfo.InvariantCulture);
            return r;
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (var ch in s)
            {
                switch (ch)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (ch < 0x20)
                            sb.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            sb.Append(ch);
                        break;
                }
            }
            sb.Append('"');
        }
    }
}
