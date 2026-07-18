package flightrecorder

// Invariants: a claim about EVERY execution, written once and checked against any recording — so
// it can condemn the very first observation of a bug, which no recording can. A bug replays
// bit-for-bit forever; only a spec can call it wrong.
//
// An invariant consumes the tape, and the tape is shared, so a recording made by ANY
// implementation can be judged against Go invariants. It asserts over the replayed result, the
// writes the code performed, and the claims it made — the trajectory of one execution.

import "fmt"

// Trajectory is what an invariant asserts on: the replayed execution, not the recorded one (the
// recorded result is the thing being questioned).
type Trajectory struct {
	Result any              // the replayed result, jsonable
	Error  string           // the replayed error, or "" if none
	Writes []map[string]any // every write the replayed code performed (op/sig/args)
	Sems   []SemPair        // the replayed semantic claims, in order
	Kwargs map[string]any   // the call's kwargs, revived
	// Trace is the execution seen from the inside: every local, on every executed line. It is
	// what makes "level never excludes the whole corpus" a lookup rather than an inference, and
	// it is the only way to condemn an output that is wrong while being entirely self-consistent
	// with itself. Empty outside an instrumented run — a claim that reads it should say so
	// rather than pass vacuously.
	Trace *Trace
}

// An Invariant is a named claim. Check returns an error (or panics) to condemn the trajectory.
type Invariant struct {
	Name  string
	Check func(*Trajectory) error
}

// NewInvariant is a small constructor: NewInvariant("never done with words left", func(t) {...}).
func NewInvariant(name string, check func(*Trajectory) error) Invariant {
	return Invariant{Name: name, Check: check}
}

// InvariantResult is one invariant's verdict against one trajectory.
type InvariantResult struct {
	Name string
	OK   bool
	Err  string
}

// InvariantReport pairs the replay with each invariant's verdict.
type InvariantReport struct {
	Replay  *ReplayReport
	Results []InvariantResult
}

// OK iff every invariant held.
func (r InvariantReport) OK() bool {
	for _, x := range r.Results {
		if !x.OK {
			return false
		}
	}
	return true
}

// CheckInvariants replays call `index` of the tape at `path` and checks every invariant against
// the resulting trajectory.
func CheckInvariants(path string, index int, resolve Resolver, invariants []Invariant) (*InvariantReport, error) {
	rep, err := Replay(path, index, resolve)
	if err != nil {
		return nil, err
	}
	return runInvariants(rep, invariants), nil
}

// CheckInvariantsCall checks invariants against a replay of a (possibly mutated) call view — the
// mutation + invariant flow: empty a result, mark it a probe, and assert the code still holds.
func CheckInvariantsCall(cv *CallView, resolve Resolver, probe bool, invariants []Invariant) (*InvariantReport, error) {
	rep, err := ReplayCall(cv, resolve, probe)
	if err != nil {
		return nil, err
	}
	return runInvariants(rep, invariants), nil
}

func runInvariants(rep *ReplayReport, invariants []Invariant) *InvariantReport {
	traj := &Trajectory{
		Result: rep.ReplayedResult,
		Error:  rep.ReplayedError,
		Writes: rep.Writes,
		Sems:   rep.SemsReplayed,
		Kwargs: rep.Kwargs,
		Trace:  rep.Trace,
	}
	out := &InvariantReport{Replay: rep}
	for _, inv := range invariants {
		err := safeInvariant(inv.Check, traj)
		res := InvariantResult{Name: inv.Name, OK: err == nil}
		if err != nil {
			res.Err = err.Error()
		}
		out.Results = append(out.Results, res)
	}
	return out
}

// safeInvariant turns a panicking assertion (the natural Go idiom is a failed check that panics
// or returns an error) into a verdict, so one broken invariant cannot take the run down.
func safeInvariant(check func(*Trajectory) error, t *Trajectory) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("%v", r)
		}
	}()
	return check(t)
}
