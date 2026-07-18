// Tape v1 conformance checker — the .NET mirror of spec/validate.py and js/src/spec/validate.js.
//
// The three files are the same claim written thrice, on purpose. The tape is the contract
// between the runtimes, and the only way to know a contract holds is to have independent
// parties agree about the same artifact: every checker runs against the same fixtures in
// spec/fixtures/, and a disagreement means the tape has forked — the single failure this whole
// arrangement exists to prevent.
//
// Like its twins it imports nothing from the recorder, so it cannot bless whatever an
// implementation happens to do. It knows only JSON and the spec. Returns a list of
// human-readable violations; empty means conformant.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;

namespace FlightRecorder.Spec
{
    public static class Validate
    {
        public const int Version = 1;
        public const int MaxDepth = 16;

        private static readonly HashSet<string> Markers = new HashSet<string>
            { "__dt__", "__date__", "__undef__", "__opaque__" };
        private static readonly HashSet<string> ReservedMarkers = new HashSet<string>
            { "__snap__", "__seq__", "__str__", "__esc__" };
        private static readonly HashSet<string> EventKinds = new HashSet<string>
            { "fx", "db", "now", "perf", "rand", "sem" };
        private static readonly HashSet<string> SemPhases = new HashSet<string>
            { "begin", "end", "point" };
        // python | node | dotnet | go. Adding a runtime is an additive change (the spec's own "add
        // a key, no version bump" rule): existing tapes still validate, and a further recorder's
        // tapes now validate too.
        private static readonly string[] Runtimes = { "python", "node", "dotnet", "go", "java" };

        private static readonly Regex Iso = new Regex(
            @"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$", RegexOptions.Compiled);
        private static readonly Regex HasOffset = new Regex(@"(Z|[+-]\d{2}:?\d{2})$", RegexOptions.Compiled);
        private static readonly Regex Hex = new Regex("^[0-9a-f]+$", RegexOptions.Compiled);

        private static bool IsIso(object? s) => s is string str && Iso.IsMatch(str);
        private static bool IsTzAware(object? s) => IsIso(s) && HasOffset.IsMatch((string)s!);
        private static bool IsInt(object? v) => v is long || v is int;
        private static bool IsNumber(object? v) => v is long || v is int || v is double || v is float;
        private static bool IsObj(object? v) => v is IDictionary<string, object?>;
        private static IDictionary<string, object?> Obj(object? v) => (IDictionary<string, object?>)v!;

        private static double AsNum(object? v) => v switch
        {
            long l => l, int i => i, double d => d, float f => f, _ => double.NaN,
        };

        private static void CheckValue(object? v, string path, List<string> outp, int depth = 0)
        {
            if (depth > MaxDepth)
            {
                outp.Add($"{path}: nested deeper than {MaxDepth}; must degrade to __opaque__");
                return;
            }
            if (v == null || v is string || IsNumber(v) || v is bool) return;

            if (v is IEnumerable<object?> arr && !IsObj(v))
            {
                var i = 0;
                foreach (var x in arr) CheckValue(x, $"{path}[{i++}]", outp, depth + 1);
                return;
            }

            if (v is IDictionary<string, object?> map)
            {
                if (map.Count == 1)
                {
                    var k = map.Keys.First();
                    if (Markers.Contains(k))
                    {
                        var payload = map[k];
                        if ((k == "__dt__" || k == "__date__") && !IsIso(payload))
                            outp.Add($"{path}: {k} payload is not ISO-8601: {Json.Stringify(payload)}");
                        if (k == "__undef__" && !(payload is bool bb && bb))
                            outp.Add($"{path}: __undef__ payload must be true");
                        if (k == "__opaque__")
                        {
                            if (!(payload is string ps)) outp.Add($"{path}: __opaque__ payload must be a string");
                            else if (ps.Length > 200) outp.Add($"{path}: __opaque__ payload exceeds 200 chars");
                        }
                        return;
                    }
                    if (ReservedMarkers.Contains(k)) return; // reserved: legal, not interpreted here
                }
                foreach (var kv in map) CheckValue(kv.Value, $"{path}.{kv.Key}", outp, depth + 1);
                return;
            }

            outp.Add($"{path}: {v.GetType().Name} is not JSON");
        }

        private static void CheckSnapshot(object? s, string path, List<string> outp)
        {
            if (!IsObj(s)) { outp.Add($"{path}: snapshot must be an object"); return; }
            var map = Obj(s);
            foreach (var key in new[] { "id", "exists", "data" })
                if (!map.ContainsKey(key)) outp.Add($"{path}: snapshot missing '{key}'");
            if (map.ContainsKey("exists") && !(map["exists"] is bool))
                outp.Add($"{path}.exists: must be a bool");
            if (map.ContainsKey("data")) CheckValue(map["data"], $"{path}.data", outp);
        }

