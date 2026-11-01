// The stock credential scrub masks the same text to the same bytes in every runtime.
//
// spec/fixtures/credentials.json is written by the Python reference; each case is a text and its
// masked form, or parsed data and its masked parse, under that case's exempt field names.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using FlightRecorder;
using Xunit;

namespace FlightRecorder.Tests
{
    public class ScrubsTests
    {
        private static Dictionary<string, object?> Fixture() =>
            (Dictionary<string, object?>)Json.Parse(
                File.ReadAllText(Path.Combine(TestSupport.FixturesDir(), "credentials.json")))!;

        private static IEnumerable<Dictionary<string, object?>> Cases() =>
            ((List<object?>)Fixture()["cases"]!).Cast<Dictionary<string, object?>>();

        private static string[] Exempt(Dictionary<string, object?> c) =>
            ((List<object?>)c["exempt"]!).Cast<string>().ToArray();

        [Fact]
        public void EveryFixtureTextMasksToTheReferenceBytes()
        {
            var n = 0;
            foreach (var c in Cases().Where(c => c.ContainsKey("text")))
            {
                Assert.Equal((string)c["masked"]!, Scrubs.Credentials((string)c["text"]!, Exempt(c)));
                n++;
            }
            Assert.True(n > 0, "the fixture holds no text case");
        }

        [Fact]
        public void EveryFixtureDocumentMasksToTheReferenceParse()
        {
            var n = 0;
            foreach (var c in Cases().Where(c => c.ContainsKey("data")))
            {
                Assert.Equal(Json.Canonical(c["masked_data"]),
                    Json.Canonical(Scrubs.CredentialsData(c["data"], Exempt(c))));
                n++;
            }
            Assert.True(n > 0, "the fixture holds no data case");
        }

        [Fact]
        public void TheScrubIsIdempotent()
        {
            foreach (var c in Cases())
            {
                var exempt = Exempt(c);
                if (c.ContainsKey("text"))
                {
                    var once = Scrubs.Credentials((string)c["text"]!, exempt);
                    Assert.Equal(once, Scrubs.Credentials(once, exempt));
                }
                else
                {
                    var once = Scrubs.CredentialsData(c["data"], exempt);
                    Assert.Equal(Json.Canonical(once), Json.Canonical(Scrubs.CredentialsData(once, exempt)));
                }
            }
        }

        [Fact]
        public void NoCredentialShapeSurvivesTheScrub()
        {
            // So a boundary declaring both never refuses a line its own scrub has cleaned.
            var shapes = Scrubs.CredentialShapes.Select(p => new Regex(p)).ToList();
            foreach (var c in Cases().Where(c => c.ContainsKey("text")))
            {
                var masked = Scrubs.Credentials((string)c["text"]!, Exempt(c));
                foreach (var shape in shapes)
                    Assert.False(shape.IsMatch(masked), $"/{shape}/ matches a scrubbed fixture text");
            }
        }

        [Fact]
        public void TheMaskIsTheFixturesMask()
        {
            Assert.Equal((string)Fixture()["mask"]!, Scrubs.Mask.ToString());
            Assert.Matches("^" + Scrubs.Mask + "$", Scrubs.Secret("hunter22"));
            Assert.Equal("<secret:20d2fe5e>", Scrubs.Secret("hunter22"));
        }

        [Fact]
        public void ABoundaryDeclaresTheScrubAndItsTripwire()
        {
            const string key = "sk-ant-api03-abcdefghijklmnopqrstu";
            var boundary = new Boundary()
                .Scrubbing(s => Scrubs.Credentials(s, "token_env"))
                .Forbidden(Scrubs.CredentialShapes);
            Assert.Equal(Scrubs.CredentialShapes.Count, boundary.Forbid.Count);

            var masked = Serial.RedactJsonable(
                new Dictionary<string, object?> { ["note"] = "use " + key, ["token_env"] = "'ALPACA'" },
                boundary.Redact, boundary.Scrub);
            var line = Json.Stringify(masked);
            Assert.DoesNotContain(key, line);
            Assert.Contains(Scrubs.Secret(key), line);
            Assert.Null(Serial.ForbiddenHit(line, boundary.Forbid));
            Assert.NotNull(Serial.ForbiddenHit(key, boundary.Forbid));

            // The bare method group is a scrub too.
            var plain = new Boundary().Scrubbing(Scrubs.Credentials);
            Assert.Equal(Scrubs.Secret(key), plain.Scrub!(key));
        }

