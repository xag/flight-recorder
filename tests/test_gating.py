"""The per-call gate (issue #3): `enabled` as a callable is consulted on every tool call,
so one running process can record one user's request and leave the rest of its traffic
alone — and a gate that never says yes leaves no session file behind at all."""

import asyncio
from pathlib import Path

import pytest

import flight_recorder as fr
from tests import toy_tools
from tests.test_roundtrip import make_boundary


@pytest.fixture
def uninstalled():
    yield
    fr.uninstall()


def _calls():
    session = fr.session_path()
    if session is None:
        return []
    _, calls = fr.load_session(session)
    return calls


def test_gate_records_only_the_calls_it_admits(uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: kwargs.get("email") == "wanted@example.com")

    toy_tools.greet("ignored@example.com", count=2)
    assert fr.session_path() is None  # nothing admitted yet: no file, not even a header

    toy_tools.greet("wanted@example.com", count=2)
    toy_tools.greet("ignored@example.com", count=1)

    calls = _calls()
    assert len(calls) == 1
    assert calls[0]["fn"] == "greet"
    assert calls[0]["kwargs"]["email"] == "wanted@example.com"


def test_gate_can_select_by_tool_name(uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: fn == "remote_sum")

    toy_tools.greet("t@example.com", count=2)
    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))

    calls = _calls()
    assert [c["fn"] for c in calls] == ["remote_sum"]


def test_a_gate_that_never_fires_leaves_no_session_file(uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: False)

    toy_tools.greet("t@example.com", count=2)
    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))

    assert fr.session_path() is None
    assert list(tmp_path.glob("*.jsonl")) == []


def test_an_admitted_call_still_records_its_boundary_events(uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: True)
    asyncio.run(toy_tools.remote_sum("t@example.com", "abc", "wxyz"))
    session = fr.session_path()
    fr.uninstall()

    from tests.test_roundtrip import ToyAdapter
    report = fr.replay_call(session, 0, ToyAdapter(), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_concurrent_async_calls_are_gated_and_buffered_independently(uninstalled, tmp_path):
    # The gate decides per call, and `_active` is a ContextVar, so twelve interleaved tasks
    # must produce six clean recordings — not one recording with everyone's events in it.
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: kwargs["email"].startswith("rec"))

    async def all_of_them():
        await asyncio.gather(*[toy_tools.remote_sum(f"{p}{i}@x.com", "ab", "cd")
                               for i in range(6) for p in ("rec", "skip")])

    asyncio.run(all_of_them())

    calls = _calls()
    assert sorted(c["kwargs"]["email"] for c in calls) == [f"rec{i}@x.com" for i in range(6)]
    for c in calls:
        assert [e["fn"].split(".")[-1] for e in c["events"]] == [
            "fetch_remote", "fetch_remote", "maybe_fail", "read_config"]

    session = fr.session_path()
    fr.uninstall()
    from tests.test_roundtrip import ToyAdapter
    for i in range(len(calls)):
        report = fr.replay_call(session, i, ToyAdapter(), None)
        assert report.ok, (i, report.divergence, report.result_diff)


def test_the_outermost_tool_call_decides_for_the_whole_tree(uninstalled, tmp_path):
    # outer() calls greet(). The gate admits outer, so greet must fold into outer's record
    # rather than opening a second one — and the gate is never asked about greet at all.
    asked = []

    def gate(fn, kwargs):
        asked.append(fn)
        return fn == "outer"

    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=gate)
    toy_tools.outer("t@example.com")

    assert asked == ["outer"]
    calls = _calls()
    assert [c["fn"] for c in calls] == ["outer"]
    assert calls[0]["events"], "greet's boundary events belong to outer's record"


def test_a_declined_outer_call_does_not_let_its_inner_tool_be_recorded(uninstalled, tmp_path):
    # The gate names greet, but greet is only ever reached through outer, which is declined.
    # Recording greet standalone here would yield a record starting mid-request.
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: fn == "greet")
    toy_tools.outer("t@example.com")

    assert fr.session_path() is None


