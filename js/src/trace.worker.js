// The tracer's other half: a Worker that drives the MAIN thread's V8 debugger.
//
// WHY A WORKER. A same-thread inspector Session cannot observe its own thread: when V8 pauses,
// the thread that would handle `Debugger.paused` is the thread that is paused. Node provides
// `Session.connectToMainThread()` for exactly this, and it is only callable from a Worker.
//
// WHY NOT STEPPING. The obvious design is stepInto/stepOver, walking the code line by line —
// and it will hard-abort the process. Step commands are validated against the isolate's debug
// state, and a command that lands when the isolate is not paused trips
// `Check failed: isolate->debug()->CheckExecutionState()`: a V8 fatal, uncatchable, taking the
// host down with it. Across threads that race is unavoidable.
//
// So: no stepping. A breakpoint on every line of every traced file, and `resume` as the only
// control command — issued solely from inside a paused event, when the isolate is by definition
// paused. The trace is identical; the failure mode is gone.

import { Session } from 'node:inspector';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parentPort, workerData } from 'node:worker_threads';

const session = new Session();
session.connectToMainThread();

const post = (method, params) =>
  new Promise((resolve, reject) =>
    session.post(method, params, (err, res) => (err ? reject(err) : resolve(res))),
  );

const include = workerData.include.map((p) => new RegExp(p));
const observations = [];
const armed = new Set();

const matches = (url) => Boolean(url) && include.some((re) => re.test(url));

/** A traced value: primitives as themselves, everything else by its debugger description. */
function readValue(v) {
  if (!v) return undefined;
  if ('value' in v) return v.value;
  return v.description ?? v.type;
}

// Which scopes hold "the locals".
//
// Not just `local`. In an ASYNC function the body's `const`/`let` do not live in the local
// scope — they must survive an await, so V8 hoists them into a closure context, and reading
// only `local` gives you the parameters and nothing else. (A synchronous function hides this:
// everything is local, and the mistake looks like it works.)
//
// `global` and `module` are excluded: they are the world and the imports, not this execution.
const SCOPES = new Set(['local', 'block', 'closure', 'catch']);

session.on('Debugger.paused', ({ params }) => {
  const frame = params.callFrames[0];
  // `resume` is the ONLY control command, and only ever from inside a pause.
  const done = () => post('Debugger.resume').catch(() => {});

  const scopes = frame.scopeChain.filter(
    (s) => SCOPES.has(s.type) && s.object && s.object.objectId,
  );
  if (!scopes.length) {
    done();
    return;
  }

  // Nearest scope wins: an inner binding shadows an outer one, exactly as the code sees it.
  Promise.all(
    scopes.map((s) =>
      post('Runtime.getProperties', { objectId: s.object.objectId, ownProperties: true })
        .then((r) => r.result)
        .catch(() => []),
    ),
  )
    .then((groups) => {
      const vars = {};
      for (const group of groups.reverse()) {
        for (const p of group) {
          if (!p.value || p.value.type === 'function') continue;
          if (p.value.type === 'undefined') continue;
          vars[p.name] = readValue(p.value);
        }
      }
      observations.push({
        file: frame.url || urlOf.get(frame.location.scriptId) || '',
        line: frame.location.lineNumber + 1,
        fn: frame.functionName || '(top level)',
        vars,
      });
    })
    .catch(() => {})
    .then(done);
});

/** Arm every line of a traced file. Debugger.enable replays scriptParsed for scripts already loaded. */
async function arm(url) {
  if (armed.has(url)) return;
  armed.add(url);

  let source;
  try {
    source = fs.readFileSync(fileURLToPath(url), 'utf8');
  } catch {
    return; // not a file we can read (bundled, virtual): nothing to arm
  }

  const lines = source.split('\n').length;
  for (let ln = 0; ln < lines; ln++) {
    // urlRegex, not scriptId: a breakpoint by URL survives the script being re-parsed.
    await post('Debugger.setBreakpointByUrl', { urlRegex: escapeRegex(url), lineNumber: ln }).catch(() => {});
  }
}

const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const candidates = [];
// V8 reports url:'' on a breakpoint's call frame, so keep the mapping ourselves: without it an
// observation cannot say WHERE it was made, and a trace whose lines have no file is half a trace.
const urlOf = new Map();
session.on('Debugger.scriptParsed', ({ params }) => {
  urlOf.set(params.scriptId, params.url);
  if (matches(params.url)) candidates.push(params.url);
});

// No top-level await: the worker module must finish evaluating, or the inspector responses it
// is waiting on never get pumped and both threads sit there forever.
//
// And "ready" must mean READY. Debugger.enable replays a scriptParsed for every script already
// loaded, but those events arrive after its response, and arming a file is itself a round-trip
// per line. Signalling ready before the breakpoints are installed lets the main thread race past
// code that is not yet armed — which is an empty trace, reported as a successful one.
post('Debugger.enable')
  .then(() => new Promise((r) => setTimeout(r, 25))) // let the replayed scriptParsed events land
  .then(() => Promise.all(candidates.map(arm)))      // …and finish arming before anyone runs
  .then(() => parentPort.postMessage({ ready: true, armed: [...armed] }))
  .catch((e) => parentPort.postMessage({ error: e.message }));

parentPort.on('message', (msg) => {
  if (msg !== 'stop') return;
  post('Debugger.disable')
    .catch(() => {})
    .then(() => parentPort.postMessage({ observations }));
});
