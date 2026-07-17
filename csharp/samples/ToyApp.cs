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

        public static object? Greet(IStore store, string user) =>
            Recorder.Record("greet", new { user }, () =>
            {
                var doc = store.Get(user);                     // fx store.get
                var token = Hex(Recorder.Random.Bytes(4));     // rand bytes
                var at = Recorder.Clock.Now();                 // now
                store.Set($"greeted:{user}", new Dictionary<string, object?> { ["at"] = at, ["token"] = token });
                return new Dictionary<string, object?> { ["name"] = doc.Name, ["token"] = token };
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

        public static object? Enrol(IStore store, string email, string password, bool note = true) =>
            Recorder.Record("enrol", new { email, password }, () =>
            {
                var started = Recorder.Clock.Now();
                return Recorder.Span("enrol", new { email, started }, () =>
                {
                    var rows = Recorder.Span("load_corpus", () =>
                    {
                        var sig = $"collection(\"users\").document(\"{email}\").collection(\"items\").where(\"x\", \">\", 0)";
                        var snaps = Recorder.DbRead("stream", sig, () => new List<Snapshot>
                        {
                            Snapshot.Of("0", new Dictionary<string, object?> { ["name"] = "alpha", ["x"] = 1 }),
                            Snapshot.Of("1", new Dictionary<string, object?> { ["name"] = "beta", ["x"] = 2 }),
                            Snapshot.Of("2", new Dictionary<string, object?> { ["name"] = "gamma", ["x"] = 3 }),
                        });
                        return snaps.Count;
                    });
                    if (note) Recorder.Note("corpus_read", new { rows });

                    Account? account = null;
                    try
                    {
                        Recorder.Span("register", new { password }, () =>
                        {
                            account = store.CreateAccount(email, new Dictionary<string, object?> { ["password"] = password });
                            store.MaybeFail(99);
                        });
                    }
                    catch (ToyError e)
                    {
                        Recorder.Note("registration_failed", new { why = e.Message });
                    }

                    return new Dictionary<string, object?>
                    {
                        ["email"] = email,
                        ["account"] = account == null ? null : new Dictionary<string, object?>
                        {
                            ["id"] = account.Id, ["email"] = account.Email, ["password"] = account.Password,
                        },
                    };
                });
            });
    }
}