        private static void CheckEvent(object? e, string path, List<string> outp)
        {
            if (!IsObj(e)) { outp.Add($"{path}: event must be an object"); return; }
            var ev = Obj(e);
            var k = ev.GetValueOrNull("k") as string;
            if (k == null || !EventKinds.Contains(k)) return; // unknown kind: a reader must ignore it

            switch (k)
            {
                case "fx":
                    if (!(ev.GetValueOrNull("fn") is string)) outp.Add($"{path}: fx needs a string 'fn'");
                    if (!(ev.GetValueOrNull("args") is IEnumerable<object?> fa && !IsObj(ev.GetValueOrNull("args"))))
                        outp.Add($"{path}: fx needs an array 'args'");
                    else CheckValue(ev["args"], $"{path}.args", outp);
                    if (!IsObj(ev.GetValueOrNull("kwargs"))) outp.Add($"{path}: fx needs an object 'kwargs' ({{}} in JS)");
                    else CheckValue(ev["kwargs"], $"{path}.kwargs", outp);
                    var hasRes = ev.ContainsKey("res");
                    var hasErr = ev.ContainsKey("err");
                    if (hasRes == hasErr) outp.Add($"{path}: fx must carry exactly one of 'res' / 'err'");
                    if (hasRes) CheckValue(ev["res"], $"{path}.res", outp);
                    if (hasErr && (!IsObj(ev["err"]) || !(Obj(ev["err"]).GetValueOrNull("type") is string)))
                        outp.Add($"{path}.err: must be an object with a string 'type'");
                    break;

                case "db":
                    if (!(ev.GetValueOrNull("op") is string)) outp.Add($"{path}: db needs a string 'op'");
                    if (!(ev.GetValueOrNull("sig") is string)) outp.Add($"{path}: db needs a string 'sig'");
                    var dbRes = ev.ContainsKey("res");
                    var dbArgs = ev.ContainsKey("args");
                    if (dbRes && dbArgs) outp.Add($"{path}: db carries 'res' (a read) or 'args' (a write), never both");
                    if (!dbRes && !dbArgs) outp.Add($"{path}: db must carry 'res' or 'args'");
                    if (dbRes)
                    {
                        if (ev["res"] is IEnumerable<object?> rlist && !IsObj(ev["res"]))
                        {
                            var i = 0;
                            foreach (var s in rlist) CheckSnapshot(s, $"{path}.res[{i++}]", outp);
                        }
                        else CheckSnapshot(ev["res"], $"{path}.res", outp);
                    }
                    if (dbArgs) CheckValue(ev["args"], $"{path}.args", outp);
                    break;

                case "now":
                    if (!IsIso(ev.GetValueOrNull("v")))
                        outp.Add($"{path}: now.v must be an ISO-8601 string, got {Json.Stringify(ev.GetValueOrNull("v"))}");
                    break;

                case "perf":
                    if (!IsNumber(ev.GetValueOrNull("v")))
                        outp.Add($"{path}: perf.v must be a number (milliseconds), got {Json.Stringify(ev.GetValueOrNull("v"))}");
                    break;

                case "sem":
                    if (!(ev.GetValueOrNull("name") is string name) || name.Length == 0)
                        outp.Add($"{path}: sem needs a non-empty string 'name'");
                    var phase = ev.GetValueOrNull("phase") as string;
                    if (phase == null || !SemPhases.Contains(phase))
                        outp.Add($"{path}: sem.phase must be one of begin|end|point, got {Json.Stringify(ev.GetValueOrNull("phase"))}");
                    if (!IsInt(ev.GetValueOrNull("sid")))
                        outp.Add($"{path}: sem needs an int 'sid', unique within the call");
                    if (ev.ContainsKey("data"))
                    {
                        if (!IsObj(ev["data"])) outp.Add($"{path}: sem.data must be an object");
                        else CheckValue(ev["data"], $"{path}.data", outp);
                    }
                    if (ev.ContainsKey("outcome"))
                    {
                        if (phase != "end") outp.Add($"{path}: sem.outcome belongs to an 'end', not a {Json.Stringify(phase)}");
                        var oc = ev["outcome"] as string;
                        if (oc != "ok" && oc != "error")
                            outp.Add($"{path}: sem.outcome must be 'ok' or 'error', got {Json.Stringify(ev["outcome"])}");
                    }
                    break;

                case "rand":
                    CheckRand(ev, path, outp);
                    break;
            }
        }

