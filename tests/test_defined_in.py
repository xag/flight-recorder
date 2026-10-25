"""A module that gathers the tools of several records each under the name it holds it by
(`install(..., defined_in=...)`); a function it imports from anywhere else stays a helper."""

import json
import sys
import types

import pytest

import flight_recorder as fr
from tests.test_roundtrip import make_boundary


@pytest.fixture
def modules():
    part = types.ModuleType("toy_part")
    exec("def greet(svc, name):\n    return 'hello ' + name\n", part.__dict__)
    helper = types.ModuleType("toy_helper")
    exec("def shout(svc, text):\n    return text.upper()\n", helper.__dict__)
    gathered = types.ModuleType("toy_gathered")
    exec("def own(svc):\n    return 'own'\n", gathered.__dict__)
    gathered.greet, gathered.shout = part.greet, helper.shout
    for m in (part, helper, gathered):
        sys.modules[m.__name__] = m
    yield part, helper, gathered
    fr.uninstall()
    for m in (part, helper, gathered):
        sys.modules.pop(m.__name__, None)


def _fns():
    _, calls = fr.load_session(fr.session_path())
    return [c["fn"] for c in calls]


def test_the_tools_of_a_named_module_are_recorded_where_they_are_gathered(modules, tmp_path):
    part, helper, gathered = modules
    fr.install(make_boundary(), gathered, directory=str(tmp_path), defined_in=(part,))
    assert gathered.greet(None, name="mina") == "hello mina"
    assert gathered.own(None) == "own"
    assert gathered.shout(None, text="hi") == "HI"
    assert _fns() == ["greet", "own"]


def test_without_it_a_gathered_function_is_a_helper(modules, tmp_path):
    part, helper, gathered = modules
    fr.install(make_boundary(), gathered, directory=str(tmp_path))
    gathered.greet(None, name="mina")
    gathered.own(None)
    assert _fns() == ["own"]