        [Fact]
        public void ShapesAreAsciiAsInEveryOtherRuntime()
        {
            // .NET's \w, \s and \b are Unicode; Python's re.ASCII are not. A letter outside ASCII
            // must neither extend a key nor stand in for the separator before one.
            Assert.Equal("é" + Scrubs.Secret("sk-ant-api03-abcdefghijklmnopqrstu"),
                Scrubs.Credentials("ésk-ant-api03-abcdefghijklmnopqrstu"));
            Assert.Equal("ÉBearer " + Scrubs.Secret("abc123def456"), Scrubs.Credentials("ÉBearer abc123def456"));
            // The Kelvin sign is not a k under ASCII case folding.
            Assert.Equal("{\"Key\": \"v\"}", Scrubs.Credentials("{\"Key\": \"v\"}"));
            Assert.Equal("{\"KEY\": \"" + Scrubs.Secret("v") + "\"}", Scrubs.Credentials("{\"KEY\": \"v\"}"));
        }

        // --- what actually leaked: a pair inside a JSON string, and unquoted assignments -------
        //
        // The domain mirrors tests/test_scrubs.py. Credential-shaped values are built by
        // concatenation so this file does not read as carrying live-looking keys.

        private static readonly string[] Keys =
        {
            "ALPACA_API_KEY_ID", "api_token", "Authorization", "password", "session", "key", "token_env",
            "auth", "author", "keywords", "command", "url", "DB",
        };

        private static readonly string[] ExemptNames = { "key", "token_env", "auth" };

        private static readonly string[] Values =
        {
            "PKRAW0001", "s3cr\"et\\x", "Bearer abc123def456", "sk-ant-" + "api03-abcdefghijklmnopqrstu",
            "ghp_" + "abcdefghijklmnopqrstuvwxyz0123", "AKIA" + "ABCDEFGHIJKLMNOP", "postgres://u:pw1234@h/db",
            "https://x.io/mcp?api_key=abc123&x=1", "${MY_TOKEN}", "{env:MY_TOKEN}", "$MY_TOKEN", "", "mcpServers",
            "use a Bearer token", "é", "<secret:0123abcd>",
            "curl \"https://x.io/?token=abc123\" -s", "x://u:ab\\c@h", "curl \"https://u:pw1234@h/x\" -s",
        };

        /// <summary>A text held as a JSON string, <paramref name="depth"/> times over.</summary>
        private static string Nest(string text, int depth)
        {
            for (var i = 0; i < depth; i++) text = Json.Stringify(text);
            return text;
        }

        private static string Q(string s) => Json.Stringify(s);

        [Fact]
        public void APairInsideAJsonStringMasksAsItsParseDoes()
        {
            var nestable = Values.Where(v => v.IndexOfAny(new[] { '"', '\\', '\n' }) < 0).ToList();
            foreach (var exempt in new[] { Array.Empty<string>(), ExemptNames })
            foreach (var key in Keys)
            foreach (var value in nestable)
            {
                var json = "{\n  \"servers\": {\n    \"x\": {\n      " + Q(key) + ": " + Q(value) + "\n    }\n  }\n}";
                var toml = Q(key) + " = " + Q(value) + "\n";
                foreach (var inner in new[] { json, toml })
                for (var depth = 1; depth <= 3; depth++)
                {
                    var masked = Scrubs.Credentials(Nest(inner, depth), exempt);
                    Assert.Equal(Scrubs.Credentials(Nest(inner, depth - 1), exempt), (string)Json.Parse(masked)!);
                    Assert.Equal(masked, Scrubs.Credentials(masked, exempt));
                }
            }
        }

        [Fact]
        public void TheTranscriptRowThatLeakedIsMasked()
        {
            // A trace line read with a tool, held as the tool result's content in one transcript
            // row; and that row once more inside the tape that recorded the transcript.
            var keyValue = "PKRAW" + "0001TESTVALUE";
            var traceLine = "{\"e\": \"L\", \"d\": {\"env\": {\"ALPACA_API_SECRET_KEY\": " + Q(keyValue)
                + ", \"PATH\": \"/usr/bin\"}}}";
            var row = "{\"type\": \"user\", \"message\": {\"content\": [{\"type\": \"tool_result\", "
                + "\"tool_use_id\": \"t1\", \"content\": " + Q(traceLine) + "}]}}";
            foreach (var text in new[] { row, Q(row) })
            {
                var masked = Scrubs.Credentials(text);
                Assert.DoesNotContain(keyValue, masked);
                Assert.Contains("/usr/bin", masked);
                Assert.Equal(masked, Scrubs.Credentials(masked));
            }
        }

