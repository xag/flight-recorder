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
