// The boundary declaration: the one app-specific artifact.
//
// A program's execution is fully determined by its code plus its nondeterministic inputs. A
// Boundary names those inputs and how the tape should treat them — nothing more. The recorder
// cannot know about an input it was never told crosses the boundary; when an app grows a new
// one it is added here. That is the whole maintenance contract.
//
// Unlike Python (which patches module functions) and like Node (whose module namespace is
// immutable), .NET declares its boundary by WRAPPING what the app holds — an effect client is
// `Recorder.Wrap`ped, the clock and RNG are the `Recorder.Clock`/`Recorder.Random` handles the
// app calls. So a Boundary here carries the tape's TREATMENT rules (redaction, the forbid
// tripwire, error revival, header constants), not a list of functions to patch.

using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;

namespace FlightRecorder
{
    public sealed class Boundary
    {
        /// <summary>"module.NAME" → value, snapshotted into the session header and available to
        /// replay. Jsonable.</summary>
        public Dictionary<string, object?> Constants { get; } = new Dictionary<string, object?>();

        /// <summary>Field-name redaction rules, applied to every recorded payload before it is
        /// written and re-applied to the replayed side of every comparison, so a redacted
        /// recording still verifies. A null transform masks as <see cref="Serial.Redacted"/>; a
        /// transform must be deterministic AND idempotent — replay re-applies it to already-
        /// transformed values.</summary>
        public Dictionary<string, RedactTransform?> Redact { get; } = new Dictionary<string, RedactTransform?>();

        /// <summary>Value-level redaction: handed every leaf string the recorder is about to write,
        /// wherever it sits, and returns the masked text. Where <see cref="Redact"/> needs a field
        /// name, this needs nothing — which is the point, because the secret that hurts is the one
        /// with no name: a positional argument, a token interpolated into a document key, an API key
        /// quoted mid-sentence in a message body. <see cref="Forbid"/> can only refuse such a call;
        /// this can record it.
        ///
        /// It must be deterministic AND idempotent — replay scrubs the re-derived question and
        /// compares it to the tape, so a value that is already a mask must scrub to itself, or the
        /// recording can never be replayed. It is applied to the output of a <see cref="Redact"/>
        /// transform too. A scrub that throws masks as <see cref="Serial.Redacted"/>.</summary>
        public ScrubTransform? Scrub { get; set; }

        /// <summary>The tripwire that backstops <see cref="Redact"/> and <see cref="Scrub"/>: patterns matched against the
        /// fully-redacted line the recorder is about to write. A hit raises
        /// <see cref="ForbiddenValue"/> and writes nothing. Match shapes, not values — a credential
        /// you can enumerate you can already redact; it is the one you cannot name this is for.</summary>
        public List<Regex> Forbid { get; } = new List<Regex>();

        /// <summary>Type name → rebuild an exception from its recorded constructive args. Replay
        /// must rebuild a recorded error with its real TYPE, because the code very likely branches
        /// on it (catch + type test); an unlisted type replays as <see cref="ReplayedEffectError"/>.</summary>
        public Dictionary<string, Func<IReadOnlyList<object?>, Exception>> ErrorRevivers { get; }
            = new Dictionary<string, Func<IReadOnlyList<object?>, Exception>>();

        /// <summary>Extra key/values for the session header (digests, versions…). Preserved by any
        /// reader that rewrites the tape.</summary>
        public Dictionary<string, object?> HeaderExtras { get; } = new Dictionary<string, object?>();

        /// <summary>Mask the named fields (bare rules) — sugar for the common case.</summary>
        public Boundary MaskFields(params string[] names)
        {
            foreach (var n in names) Redact[n] = null;
            return this;
        }

        /// <summary>Add a forbid pattern from a regex string.</summary>
        public Boundary Forbidden(string pattern)
        {
            Forbid.Add(new Regex(pattern, RegexOptions.Compiled));
            return this;
        }

        /// <summary>Mask every occurrence of a pattern in every recorded string — sugar for the
        /// common shape of <see cref="Scrub"/>, and the shape that is idempotent for free, since
        /// <paramref name="mask"/> is checked not to match <paramref name="pattern"/>: rescrubbing a
        /// masked value finds nothing left to mask. Calling it twice STACKS, so several secret
        /// shapes each get their own line rather than one regex having to spell them all.</summary>
        public Boundary Scrubbing(string pattern, string mask = Serial.Redacted)
        {
            var re = new Regex(pattern, RegexOptions.Compiled);
            if (re.IsMatch(mask))
                throw new ArgumentException(
                    $"the mask \"{mask}\" itself matches /{pattern}/, so scrubbing it again would " +
                    "change it — replay re-scrubs what it reads off the tape, and would diverge on " +
                    "every recording this rule touched");
            var prior = Scrub;
            Scrub = s => re.Replace(prior == null ? s : prior(s), mask);
            return this;
        }

        internal IReadOnlyDictionary<string, RedactTransform?> RedactRules => Redact;

        /// <summary>Rebuild a recorded effect error. Its type drives which reviver runs; an
        /// unlisted type, or a reviver that throws, falls back to <see cref="ReplayedEffectError"/>.</summary>
        public Exception ReviveError(IDictionary<string, object?> err)
        {
            var type = err.TryGetValue("type", out var t) ? t as string ?? "" : "";
            var args = new List<object?>();
            if (err.TryGetValue("args", out var a) && Serial.FromJsonable(a) is IEnumerable<object?> list)
                args.AddRange(list);

            if (ErrorRevivers.TryGetValue(type, out var reviver))
            {
                try { return reviver(args); }
                catch { /* fall through */ }
            }
            var repr = err.TryGetValue("repr", out var r) ? r as string ?? "" : "";
            return new ReplayedEffectError($"{type}: {repr}");
        }
    }
}
