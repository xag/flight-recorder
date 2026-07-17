using System.Collections.Generic;

namespace FlightRecorder
{
    /// <summary>A document snapshot in the shape a well-behaved consumer reads: identity,
    /// existence, data. The only surface a chain read records.</summary>
    public sealed class Snapshot
    {
        public string? Id { get; }
        public bool Exists { get; }
        public object? Data { get; }

        public Snapshot(string? id, bool exists, object? data)
        {
            Id = id;
            Exists = exists;
            Data = data;
        }

        /// <summary>A present document holding <paramref name="data"/>.</summary>
        public static Snapshot Of(string? id, object? data) => new Snapshot(id, true, data);

        /// <summary>A document that does not exist.</summary>
        public static Snapshot Missing(string? id) => new Snapshot(id, false, null);

        internal static Snapshot FromJsonable(object? v)
        {
            if (v is IDictionary<string, object?> m)
            {
                var id = m.TryGetValue("id", out var i) ? i as string : null;
                var exists = m.TryGetValue("exists", out var e) && e is bool b && b;
                var data = m.TryGetValue("data", out var d) ? Serial.FromJsonable(d) : null;
                return new Snapshot(id, exists, data);
            }
            return new Snapshot(null, false, null);
        }
    }
}