        private static void CheckRand(IDictionary<string, object?> ev, string path, List<string> outp)
        {
            var m = ev.GetValueOrNull("m") as string;
            switch (m)
            {
                case "sample":
                    foreach (var key in new[] { "n", "kk" })
                        if (!IsInt(ev.GetValueOrNull(key))) outp.Add($"{path}: rand.{key} must be an int");
                    var idxObj = ev.GetValueOrNull("idx");
                    if (!(idxObj is IEnumerable<object?> idx && !IsObj(idxObj) && idx.All(IsInt)))
                        outp.Add($"{path}: rand.idx must be an array of ints");
                    else if (IsInt(ev.GetValueOrNull("n")))
                    {
                        var n = (long)AsNum(ev["n"]);
                        var idxList = idx.Select(x => (long)AsNum(x)).ToList();
                        var bad = idxList.Where(i => !(i >= 0 && i < n)).ToList();
                        if (bad.Count > 0)
                            outp.Add($"{path}: rand.idx [{string.Join(",", bad)}] out of range for population {n}");
                        if (IsInt(ev.GetValueOrNull("kk")) && idxList.Count != (long)AsNum(ev["kk"]))
                            outp.Add($"{path}: rand.idx has {idxList.Count} positions but kk={ev["kk"]}");
                    }
                    break;
                case "bytes":
                    var nb = ev.GetValueOrNull("n");
                    if (!IsInt(nb) || (long)AsNum(nb) < 0) outp.Add($"{path}: rand.n must be a non-negative int");
                    var hx = ev.GetValueOrNull("hex") as string;
                    if (hx == null || (hx.Length > 0 && !Hex.IsMatch(hx)))
                        outp.Add($"{path}: rand.hex must be a lowercase hex string");
                    else if (IsInt(nb) && hx.Length != 2 * (long)AsNum(nb))
                        outp.Add($"{path}: rand.hex is {hx.Length} chars but n={nb} implies {2 * (long)AsNum(nb)}");
                    break;
                case "float":
                    var fv = ev.GetValueOrNull("v");
                    if (!IsNumber(fv) || !(AsNum(fv) >= 0.0 && AsNum(fv) < 1.0))
                        outp.Add($"{path}: rand.v must be a number in [0, 1), got {Json.Stringify(fv)}");
                    break;
                case "int":
                    if (!IsInt(ev.GetValueOrNull("v")))
                        outp.Add($"{path}: rand.v must be an int, got {Json.Stringify(ev.GetValueOrNull("v"))}");
                    break;
                default:
                    outp.Add($"{path}: rand.m must be one of sample|bytes|float|int, got {Json.Stringify(m)}");
                    break;
            }
        }

        private static void CheckSemNesting(IEnumerable<object?> evs, string path, List<string> outp)
        {
            var stack = new List<(long Sid, string Name)>();
            var seen = new HashSet<long>();
            var j = 0;
            foreach (var e in evs)
            {
                var idx = j++;
                if (!(e is IDictionary<string, object?> ev) || (ev.GetValueOrNull("k") as string) != "sem") continue;
                if (!IsInt(ev.GetValueOrNull("sid"))) continue;
                var phase = ev.GetValueOrNull("phase") as string;
                if (phase == null || !SemPhases.Contains(phase)) continue;
                var sid = (long)AsNum(ev["sid"]);
                var name = ev.GetValueOrNull("name") as string ?? "";

                if (phase == "begin" || phase == "point")
                {
                    if (seen.Contains(sid))
                        outp.Add($"{path}.events[{idx}]: sem sid {sid} is reused — a sid must be unique within " +
                                 "the call, or an 'end' cannot name its 'begin'");
                    seen.Add(sid);
                    if (phase == "begin") stack.Add((sid, name));
                }
                else // end
                {
                    if (stack.Count == 0)
                        outp.Add($"{path}.events[{idx}]: sem 'end' (sid {sid}) with no open span");
                    else if (stack[stack.Count - 1].Sid != sid)
                    {
                        var (openSid, openName) = stack[stack.Count - 1];
                        outp.Add($"{path}.events[{idx}]: sem spans are not well-nested — 'end' closes sid {sid} " +
                                 $"while sid {openSid} (\"{openName}\") is still open. Spans nest; they never straddle.");
                        if (stack.Any(s => s.Sid == sid))
                        {
                            while (stack.Count > 0 && stack[stack.Count - 1].Sid != sid) stack.RemoveAt(stack.Count - 1);
                            if (stack.Count > 0) stack.RemoveAt(stack.Count - 1);
                        }
                    }
                    else stack.RemoveAt(stack.Count - 1);
                }
            }
            foreach (var (sid, name) in stack)
                outp.Add($"{path}: sem span \"{name}\" (sid {sid}) is never closed — a completed call holds no open spans");
        }

