"""Explicitly loaded pytest observer for a fresh dependency-contract experiment."""
from __future__ import annotations

import importlib
import json
import platform
import sys
from pathlib import Path

import pytest

_STATE = pytest.StashKey[dict]()


def pytest_addoption(parser):
    parser.addoption('--dependency-contract-plan', type=Path)


def pytest_configure(config):
    path = config.getoption('--dependency-contract-plan')
    plan = json.loads(path.read_bytes())
    modules = {role: importlib.import_module(plan['spec'][f'{role}_module'])
               for role in ('provider', 'consumer')}
    state = {'plan': plan, 'modules': modules, 'collected': {}, 'reports': [],
             'deselected': [], 'violations': [], 'path': path}
    config.stash[_STATE] = state
    _verify_bindings(state)


def _verify_origins(state):
    plan = state['plan']
    for role, module in state['modules'].items():
        expected = Path(plan['root']) / plan['spec'][f'{role}_source']
        assert Path(module.__file__).resolve() == expected.resolve(), 'Wrong module origin'
        assert sys.modules[plan['spec'][f'{role}_module']] is module, 'Replaced module'


def _verify_bindings(state):
    _verify_origins(state)
    provider = state['modules']['provider']
    consumer = state['modules']['consumer']
    for local, public in state['plan']['spec']['bindings'].items():
        assert getattr(consumer, local) is getattr(provider, public), 'Changed public binding'


def pytest_collection_finish(session):
    state = session.config.stash[_STATE]
    root = Path(state['plan']['root'])
    state['collected'] = {item.nodeid: item.path.resolve().relative_to(root).as_posix()
                          for item in session.items}


def pytest_deselected(items):
    if items:
        items[0].config.stash[_STATE]['deselected'].extend(item.nodeid for item in items)


def pytest_runtest_call(item):
    state = item.config.stash[_STATE]
    _verify_bindings(state)
    fixture = item.funcargs.get('monkeypatch')
    if fixture is not None:
        assert type(fixture) is pytest.MonkeyPatch, 'Substituted monkeypatch fixture'


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    yield
    _verify_bindings(item.config.stash[_STATE])


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    item.config.stash[_STATE]['reports'].append({
        'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome,
        'xfail': hasattr(report, 'wasxfail'),
    })


def pytest_sessionfinish(session, exitstatus):
    state = session.config.stash[_STATE]
    _verify_bindings(state)
    report = {key: state[key] for key in ('collected', 'reports', 'deselected', 'violations')}
    report.update(exitstatus=int(exitstatus), python=sys.version, executable=sys.executable,
                  platform=platform.platform(), pytest=pytest.__version__)
    destination = state['path'].with_name('probe-result.json')
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
