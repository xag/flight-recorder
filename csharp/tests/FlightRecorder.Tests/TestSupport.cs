using System;
using System.IO;
using System.Collections.Generic;
using FlightRecorder;
using FlightRecorder.Toy;

// The recorder is global (Install/Uninstall), so tests must not run concurrently.
[assembly: Xunit.CollectionBehavior(DisableTestParallelization = true)]

namespace FlightRecorder.Tests
{
    internal static class TestSupport
    {
        /// <summary>The repo root — the ancestor directory holding spec/tape-v1.md.</summary>
        public static string RepoRoot()
        {
            var dir = new DirectoryInfo(AppContext.BaseDirectory);
            while (dir != null && !File.Exists(Path.Combine(dir.FullName, "spec", "tape-v1.md")))
                dir = dir.Parent;
            if (dir == null) throw new DirectoryNotFoundException("could not locate the repo root (spec/tape-v1.md)");
            return dir.FullName;
        }

        public static string FixturesDir() => Path.Combine(RepoRoot(), "spec", "fixtures");

        public static string TempDir()
        {
            var d = Path.Combine(Path.GetTempPath(), "fr-test-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(d);
            return d;
        }

        /// <summary>The boundary the toy is recorded and replayed against: password masked, and a
        /// reviver so a recorded ToyError replays as a real ToyError (not the generic fallback).</summary>
        public static Boundary ToyBoundary()
        {
            var b = new Boundary().MaskFields("password");
            b.ErrorRevivers["ToyError"] = args => new ToyError(args.Count > 0 ? args[0] as string ?? "" : "");
            return b;
        }

        public static IStore WrapStore() =>
            Recorder.WrapAs<IStore>(new ToyStore(), "store",
                nameof(IStore.Get), nameof(IStore.Set), nameof(IStore.CreateAccount), nameof(IStore.MaybeFail));

        /// <summary>Record a scenario to a fresh tape and return its path.</summary>
        public static string RecordToTape(Boundary boundary, Action<IStore> scenario)
        {
            var store = WrapStore();
            var dir = TempDir();
            var path = Recorder.Install(boundary, directory: dir)!;
            try { scenario(store); } finally { Recorder.Uninstall(); }
            return path;
        }
    }
}
