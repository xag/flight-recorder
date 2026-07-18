// A toy app at its nondeterminism boundary — shared by the tests and the fixture generator, so
// the tape the tests replay is the tape the fixtures pin. It touches every door: an effect
// client (fx), a chained read (db), the clock (now), randomness (rand), semantic spans (sem),
// and a redacted field (password).

using System;
using System.Collections.Generic;
using FlightRecorder;

namespace FlightRecorder.Toy
{
    public sealed class ToyError : Exception
    {
        public ToyError(string message) : base(message) { }
    }

    public sealed class Doc
    {
        public string Name { get; set; } = "";
        public int X { get; set; }
    }

    public sealed class Account
    {
        public string Id { get; set; } = "";
        public string Email { get; set; } = "";
        public string Password { get; set; } = "";
    }

    /// <summary>The effect client the app holds. In production it talks to a store; here it is
    /// wrapped by the recorder, so its named methods become `fx` events.</summary>
    public interface IStore
    {
        Doc Get(string key);
        string Set(string key, IDictionary<string, object?> value);
        Account CreateAccount(string email, IDictionary<string, object?> fields);
        void MaybeFail(int n);
        void Boom(string key);
    }

    public sealed class ToyStore : IStore
    {
        private readonly Dictionary<string, Doc> _data = new Dictionary<string, Doc>
        {
            ["alice"] = new Doc { Name = "Alice", X = 3 },
        };

        public Doc Get(string key)
        {
            if (!_data.TryGetValue(key, out var d)) throw new ToyError($"no such key: {key}");
            return d;
        }

        public string Set(string key, IDictionary<string, object?> value) => "OK";

        public Account CreateAccount(string email, IDictionary<string, object?> fields) =>
            new Account { Id = "acct-13", Email = email, Password = (string)(fields["password"] ?? "") };

        public void MaybeFail(int n) => throw new ToyError("kaput");

        // The canonical scenario's failing effect: the message names the key, so every runtime's
        // `registration_failed` note reads the same.
        public void Boom(string key) => throw new ToyError($"no such key: {key}");
    }

    /// <summary>The recorded tools. Each `Recorder.Record` envelope is one tape line.</summary>
    public static class ToyTools
    {
        private static string Hex(byte[] bytes)
        {
            var c = new char[bytes.Length * 2];
            const string hex = "0123456789abcdef";
            for (var i = 0; i < bytes.Length; i++)
            {
                c[i * 2] = hex[bytes[i] >> 4];
                c[i * 2 + 1] = hex[bytes[i] & 0xF];
            }
            return new string(c);
        }

        /// <summary>The canonical plain scenario: an effect, a chained read, all four random
        /// shapes, both clocks, and a chained write — every event kind the format defines, on
        /// one tape, in the same order every runtime writes them.</summary>
        public static object? Greet(IStore store, string user) =>
            Recorder.Record("greet", new { user }, () =>
            {
                var doc = store.Get(user);                     // fx store.get

                Recorder.DbRead("stream", "collection(\"users\").where(\"x\", \">\", 0)",
                    () => new List<Snapshot>
                    {
                        Snapshot.Of("0", new Dictionary<string, object?> { ["name"] = "alpha", ["x"] = 1 }),
                        Snapshot.Of("1", new Dictionary<string, object?> { ["name"] = "beta", ["x"] = 2 }),
                    });

                Recorder.Random.Sample(new[] { 0, 1, 2 }, 2);  // rand sample
                Recorder.Random.Bytes(4);                      // rand bytes
                Recorder.Random.NextDouble();                  // rand float
                Recorder.Random.NextInt(100);                  // rand int
                var at = Recorder.Clock.Now();                 // now
                Recorder.Clock.Mono();                         // perf

                Recorder.DbWrite("set", $"store.set(greeted:{user})",
                    new object?[] { new Dictionary<string, object?> { ["at"] = at } }, () => { });

                return new Dictionary<string, object?> { ["name"] = doc.Name };
            });

        public static object? Explode(IStore store, string user) =>
            Recorder.Record("explode", new { user }, () => (object?)store.Get(user));

        public static object? Signup(IStore store, string email, string password) =>
            Recorder.Record("signup", new { email, password }, () =>
            {
                var acct = store.CreateAccount(email, new Dictionary<string, object?> { ["password"] = password });
                return new Dictionary<string, object?>
                {
                    ["email"] = acct.Email,
                    ["password"] = acct.Password, // redacted by field name on the way to the tape
                };
            });

        /// <summary>The canonical `enrol` scenario, identical across all six runtimes so a reader
        /// recovers the same account whoever wrote the tape.</summary>
        public static object? Enrol(IStore store, string user, string password, bool note = true) =>
            Recorder.Record("enrol", new { user, password }, () =>
            {
                var started = Recorder.Clock.Now();
                return Recorder.Span("enrol", new { user, started, password }, () =>
                {
                    // A chained read, not an effect: the canonical scenario puts a `db` event
                    // inside a span, which is the one enclosure a reader most wants to see and
                    // the one an fx-only span never demonstrates.
                    var snap = Recorder.Span("load_corpus", () =>
                    {
                        var sig = $"collection(\"users\").document(\"{user}\")";
                        return Recorder.DbRead("get", sig, () => new List<Snapshot>
                        {
                            Snapshot.Of(user, new Dictionary<string, object?> { ["name"] = "Alice", ["x"] = 3 }),
                        })[0];
                    });
                    if (note) Recorder.Note("corpus_read", new { found = snap.Exists });

                    try
                    {
                        Recorder.Span("register", new { password }, () =>
                        {
                            store.Set($"user:{user}", new Dictionary<string, object?> { ["password"] = password });
                            store.Boom(user);
                        });
                    }
                    catch (ToyError e)
                    {
                        Recorder.Note("registration_failed", new { why = e.Message });
                    }

                    var data = snap.Data as IDictionary<string, object?>;
                    return new Dictionary<string, object?>
                    {
                        ["user"] = user,
                        ["name"] = data == null ? null : data["name"],
                    };
                });
            });
    }
}
