using System.Collections.Generic;

namespace FlightRecorder
{
    internal static class DictExtensions
    {
        public static object? GetValueOrNull(this IDictionary<string, object?> d, string key) =>
            d.TryGetValue(key, out var v) ? v : null;
    }
}
