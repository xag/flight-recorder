// Stock declarations an app may hand its Boundary: credentials, masked by shape and by field.
//
// The recorder knows nothing about what any value means, and nothing here changes that: no
// recording consults this class unless the app declares it, exactly as it declares any other
// scrub or forbid pattern. It ships beside the recorder because every app that records a
// configuration file, an environment or a conversation transcript meets the same credentials.
//
//     var boundary = new Boundary()
//         .Scrubbing(Scrubs.Credentials)              // mask every credential, wherever it sits
//         .Forbidden(Scrubs.CredentialShapes);        // and refuse a line where one survived
//
// An app whose own fields are named like credentials and hold none (a `key` naming where a client
// keeps its servers, a `token_env` naming a variable) says so with `exempt`:
//
//     .Scrubbing(s => Scrubs.Credentials(s, "key", "token_env"))
//
// `Credentials` masks TEXT: in any string, every credential shape (an `sk-` key, a GitHub or
// GitLab token, an AWS access key id, a Slack token, a JWT, a Bearer or Basic credential, a
// password in a URL, a secret in a query string), and the value of every field whose name says it
// holds a credential: in JSON, YAML or TOML text; in such text held inside a JSON string at any
// depth (`\"KEY\": \"value\"`, which is how a Claude Code transcript holds a file a tool read);
// and in unquoted assignments (`KEY=value`, `export KEY=value`, `set KEY=value`,
// `$env:KEY = value`, `--password=value`). `CredentialsData` masks what that text
// parses to, and the two agree: parsing masked text gives the masked parse.
//
// A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the sha256 of the value's UTF-8:
// deterministic, so a replay re-derives the same mask, and distinct, so two different keys stay
// two different masks. Both are idempotent (a mask is never masked again), which Boundary.Scrub
// requires. A value under a credential field is kept when it is empty, a variable reference
// (`${TOKEN}`, `{env:TOKEN}`, `%TOKEN%`) or already a mask.
//
// The patterns are ASCII, as in the Python reference (which compiles with re.ASCII): .NET's \w,
// \s, \b and IgnoreCase are Unicode-aware, so none of them appears below. Word characters are
// spelled [A-Za-z0-9_], white space [ \t\n\r\f\v], and case-insensitive names letter by letter.
// That is what makes every runtime mask the same text to the same bytes
// (spec/fixtures/credentials.json).

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace FlightRecorder
{
    public static class Scrubs
    {
        private const RegexOptions Options = RegexOptions.Compiled | RegexOptions.CultureInvariant;

        private const string Word = "A-Za-z0-9_";
        private const string Space = @" \t\n\r\f\v";

        /// <summary>What a masked value looks like: <c>&lt;secret:</c>, 8 lowercase hex, <c>&gt;</c>.</summary>
        public static readonly Regex Mask = new Regex("<secret:[0-9a-f]{8}>", Options);

        // A mask, and a variable reference, starting exactly at the index a match is asked at.
        private static readonly Regex MaskAt = new Regex(@"\G<secret:[0-9a-f]{8}>", Options);

        private static readonly Regex ReferenceAt = new Regex(
            $@"\G(?:\$\{{(?:env:)?[{Word}]+\}}|\{{env:[{Word}]+\}}|%[{Word}]+%)", Options);

        // A field whose name says it holds a credential, matched anywhere in the name, ASCII case
        // folded: key (not keyword(s) or keyboard), secret, token, passw, auth (not author, but
        // authorization), credential, cookie, session, bearer.
        private static readonly Regex SecretFieldName = new Regex(
            Ci("key") + "(?!" + Ci("word") + Ci("s") + "?|" + Ci("board") + ")"
            + "|" + Ci("secret") + "|" + Ci("token") + "|" + Ci("passw")
            + "|" + Ci("auth") + "(?!" + Ci("or") + "(?!" + Ci("i") + "))"
            + "|" + Ci("credential") + "|" + Ci("cookie") + "|" + Ci("session") + "|" + Ci("bearer"),
            Options);

        // A variable reference names a secret without holding it.
        private static readonly Regex Reference = new Regex(
            $@"\A(?:\$\{{(?:env:)?[{Word}]+\}}|\{{env:[{Word}]+\}}|%[{Word}]+%|\$(?:env:)?[{Word}]+)\z", Options);

        /// <summary>Credential shapes, wherever they appear, as .NET patterns. Group <c>v</c> is the
        /// part masked; the rest is context that says it is a credential (<c>Bearer </c>,
        /// <c>://user:</c>…<c>@</c>, <c>?api_key=</c>). Hand them to
        /// <see cref="Boundary.Forbidden(IEnumerable{string})"/> as the tripwire behind the scrub.</summary>
        public static IReadOnlyList<string> CredentialShapes { get; } = new[]
        {
            $@"(?<![{Word}-])(?<v>sk-(?:ant-)?[A-Za-z0-9_-]{{20,}})",
            $@"(?<![{Word}-])(?<v>gh[pousr]_[A-Za-z0-9]{{20,}})",
            $@"(?<![{Word}-])(?<v>github_pat_[A-Za-z0-9_]{{20,}})",
            $@"(?<![{Word}-])(?<v>glpat-[A-Za-z0-9_-]{{20,}})",
            @"(?<![A-Za-z0-9])(?<v>AKIA[0-9A-Z]{16})(?![A-Za-z0-9])",
            $@"(?<![{Word}-])(?<v>xox[abpr]-[A-Za-z0-9-]{{10,}})",
            $@"(?<![{Word}-])(?<v>eyJ[A-Za-z0-9_-]{{10,}}\.[A-Za-z0-9_-]{{10,}}\.[A-Za-z0-9_-]{{10,}})",
            // with a digit in it: "use a Bearer token" is prose, not a credential. The word
            // boundary before Bearer is spelled as "no word character before it".
            $@"(?<![{Word}])(?:Bearer|Basic)[{Space}]+(?<v>(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{{8,}})",
            // a password in a URL, unless it is a reference
            $@"://[^:@/{Space}""'<>\\]+:(?<v>(?![$%{{])[^:@/{Space}""'<>\\]+)@",
            $@"[?&][{Word}-]*(?:key|secret|token|passw|auth|session|sig)[{Word}-]*=(?<v>[^&{Space}""'<>#\\]+)",
        };

        private static readonly Regex[] Shapes = CredentialShapes.Select(p => new Regex(p, Options)).ToArray();

        // A `key: "value"` (JSON, YAML) or `key = "value"` (TOML) pair in text. A quoted key does
        // not open at an escaped quote: in a long string of escaped JSON every `\"` could otherwise
        // open a token running to the string's end, and the scan took time quadratic in its length.
        private static readonly string PairPattern =
            $@"(?<k>(?<!\\)""(?:[^""\\\n]|\\.)*""|'[^'\n]*'|(?<![{Word}.-])[A-Za-z0-9_.-]+)"
            + $@"(?<sep>[{Space}]*[:=][{Space}]*)"
            + $@"(?<v>""(?:[^""\\\n]|\\.)*""|'[^'\n]*')";

        private static readonly Regex Pair = new Regex(PairPattern, Options);
        private static readonly Regex PairAt = new Regex(@"\G(?:" + PairPattern + ")", Options);

        // The same pair written inside a JSON string, at any depth: `\"KEY\": \"value\"` is a JSON
        // document held in a string (a tool result in a Claude Code transcript), `\\\"KEY\\\": ...`
        // that string held in another. All four quotes carry the same run of backslashes. Key and
        // value hold no quote, backslash or line break, so the value in the text is the value in
        // its parse, and both mask to the same `<secret:...>`. Group q2 is the value's backslash
        // run, where the scan tries the next pair when this one is left as it is.
        private static readonly string EscapedPairPattern =
            @"(?<!\\)(?<q>\\+)""(?<k>[^""\\\n]+)\k<q>""(?<sep>[ \t]*[:=][ \t]*)"
            + @"(?<q2>\k<q>)""(?<v>[^""\\\n]*)\k<q>""";

        private static readonly Regex EscapedPair = new Regex(EscapedPairPattern, Options);
        private static readonly Regex EscapedPairAt = new Regex(@"\G(?:" + EscapedPairPattern + ")", Options);

        // ...and with a bare key: `KEY=\"value\"`, `password: \"value\"`, a dotenv or YAML line
        // held in a JSON string.
        private static readonly Regex EscapedValue = new Regex(
            $@"(?<![{Word}.-])(?<k>[A-Za-z0-9_.-]+)(?<sep>[ \t]*[:=][ \t]*)"
            + @"(?<q>\\+)""(?<v>[^""\\\n]*)\k<q>""",
            Options);

        // An unquoted assignment: `KEY=value`, `export KEY=value`, `set KEY=value`,
        // `$env:KEY = value`, a .env line, a `--password=...` flag. The value runs to white space
        // or the end of the line, and stops at a quote, a backslash (where a JSON escape such as
        // `\n` begins), or `&;|<>,)}]`. A value starting with `=` is a comparison (`token == x`),
        // not an assignment. Quoted values are the pair's (`KEY="value"`).
        private static readonly Regex Assign = new Regex(
            $@"(?<![{Word}.-])(?<k>[A-Za-z0-9_.-]+)(?<sep>[ \t]*=[ \t]*)"
            + $@"(?<v>(?!=)[^{Space}""'`\\&;|<>,)}}\]]+)",
            Options);

        /// <summary>The mask of one value: <c>&lt;secret:</c> + the first 8 hex of the sha256 of
        /// its UTF-8 + <c>&gt;</c>.</summary>
        public static string Secret(string value)
        {
            byte[] hash;
            using (var sha = SHA256.Create()) hash = sha.ComputeHash(Encoding.UTF8.GetBytes(value));
            var hex = new StringBuilder(8);
            for (var i = 0; i < 4; i++) hex.Append(hash[i].ToString("x2", CultureInfo.InvariantCulture));
            return "<secret:" + hex + ">";
        }

        /// <summary>Whether a field of this name holds a credential, unless the app exempted the
        /// name (an exact match).</summary>
        public static bool SecretField(string name, IEnumerable<string>? exempt = null) =>
            IsSecret(name, new HashSet<string>(exempt ?? Array.Empty<string>(), StringComparer.Ordinal));

        // A name holding a mask is a value an earlier pass masked, read back as the key of the
        // next pair (`token="<secret:...>": "v"`); `secret` in the mask is not the field saying
        // anything.
        private static bool IsSecret(string name, HashSet<string> exempt) =>
            !exempt.Contains(name) && !Mask.IsMatch(name) && SecretFieldName.IsMatch(name);

        /// <summary><paramref name="text"/> with every credential field's value and every
        /// credential shape masked. Its signature is a <see cref="ScrubTransform"/>'s, so the
        /// method group is a scrub: <c>new Boundary().Scrubbing(Scrubs.Credentials)</c>.</summary>
        public static string Credentials(string text) => Credentials(text, (IEnumerable<string>)Array.Empty<string>());

        /// <summary><see cref="Credentials(string)"/>, leaving the values of the fields named in
        /// <paramref name="exempt"/> alone.</summary>
        public static string Credentials(string text, params string[] exempt) =>
            Credentials(text, (IEnumerable<string>)exempt);

        /// <summary><see cref="Credentials(string)"/>, leaving the values of the fields named in
        /// <paramref name="exempt"/> alone.</summary>
        public static string Credentials(string text, IEnumerable<string> exempt)
        {
            var names = new HashSet<string>(exempt ?? Array.Empty<string>(), StringComparer.Ordinal);
            // The passes run until the text stops changing. A later pass can change how an earlier
            // one reads the text: a shape or an assignment masks the key of a pair that had
            // swallowed the next pair's opening quote, and the next round reads that pair.
            // Repeating makes the result a fixed point, so a second scrub is a no-op by
            // construction (a replay re-scrubs what it read off the tape and must find it
            // unchanged). It ends: every round that changes the text replaces at least one
            // character outside the masks with a mask, and a mask is never masked again. Text that
            // holds no credential costs one round.
            while (true)
            {
                var next = Round(text, names);
                if (string.Equals(next, text, StringComparison.Ordinal)) return next;
                text = next;
            }
        }

        /// <summary>One round of the five passes: pair, escaped pair, escaped value, assignments,
        /// shapes.</summary>
        private static string Round(string text, HashSet<string> names)
        {
            var paired = Chained(Pair, PairAt, m =>
            {
                var k = m.Groups["k"].Value;
                // A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
                var name = k[0] == '"' || k[0] == '\'' ? Unquote(k) : LastPart(k);
                var token = m.Groups["v"].Value;
                var value = Unquote(token);
                if (!IsSecret(name, names) || Keeps(value)) return null;
                var quote = token[0];
                return k + m.Groups["sep"].Value + quote + Secret(value) + quote;
            }, text, "v");
            var escaped = Chained(EscapedPair, EscapedPairAt, m =>
            {
                string k = m.Groups["k"].Value, value = m.Groups["v"].Value, q = m.Groups["q"].Value;
                if (!IsSecret(k, names) || Keeps(value)) return null;
                return q + "\"" + k + q + "\"" + m.Groups["sep"].Value + q + "\"" + Secret(value) + q + "\"";
            }, paired, "q2");
            var escapedValues = EscapedValue.Replace(escaped, m =>
            {
                string k = m.Groups["k"].Value, value = m.Groups["v"].Value, q = m.Groups["q"].Value;
                if (!IsSecret(LastPart(k), names) || Keeps(value)) return m.Value;
                return k + m.Groups["sep"].Value + q + "\"" + Secret(value) + q + "\"";
            });
            return MaskShapes(Assignments(escapedValues, names));
        }

        /// <summary><c>search.Replace(text, mask)</c>, except that a pair left as it is (<paramref
        /// name="mask"/> returns null) gives its value back when that value opens a pair of its own.
        ///
        /// In <c>x = "api_key": "v1"</c> the pair <c>x = "api_key"</c> is not a credential, and a
        /// scan that resumed after its value never saw <c>"api_key": "v1"</c>. So the scan tries
        /// one pair anchored at the value's start (<paramref name="anchored"/> is the same pattern
        /// behind <c>\G</c>), and only if none opens there does it skip the value whole: resuming
        /// inside a quoted value would read its escaped quotes as the openings of new tokens.
        /// <paramref name="valueGroup"/> names the group where the value's token begins.</summary>
        private static string Chained(Regex search, Regex anchored, Func<Match, string?> mask, string text,
            string valueGroup)
        {
            var outp = new StringBuilder(text.Length);
            int pos = 0, skip = 0;
            int? anchor = null;
            while (true)
            {
                var m = anchor.HasValue ? anchored.Match(text, anchor.Value) : search.Match(text, pos);
                if (!m.Success)
                {
                    if (anchor.HasValue)
                    {
                        anchor = null;
                        outp.Append(text, pos, skip - pos);
                        pos = skip;
                        continue;
                    }
                    outp.Append(text, pos, text.Length - pos);
                    return outp.ToString();
                }
                anchor = null;
                var replaced = mask(m);
                if (replaced == null)
                {
                    var start = m.Groups[valueGroup].Index;
                    outp.Append(text, pos, start - pos);
                    pos = start;
                    anchor = start;
                    skip = m.Index + m.Length;
                    continue;
                }
                outp.Append(text, pos, m.Index - pos).Append(replaced);
                pos = m.Index + m.Length;
            }
        }

        /// <summary>The unquoted assignments masked, scanning left to right.
        ///
        /// Unlike the other passes, an assignment left as it is gives its value back to the scan:
        /// the search resumes right after its name, not after its value. In <c>a=token=x</c> the
        /// value of <c>a</c> holds an assignment of its own, and in <c>AKIA...=Bearer = v</c> a
        /// shape masked later turns the first name into a mask and frees <c>Bearer = v</c>; a scan
        /// that had swallowed it would mask it only on a second pass, and a scrub must be a no-op on
        /// its own output. A value directly followed by a mask is the rest of a value already masked
        /// (<c>$env:&lt;secret:...&gt;</c>) and is kept, as is one opening with a reference.
        /// <c>Regex.Match(text, pos)</c> lets the lookbehind see the text before
        /// <c>pos</c>, as Python's <c>search(text, pos)</c> does.</summary>
        private static string Assignments(string text, HashSet<string> exempt)
        {
            var outp = new StringBuilder(text.Length);
            var pos = 0;
            while (true)
            {
                var m = Assign.Match(text, pos);
                if (!m.Success)
                {
                    outp.Append(text, pos, text.Length - pos);
                    return outp.ToString();
                }
                Group k = m.Groups["k"], v = m.Groups["v"];
                var name = LastPart(k.Value);
                if (!IsSecret(name, exempt) || Keeps(v.Value)
                    || MaskAt.IsMatch(text, v.Index + v.Length) || ReferenceAt.IsMatch(text, v.Index))
                {
                    var keyEnd = k.Index + k.Length;
                    outp.Append(text, pos, keyEnd - pos);
                    pos = keyEnd;
                    continue;
                }
                outp.Append(text, pos, v.Index - pos).Append(Secret(v.Value));
                pos = v.Index + v.Length;
            }
        }

        /// <summary>A dotted name's field: the part after its last dot (a TOML table path, or
        /// <c>$env:KEY</c>'s <c>KEY</c> once the bare-key pattern has started after the colon).</summary>
        private static string LastPart(string k) => k.Substring(k.LastIndexOf('.') + 1);

        /// <summary><see cref="Credentials(string)"/> for parsed data: a string under a credential
        /// field is masked whole, any other string as text. Maps and lists are walked into new
        /// ones; other scalars are kept.</summary>
        public static object? CredentialsData(object? value, IEnumerable<string>? exempt = null, string? key = null) =>
            Data(value, new HashSet<string>(exempt ?? Array.Empty<string>(), StringComparer.Ordinal), key);

        private static object? Data(object? value, HashSet<string> exempt, string? key)
        {
            switch (value)
            {
                case string s:
                    if (key != null && IsSecret(key, exempt) && !Keeps(s))
                        return Secret(s);
                    return Credentials(s, exempt);
                case IDictionary<string, object?> map:
                {
                    var outMap = new Dictionary<string, object?>();
                    foreach (var kv in map) outMap[kv.Key] = Data(kv.Value, exempt, kv.Key);
                    return outMap;
                }
                case IDictionary dict:
                {
                    var outMap = new Dictionary<string, object?>();
                    foreach (DictionaryEntry e in dict)
                    {
                        var k = Convert.ToString(e.Key, CultureInfo.InvariantCulture) ?? "";
                        outMap[k] = Data(e.Value, exempt, k);
                    }
                    return outMap;
                }
                case IList list:
                {
                    var outList = new List<object?>(list.Count);
                    foreach (var x in list) outList.Add(Data(x, exempt, null));
                    return outList;
                }
                default:
                    return value;
            }
        }

        /// <summary>A value left as it is under a credential field: empty, a reference, or one
        /// already holding a mask. The last is what makes a second pass a no-op: a reference kept on
        /// the first pass (<c>$AKIA...</c>, <c>${...}</c>) can have a shape masked inside it, and
        /// what is left (<c>$&lt;secret:...&gt;</c>) is no longer a reference but must not be masked
        /// again.</summary>
        private static bool Keeps(string value) =>
            value.Length == 0 || Reference.IsMatch(value) || Mask.IsMatch(value);

        /// <summary>A quoted token's content: a single-quoted one stripped of its quotes, a
        /// double-quoted one read as a JSON string, or stripped of its quotes when it is not one.</summary>
        private static string Unquote(string token)
        {
            if (token[0] == '\'') return token.Substring(1, token.Length - 2);
            try
            {
                if (Json.Parse(token) is string s) return s;
            }
            catch { /* not JSON: fall through */ }
            return token.Substring(1, token.Length - 2);
        }

        private static string MaskShapes(string text)
        {
            foreach (var shape in Shapes)
            {
                text = shape.Replace(text, m =>
                {
                    var v = m.Groups["v"];
                    var at = v.Index - m.Index;
                    var whole = m.Value;
                    return whole.Substring(0, at) + Secret(v.Value) + whole.Substring(at + v.Length);
                });
            }
            return text;
        }

        /// <summary>A literal ASCII word as a pattern matching it in any ASCII case, and nothing
        /// else: <c>key</c> becomes <c>[Kk][Ee][Yy]</c>. .NET's IgnoreCase would also let the
        /// Kelvin sign stand for a k.</summary>
        private static string Ci(string word)
        {
            var sb = new StringBuilder();
            foreach (var c in word)
            {
                if (c >= 'a' && c <= 'z') sb.Append('[').Append((char)(c - 32)).Append(c).Append(']');
                else sb.Append(Regex.Escape(c.ToString()));
            }
            return sb.ToString();
        }
    }
}