        private static readonly Func<string, string, string>[] Assignments =
        {
            (k, v) => $"{k}={v}",
            (k, v) => $"export {k}={v}",
            (k, v) => $"set {k}={v}",
            (k, v) => $"$env:{k} = {v}",
            (k, v) => $"{k}=\"{v}\"",
            (k, v) => $"{k}='{v}'",
            (k, v) => $"$env:{k} = \"{v}\"",
            (k, v) => $"--{k}={v}",
        };

        private static IEnumerable<string> Assignable() =>
            Values.Where(v => v.Length > 0 && v.IndexOfAny(" \t\r\n\"'`\\&;|<>,)}]".ToCharArray()) < 0 && v[0] != '=');

        /// <summary>A value left as it is under a credential field: empty, a variable reference,
        /// already a mask.</summary>
        private static bool Kept(string value) =>
            value.Length == 0 || value == "$MY_TOKEN" || value == "${MY_TOKEN}" || value == "{env:MY_TOKEN}"
            || Regex.IsMatch(value, @"\A<secret:[0-9a-f]{8}>\z");

        [Fact]
        public void AnAssignmentMasksItsValueWhenTheNameHoldsACredential()
        {
            foreach (var key in Keys)
            foreach (var value in Assignable())
            foreach (var form in Assignments)
            {
                var text = form(key, value) + " # done";
                var expected = Scrubs.SecretField(key) && !Kept(value)
                    ? form(key, Scrubs.Secret(value))
                    : form(key, Scrubs.Credentials(value));
                Assert.Equal(expected + " # done", Scrubs.Credentials(text));
            }
        }

        [Fact]
        public void ADotenvFileMasksAlikeAtEveryDepth()
        {
            var dotenv = string.Join("\n",
                from key in Keys from value in Assignable() from form in Assignments select form(key, value)) + "\n";
            var expect = Scrubs.Credentials(dotenv);
            for (var depth = 0; depth <= 2; depth++)
            {
                var outer = Nest(dotenv, depth);
                var masked = Scrubs.Credentials(outer);
                for (var i = 0; i < depth; i++) masked = (string)Json.Parse(masked)!;
                Assert.Equal(expect, masked);
                Assert.Equal(Scrubs.Credentials(outer), Scrubs.Credentials(Scrubs.Credentials(outer)));
            }
        }

        private const string Akia = "AKIA" + "ABCDEFGHIJKLMNOP";

        private static readonly string[] SecondPass =
        {
            "'" + Akia + "=Bearer  = Bearer ,",            // a shape masked later freed an assignment
            "token = $env:" + Akia + "\\\"KEY",            // a kept reference had a shape masked inside
            "{\"token\": \"$" + Akia + "\"}",
            "{\"token\": \"${" + Akia + "}\"}",
            Akia + " = \"api_key\": \"v1\"",              // a pair left as it is swallowed the next pair's key
            "token=\"token\": \"{\"x\"",                  // a masked value read back as a key holds "secret"
            "$token='pw' = \"api_key\"Bearer",
            // a later pass changed how an earlier one read the text (the passes now run to a fixed point)
            Akia + ":\"\"key\":\"y\"",
            "token=v:\"auth:\"y\"",
            "token=-=\"\"KEY\":\"1\"",
            "session=d:\\\"passw:\\\"y\\\"",
            "token=_x=\"token=\"token\": \"\t)&{",
            "token=\"token\": \"_;x=\" token=\"token\": \"0123456789",
        };

        // The fragments the differential fuzzing of the ports used (tests/test_scrubs.py).
        private static readonly string[] Fragments =
        {
            "\\", "\\\\", "\\\"", "\\\\\\\"", "\"", "\"\"", "'", "`", "$", "$env:",
            "${X}", "%X%", "--", "==", "=", " = ", ":", ": ", "token", "secretBearer", "api_key",
            "password", "a.b.", "KEY", "v1", Akia, "Bearer ", "é", "\n", " ", "\t", "<", ">", "&", "?",
            "://", "@", "{", "}", ")", ",", ";", "<secret:0123abcd>", "\"api_key\"", "${", "a=", "token=",
            "'pw'", "x = ", "\": \"", "\"token\": \"", "x=\"", "auth:", "passw:",
        };

