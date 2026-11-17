"""Helpers shared by test_sync_memory.py and the test files importing them."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


SYNC_SCRIPT = ROOT / "scripts" / "sync_memory.py"


def _load_sync_memory():
    spec = importlib.util.spec_from_file_location("sync_memory_under_test", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
