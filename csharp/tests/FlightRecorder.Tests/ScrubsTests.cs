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
    }
}
