// netstandard2.0 does not ship the `init`/record support type the compiler needs. Providing
// it here lets this library use modern C# while still targeting the widest runtime surface.

using System.ComponentModel;

namespace System.Runtime.CompilerServices
{
    [EditorBrowsable(EditorBrowsableState.Never)]
    internal static class IsExternalInit { }
}
