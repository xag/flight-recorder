using System.IO;
using System.Linq;
using FlightRecorder.Spec;
using Xunit;

namespace FlightRecorder.Tests
{
    public class SpecFixtureTests
    {
        public static TheoryData<string> Fixtures()
        {
            var data = new TheoryData<string>();
            foreach (var f in Directory.GetFiles(TestSupport.FixturesDir(), "*.jsonl").OrderBy(x => x))
                data.Add(Path.GetFileName(f));
            return data;
        }

        // Every implementation must validate every fixture — the whole point of the frozen spec.
        [Theory]
        [MemberData(nameof(Fixtures))]
        public void EveryFixtureIsConformant(string name)
        {
            var text = File.ReadAllText(Path.Combine(TestSupport.FixturesDir(), name));
            var violations = Validate.ValidateTape(text);
            Assert.True(violations.Count == 0, $"{name}:\n  " + string.Join("\n  ", violations));
        }

        [Fact]
        public void FindsTheDotnetFixtures()
        {
            // The .NET fixtures must exist and name the dotnet runtime.
            foreach (var name in new[] { "dotnet-toy.jsonl", "dotnet-sem-toy.jsonl" })
            {
                var text = File.ReadAllText(Path.Combine(TestSupport.FixturesDir(), name));
                Assert.Contains("\"dotnet\"", text);
                Assert.Empty(Validate.ValidateTape(text));
            }
        }

        [Fact]
        public void CatchesAMalformedTape()
        {
            var bad = "{\"ev\":\"session\",\"version\":1,\"started\":\"2026-07-17T10:00:00\",\"dotnet\":\"8\",\"constants\":{}}\n" +
                      "{\"ev\":\"call\",\"seq\":2,\"fn\":\"x\",\"kwargs\":{},\"events\":[],\"error\":null,\"ts\":\"2026-07-17T10:00:00+02:00\",\"ms\":1}";
            var violations = Validate.ValidateTape(bad);
            Assert.NotEmpty(violations); // naive session.started, and seq starts at 2
            Assert.Contains(violations, v => v.Contains("timezone-aware"));
            Assert.Contains(violations, v => v.Contains("monotonic"));
        }
    }
}
