// Produce the .NET conformance fixtures — spec/fixtures/dotnet-toy.jsonl and
// dotnet-sem-toy.jsonl — by recording the shared toy app. Every fixture in the spec must have
// been produced by an implementation; these are ours. Run:
//
//   dotnet run --project csharp/tools/GenFixtures -- <spec/fixtures dir>

using System;
using System.IO;
using System.Collections.Generic;
using FlightRecorder;
using FlightRecorder.Toy;

var outDir = args.Length > 0 ? args[0] : "spec/fixtures";
Directory.CreateDirectory(outDir);

string RecordScenario(Action<IStore> scenario, IReadOnlyDictionary<string, object?>? constants = null)
{
    var boundary = new Boundary().MaskFields("password");
    if (constants != null)
        foreach (var kv in constants) boundary.Constants[kv.Key] = kv.Value;

    var store = Recorder.WrapAs<IStore>(new ToyStore(), "store",
        nameof(IStore.Get), nameof(IStore.Set), nameof(IStore.CreateAccount), nameof(IStore.MaybeFail),
                nameof(IStore.Boom));

    var tmp = Path.Combine(Path.GetTempPath(), $"fr-fixture-{Guid.NewGuid():N}");
    var path = Recorder.Install(boundary, directory: tmp)!;
    try { scenario(store); }
    finally { Recorder.Uninstall(); }
    return File.ReadAllText(path);
}

// --- dotnet-toy.jsonl: fx + rand + now + a raised error + redaction --------------------
var toy = RecordScenario(store =>
{
    ToyTools.Greet(store, "alice");
    try { ToyTools.Explode(store, "ghost"); } catch (ToyError) { /* recorded as the call's error */ }
    ToyTools.Signup(store, "a@b.c", "hunter2");
}, new Dictionary<string, object?> { ["toy.LIMIT"] = 3 });
File.WriteAllText(Path.Combine(outDir, "dotnet-toy.jsonl"), toy);

// --- dotnet-sem-toy.jsonl: sem spans + db + fx + now, with a caught failure ------------
var sem = RecordScenario(store => ToyTools.Enrol(store, "alice", "hunter2"));
File.WriteAllText(Path.Combine(outDir, "dotnet-sem-toy.jsonl"), sem);

// Validate what we just wrote against our own checker, so a broken fixture never lands.
foreach (var name in new[] { "dotnet-toy.jsonl", "dotnet-sem-toy.jsonl" })
{
    var violations = FlightRecorder.Spec.Validate.ValidateTape(File.ReadAllText(Path.Combine(outDir, name)));
    if (violations.Count > 0)
    {
        Console.Error.WriteLine($"{name} is NOT conformant:");
        foreach (var v in violations) Console.Error.WriteLine($"  {v}");
        Environment.Exit(1);
    }
    Console.WriteLine($"{name}: conformant");
}