def test_a_gate_naming_an_inner_tool_still_records_it_when_called_directly(
        uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: fn == "greet")
    toy_tools.greet("t@example.com", count=2)  # top-level, so it is the outermost call

    assert [c["fn"] for c in _calls()] == ["greet"]


def test_gate_is_consulted_once_per_call_with_bound_kwargs(uninstalled, tmp_path):
    seen = []

    def gate(fn, kwargs):
        seen.append((fn, dict(kwargs)))
        return False

    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=gate)
    toy_tools.greet("t@example.com")  # count defaults to 2

    # once, with defaults applied — the gate sees the call as the recording would store it
    assert seen == [("greet", {"email": "t@example.com", "count": 2})]


def test_a_raising_gate_never_breaks_the_call(uninstalled, tmp_path):
    def gate(fn, kwargs):
        raise RuntimeError("the gate itself is broken")

    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=gate)
    out = toy_tools.greet("t@example.com", count=2)  # must not raise

    assert "t@example.com" in out
    assert fr.session_path() is None  # and the call was not recorded


def test_effects_pass_through_untouched_when_the_gate_declines(uninstalled, tmp_path):
    # The boundary is patched under a gate (the wrappers must be in place), so this pins
    # that a declined call still reaches the real effect and gets its real answer.
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: False)
    out = asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))
    assert out["email"] == "t@example.com" and out["cfg"] == "cfg:mode"


def test_static_true_still_opens_the_session_eagerly(uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    assert fr.session_path() is not None  # header written at install, as before


def test_installing_twice_under_a_gate_does_not_double_wrap(uninstalled, tmp_path):
    # Install is idempotent. Under a gate there is no recorder yet to detect the first
    # install by, so the guard has to notice the armed-but-unfired state instead.
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: True)
    wrapped = toy_tools.greet
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path),
               enabled=lambda fn, kwargs: True)
    assert toy_tools.greet is wrapped

    toy_tools.greet("t@example.com", count=2)
    assert len(_calls()) == 1  # one record, not two nested ones


def test_install_disabled_is_still_a_total_noop(tmp_path):
    orig = toy_tools.greet
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=False)
    assert toy_tools.greet is orig
    assert fr.session_path() is None


def test_a_failed_install_rolls_back_so_the_retry_is_a_fresh_install(uninstalled, tmp_path,
                                                                     monkeypatch):
    # First install dies opening the session file. It must leave nothing armed behind, or
    # the retry would hit the idempotence guard and silently record nothing forever.
    calls = {"n": 0}
    real_mkdir = Path.mkdir

    def flaky_mkdir(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("disk is briefly unwritable")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)
    with pytest.raises(PermissionError):
        fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)

    assert toy_tools.greet.__module__ == "tests.toy_tools"  # boundary unpatched again
    monkeypatch.undo()

    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    toy_tools.greet("t@example.com", count=2)
    assert [c["fn"] for c in _calls()] == ["greet"]


def test_install_mcp_gates_on_the_registered_tool_name_not_the_function_name(
        uninstalled, tmp_path):
    # A registry may alias a tool away from its Python def name. The gate is asked about
    # the name clients call, and that is the name the recording stores.
    class _Tool:
        def __init__(self, name, fn):
            self.name, self.fn = name, fn

    def _do_greet(email: str, count: int = 2) -> str:  # the def name nobody calls it by
        return toy_tools.greet(email, count)

    class _Server:
        class _Mgr:
            _tools = {"public_greet": _Tool("public_greet", _do_greet)}
        _tool_manager = _Mgr()

    server = _Server()
    fr.install_mcp(make_boundary(), server, directory=str(tmp_path),
                   enabled=lambda fn, kwargs: fn == "public_greet")

    server._tool_manager._tools["public_greet"].fn("t@example.com", 2)

    calls = _calls()
    assert [c["fn"] for c in calls] == ["public_greet"]