        private static void ValidateLine(object? obj, int i, List<string> outp, bool first)
        {
            if (!IsObj(obj)) { outp.Add($"line {i}: not an object"); return; }
            var o = Obj(obj);
            var ev = o.GetValueOrNull("ev") as string;

            if (first)
            {
                if (ev != "session") { outp.Add($"line {i}: the first line must be the session header, got ev={Json.Stringify(o.GetValueOrNull("ev"))}"); return; }
            }
            else if (ev == "session") { outp.Add($"line {i}: a second session header"); return; }

            if (ev == "session")
            {
                if (!(o.GetValueOrNull("version") is long ver && ver == Version))
                    outp.Add($"line {i}: version must be {Version}, got {Json.Stringify(o.GetValueOrNull("version"))}");
                if (!IsTzAware(o.GetValueOrNull("started")))
                    outp.Add($"line {i}: session.started must be timezone-aware ISO-8601");
                if (!IsObj(o.GetValueOrNull("constants"))) outp.Add($"line {i}: session.constants must be an object");
                else CheckValue(o["constants"], $"line {i}.constants", outp);
                var runtimes = Runtimes.Where(o.ContainsKey).ToList();
                if (runtimes.Count != 1)
                    outp.Add($"line {i}: session must name exactly one runtime (python|node|dotnet|go), got [{string.Join(",", runtimes)}]");
                return;
            }

            if (ev == "call")
            {
                if (!(o.GetValueOrNull("seq") is long seq) || seq < 1) outp.Add($"line {i}: call.seq must be an int >= 1");
                if (!(o.GetValueOrNull("fn") is string)) outp.Add($"line {i}: call.fn must be a string");
                if (!IsObj(o.GetValueOrNull("kwargs"))) outp.Add($"line {i}: call.kwargs must be an object");
                else CheckValue(o["kwargs"], $"line {i}.kwargs", outp);
                if (o.ContainsKey("result")) CheckValue(o["result"], $"line {i}.result", outp);
                if (!o.ContainsKey("error")) outp.Add($"line {i}: call must carry 'error' (null when it did not raise)");
                else if (o["error"] != null && !(o["error"] is string)) outp.Add($"line {i}: call.error must be a string or null");
                if (!IsTzAware(o.GetValueOrNull("ts"))) outp.Add($"line {i}: call.ts must be timezone-aware ISO-8601");
                if (!IsNumber(o.GetValueOrNull("ms"))) outp.Add($"line {i}: call.ms must be a number");
                var evs = o.GetValueOrNull("events");
                if (!(evs is IEnumerable<object?> list) || IsObj(evs)) outp.Add($"line {i}: call.events must be an array");
                else
                {
                    var j = 0;
                    foreach (var e in list) CheckEvent(e, $"line {i}.events[{j++}]", outp);
                    CheckSemNesting(list, $"line {i}", outp);
                }
                return;
            }
            // unknown ev (e.g. the reserved "inflight"): a reader must tolerate it.
        }

        /// <summary>Validate a whole tape. Returns violations; empty means conformant.</summary>
        public static List<string> ValidateTape(string text)
        {
            var outp = new List<string>();
            var lines = text.Split('\n').Where(ln => ln.Trim().Length > 0).ToList();
            if (lines.Count == 0) return new List<string> { "empty tape: the session header is mandatory" };

            var seqs = new List<long>();
            for (var i = 0; i < lines.Count; i++)
            {
                object? obj;
                try { obj = Json.Parse(lines[i]); }
                catch (Exception e)
                {
                    if (i == lines.Count - 1) continue; // only the final line may be torn
                    outp.Add($"line {i}: not JSON ({e.Message})");
                    continue;
                }
                ValidateLine(obj, i, outp, first: i == 0);
                if (obj is IDictionary<string, object?> o && (o.GetValueOrNull("ev") as string) == "call"
                    && o.GetValueOrNull("seq") is long s) seqs.Add(s);
            }

            var expected = Enumerable.Range(1, seqs.Count).Select(x => (long)x).ToList();
            if (!seqs.SequenceEqual(expected))
                outp.Add($"call.seq must be 1-based and monotonic; got [{string.Join(",", seqs)}]");

            return outp;
        }
    }
}
