"""Ordinary runtime type resolution works in a cold process without its facade."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = """
import importlib,sys,typing
from dataclasses import fields
sys.path.insert(0,sys.argv[1])
owner=importlib.import_module(sys.argv[2])
kind=getattr(owner,sys.argv[3])
assert kind.__module__ not in sys.modules, kind.__module__
hints=typing.get_type_hints(kind)
assert set(hints)=={field.name for field in fields(kind)}
assert hints==typing.get_type_hints(kind,vars(owner))
assert kind.__module__ not in sys.modules, kind.__module__
"""


@pytest.mark.parametrize('name', ['ProviderDescriptor', 'LLMResult', 'BackendResponse', 'CodexExecutable', 'CodexPlanningBasis'])
def test_runtime_types_resolve_without_loading_historical_facade(name):
    subprocess.run([sys.executable, '-I', '-c', SCRIPT, str(ROOT / 'scripts'),
                    'llm_wiki.model_records', name], check=True, capture_output=True, text=True, timeout=30)