        [Fact]
        public void TheScrubIsAFixedPointOverDenseFragments()
        {
            var rng = new Random(17);
            var password = new[] { "password" };
            for (var i = 0; i < 20_000; i++)
            {
                var n = rng.Next(1, 17);
                var text = string.Concat(Enumerable.Range(0, n).Select(_ => Fragments[rng.Next(Fragments.Length)]));
                var exempt = i % 2 == 1 ? password : Array.Empty<string>();
                var once = Scrubs.Credentials(text, exempt);
                Assert.True(once == Scrubs.Credentials(once, exempt), "not a fixed point: " + Json.Stringify(text));
            }
        }

        [Fact]
        public void APairLeftAsItIsGivesBackAValueThatOpensAPair()
        {
            Assert.Equal("x = \"api_key\": \"" + Scrubs.Secret("v1") + "\"", Scrubs.Credentials("x = \"api_key\": \"v1\""));
            const string e = "\\\"";
            Assert.DoesNotContain("v1", Scrubs.Credentials($"{e}x{e}: {e}api_key{e}: {e}v1{e}"));
        }

        [Fact]
        public void ALongStringOfEscapedJsonScrubsInLinearTime()
        {
            // A transcript is one long string of escaped JSON. When every escaped quote could open
            // a quoted token running to the string's end, this took seconds per call.
            Scrubs.Credentials("warm: \"up\"");          // the patterns compile on first use
            var rng = new Random(3);
            var pick = new[] { '"', 'a', 'b', ' ', ':' };
            var inner = new string(Enumerable.Range(0, 50_000).Select(_ => pick[rng.Next(pick.Length)]).ToArray());
            var body = string.Concat(Enumerable.Repeat("a\\\"", 33_334)).Substring(0, 99_999);
            var doc = "{\"note\": \"" + body + "\", \"password\": \"hunter22\"}";
            foreach (var text in new[] { Q(inner), Q(Q(inner)), Q(doc) })
            {
                var watch = System.Diagnostics.Stopwatch.StartNew();
                Scrubs.Credentials(text);
                Assert.True(watch.Elapsed < TimeSpan.FromSeconds(1),
                    $"{text.Length} characters took {watch.Elapsed.TotalSeconds:F1} s");
            }
        }

        [Fact]
        public void TheScrubIsANoOpOnItsOwnOutput()
        {
            foreach (var text in SecondPass)
            {
                var once = Scrubs.Credentials(text);
                Assert.Equal(once, Scrubs.Credentials(once));
                Assert.DoesNotContain(Akia, once);
            }
        }

        [Fact]
        public void AnUnchangedAssignmentGivesItsValueBackToTheScan()
        {
            Assert.Equal("a=token=" + Scrubs.Secret("abc123"), Scrubs.Credentials("a=token=abc123"));
        }

        [Fact]
        public void AReferenceOpeningAnUnquotedValueIsKept()
        {
            foreach (var text in new[] { "TOKEN=${MY_TOKEN}", "TOKEN={env:MY_TOKEN}", "TOKEN=%MY_TOKEN%", "TOKEN=$MY_TOKEN" })
                Assert.Equal(text, Scrubs.Credentials(text));
        }

        [Fact]
        public void ALongEscapedJsonStringDoesNotExhaustTheRegexEngine()
        {
            // The quoted-token pattern `"(?:[^"\\\n]|\\.)*"` recurses per character in some
            // engines (Java overflowed its stack). 100,000 characters, a third of them escapes.
            var body = string.Concat(Enumerable.Repeat("a\\\"", 33_334)).Substring(0, 99_999);
            var text = "{\"note\": \"" + body + "\", \"password\": \"hunter22\"}";
            Assert.True(body.Length >= 99_999);
            var masked = Scrubs.Credentials(text);
            Assert.Contains("\"password\": \"" + Scrubs.Secret("hunter22") + "\"", masked);
            Assert.Equal(masked, Scrubs.Credentials(masked));
            Assert.Equal(masked, (string)Json.Parse(Scrubs.Credentials(Q(text)))!);
        }

        [Fact]
        public void AComparisonIsNotAnAssignment()
        {
            foreach (var text in new[] { "if token == other:", "token != x", "password==hash(p)", "token<=3" })
                Assert.Equal(text, Scrubs.Credentials(text));
        }
    }
}
