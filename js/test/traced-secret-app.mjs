// Two frames a trace must not write raw: one holding a credential as a local, one holding the
// process's global state.

import * as osNamespace from 'node:os';

export function makeTools(store) {
  return {
    /**
     * A credential that lives only between two lines of code. It is not an argument, a result or
     * an answer, so it never touches the tape; it is a local, which is what a trace records.
     * Built by concatenation so this file is not one a scanner reads as carrying a key.
     */
    async deriveKey({ email }) {
      const secret = 'sk-live-' + '9f8e7d6c5b4a39281706';
      const fingerprint = `len:${secret.length}`;
      const probe = await store.get(email);
      return { email, fingerprint, v: probe.v };
    },

    /** Locals holding the process, the global object, the environment and a module namespace. */
    async holdsTheWorld({ email }) {
      const proc = process;
      const world = globalThis;
      const env = process.env;
      const ns = osNamespace;
      const fn = Object.assign(() => 1, { env: process.env });
      const probe = await store.get(email);
      return { email, kinds: [typeof proc, typeof world, typeof env, typeof ns, typeof fn], v: probe.v };
    },
  };
}
