using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using FlightRecorder;
using Xunit;

namespace FlightRecorder.Tests
{
    public class SerialTests
    {
        [Fact]
        public void EncodesAndRevivesDatetime()
        {
            var dt = new DateTime(2026, 7, 17, 10, 30, 0, DateTimeKind.Utc);
            var enc = Serial.ToJsonable(dt);
            var map = Assert.IsType<Dictionary<string, object?>>(enc);
            Assert.True(map.ContainsKey("__dt__"));
            // A __dt__ with a Z revives to an aware DateTimeOffset; a naive one to a DateTime. Either
            // way it is a real instant, not the marker.
            var revived = Serial.FromJsonable(enc);
            Assert.True(revived is DateTime || revived is DateTimeOffset);
        }

        [Fact]
        public void ExoticValuesDegradeToOpaque()
        {
            var enc = Serial.ToJsonable(new object());
            var map = Assert.IsType<Dictionary<string, object?>>(enc);
            Assert.True(map.ContainsKey("__opaque__"));
        }

        [Fact]
        public void RedactsByFieldName()
        {
            var tree = new Dictionary<string, object?>
            {
                ["user"] = "alice",
                ["password"] = "hunter2",
                ["nested"] = new Dictionary<string, object?> { ["password"] = "again" },
            };
            var rules = new Dictionary<string, RedactTransform?> { ["password"] = null };
            var red = (Dictionary<string, object?>)Serial.RedactJsonable(tree, rules)!;
            Assert.Equal("alice", red["user"]);
            Assert.Equal(Serial.Redacted, red["password"]);
            Assert.Equal(Serial.Redacted, ((Dictionary<string, object?>)red["nested"]!)["password"]);
        }

        [Fact]
        public void RedactionTransformIsIdempotentOnItsOwnOutput()
        {
            RedactTransform mask = v => "***";
            var rules = new Dictionary<string, RedactTransform?> { ["k"] = mask };
            var once = Serial.RedactJsonable(new Dictionary<string, object?> { ["k"] = "secret" }, rules);
            var twice = Serial.RedactJsonable(once, rules);
            Assert.Equal(Json.Canonical(once), Json.Canonical(twice));
        }

        [Fact]
        public void ScrubMasksAValueThatHasNoFieldName()
        {
            // Nothing here is called "token". The secret sits in a positional argument, inside a key
            // built by interpolation, and mid-sentence in prose — the three places a field-name rule
            // is blind to, which is the entire reason Scrub exists.
            var tree = new Dictionary<string, object?>
            {
                ["args"] = new List<object?> { "sk-live-4242", 7L },
                ["key"] = "seen:alice:sk-live-4242",
                ["body"] = "your key is sk-live-4242, keep it safe",
            };
            ScrubTransform scrub = s => s.Replace("sk-live-4242", Serial.Redacted);
            var red = (Dictionary<string, object?>)Serial.RedactJsonable(tree, null, scrub)!;

            Assert.DoesNotContain("sk-live-4242", Json.Canonical(red));
            Assert.Equal(Serial.Redacted, ((List<object?>)red["args"]!)[0]);
            Assert.Equal(7L, ((List<object?>)red["args"]!)[1]); // a non-string leaf is left alone
            Assert.Equal($"seen:alice:{Serial.Redacted}", red["key"]);
            Assert.Equal($"your key is {Serial.Redacted}, keep it safe", red["body"]);
        }

        [Fact]
        public void ScrubIsAppliedOnTopOfFieldNameRules()
        {
            // The two layers compose: the rule handles the field it can name, the scrub sweeps what
            // no name reaches — including the rule's own output, so a transform that shortens rather
            // than masks cannot carry the secret past the sweep.
            var tree = new Dictionary<string, object?>
            {
                ["password"] = "hunter2",
                ["hint"] = "same as sk-live-4242",
                ["short"] = "sk-live-4242",
            };
            var rules = new Dictionary<string, RedactTransform?>
            {
                ["password"] = null,
                ["short"] = v => v, // a pass-through rule: the sweep is the only thing left to catch it
            };
            ScrubTransform scrub = s => s.Replace("sk-live-4242", Serial.Redacted);
            var red = (Dictionary<string, object?>)Serial.RedactJsonable(tree, rules, scrub)!;

            Assert.Equal(Serial.Redacted, red["password"]);
            Assert.Equal($"same as {Serial.Redacted}", red["hint"]);
            Assert.Equal(Serial.Redacted, red["short"]);
        }

        [Fact]
        public void ScrubIsIdempotentOnItsOwnOutput()
        {
            // The claim replay leans on: a value read back off the tape is already a mask, and must
            // scrub to itself. See RecordReplayTests for the same claim exercised end to end.
            ScrubTransform scrub = s => s.Replace("sk-live-4242", Serial.Redacted);
            var tree = new Dictionary<string, object?> { ["k"] = "seen:sk-live-4242" };
            var once = Serial.RedactJsonable(tree, null, scrub);
            var twice = Serial.RedactJsonable(once, null, scrub);
            Assert.Equal(Json.Canonical(once), Json.Canonical(twice));
        }

        [Fact]
        public void AThrowingScrubMasksRatherThanLeaks()
        {
            ScrubTransform scrub = _ => throw new InvalidOperationException("boom");
            var red = (Dictionary<string, object?>)Serial.RedactJsonable(
                new Dictionary<string, object?> { ["k"] = "sk-live-4242" }, null, scrub)!;
            Assert.Equal(Serial.Redacted, red["k"]);
        }

        [Fact]
        public void NoScrubMeansNoSweep()
        {
            var tree = new Dictionary<string, object?> { ["k"] = "sk-live-4242" };
            Assert.Same(tree, Serial.RedactJsonable(tree, null, null));
        }

        [Fact]
        public void ScrubbingRefusesAMaskThatItsOwnPatternMatches()
        {
            // Such a rule would keep moving the value on every pass, so a recording made under it
            // could never be replayed. Better to refuse at declaration than to diverge at replay.
            var b = new Boundary();
            Assert.Throws<ArgumentException>(() => b.Scrubbing("sk-[a-z0-9-]+", "sk-masked"));
            Assert.Null(b.Scrub);
        }

        [Fact]
        public void ScrubbingStacks()
        {
            var b = new Boundary().Scrubbing("sk-live-[0-9]+").Scrubbing("[0-9]{16}");
            Assert.Equal($"{Serial.Redacted} and {Serial.Redacted}",
                b.Scrub!("sk-live-4242 and 4111111111111111"));
        }

        [Fact]
        public void ForbiddenHitReturnsThePatternNotTheValue()
        {
            var patterns = new List<Regex> { new Regex(@"\b[a-f0-9]{64}\b") };
            var digest = new string('a', 64);
            var hit = Serial.ForbiddenHit($"...{digest}...", patterns);
            Assert.NotNull(hit);
            Assert.DoesNotContain(digest, hit!); // never quotes the credential it caught
        }

        [Fact]
        public void UndefMarkerRevivesToNull()
        {
            var v = Serial.FromJsonable(new Dictionary<string, object?> { ["__undef__"] = true });
            Assert.Null(v);
        }
    }
}
