// The effect-recording proxy — the .NET form of "wrap what the app holds".
//
// A DispatchProxy forwards every call to the real target and records the named methods as `fx`
// events. Everything not named passes straight through, untouched. `this` is the real target,
// so a client whose methods call each other internally is not double-recorded — the inner call
// goes to the raw object, not back through the proxy.
//
// The one thing a statically-typed runtime must do that Python and Node do not: on replay the
// recorded answer is jsonable, but the method's declared return type is concrete. So a replayed
// answer is coerced back into that type by a JSON round-trip — the code sees a real POCO, not a
// dictionary, and reads it exactly as it did when the world answered.

using System;
using System.Collections.Generic;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text.Json;
using System.Threading.Tasks;

namespace FlightRecorder
{
    internal static class DispatchProxyFactory
    {
        public static T Create<T>() where T : class => DispatchProxy.Create<T, EffectProxy>();
    }

    public class EffectProxy : DispatchProxy
    {
        private object _target = null!;
        private HashSet<string> _methods = null!;
        private string _prefix = "";

        internal void Init(object target, HashSet<string> methods, string prefix)
        {
            _target = target;
            _methods = methods;
            _prefix = prefix;
        }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method == null) throw new ArgumentNullException(nameof(method));
            args ??= Array.Empty<object?>();

            if (!_methods.Contains(method.Name))
                return InvokeReal(method, args);

            var fn = string.IsNullOrEmpty(_prefix) ? method.Name : $"{_prefix}.{method.Name}";
            var ret = method.ReturnType;

            // Task<X>
            if (ret.IsGenericType && ret.GetGenericTypeDefinition() == typeof(Task<>))
            {
                var x = ret.GetGenericArguments()[0];
                var inner = Recorder.EffectAsync(fn, args, async () =>
                {
                    var task = (Task)InvokeReal(method, args)!;
                    await task.ConfigureAwait(false);
                    return task.GetType().GetProperty("Result")?.GetValue(task);
                });
                return AdaptGeneric(x, inner, Recorder.Replaying);
            }

            // Task (no result)
            if (ret == typeof(Task))
            {
                var inner = Recorder.EffectAsync(fn, args, async () =>
                {
                    await ((Task)InvokeReal(method, args)!).ConfigureAwait(false);
                    return (object?)null;
                });
                return AdaptVoid(inner);
            }

            // Synchronous
            var raw = Recorder.Effect(fn, args, () => InvokeReal(method, args));
            if (ret == typeof(void)) return null;
            return Recorder.Replaying ? Coerce(raw, ret) : raw;
        }

        // Reflection wraps a method's own exception in TargetInvocationException; unwrap it so the
        // effect records the real type (a store's NotFound, not an InvocationException) and the app
        // catches what it actually threw.
        private object? InvokeReal(MethodInfo method, object?[] args)
        {
            try { return method.Invoke(_target, args); }
            catch (TargetInvocationException e) when (e.InnerException != null)
            {
                ExceptionDispatchInfo.Capture(e.InnerException).Throw();
                throw; // unreachable
            }
        }

        private static async Task AdaptVoid(Task<object?> inner) => await inner.ConfigureAwait(false);

        private static readonly MethodInfo AdaptMethod =
            typeof(EffectProxy).GetMethod(nameof(Adapt), BindingFlags.NonPublic | BindingFlags.Static)!;

        private static object AdaptGeneric(Type x, Task<object?> inner, bool replaying) =>
            AdaptMethod.MakeGenericMethod(x).Invoke(null, new object[] { inner, replaying })!;

        private static async Task<TX> Adapt<TX>(Task<object?> inner, bool replaying)
        {
            var raw = await inner.ConfigureAwait(false);
            if (!replaying) return (TX)raw!;      // recording: raw is the real typed result
            return (TX)Coerce(raw, typeof(TX))!;  // replay: coerce the jsonable answer back to TX
        }

        private static readonly JsonSerializerOptions CoerceOpts = new JsonSerializerOptions
        {
            // Tapes carry camelCase field names (see Serial.PocoWrite); bind them back into PascalCase
            // .NET properties without ceremony.
            PropertyNameCaseInsensitive = true,
        };

        internal static object? Coerce(object? jsonable, Type target)
        {
            if (jsonable == null) return null;
            if (target.IsInstanceOfType(jsonable)) return jsonable;
            try
            {
                var json = Json.Stringify(jsonable);
                return JsonSerializer.Deserialize(json, target, CoerceOpts);
            }
            catch
            {
                return jsonable;
            }
        }
    }
}
