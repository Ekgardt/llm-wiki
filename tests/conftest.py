"""Shared pytest fixtures and environment bootstrap.

Makes the suite **hermetic** — runs green from a fresh clone without
any pre-set environment variables or pre-existing runtime state.

  1. Subprocess-invoked hooks read `LLM_WIKI_ROOT`; absent env → no-op.
  2. State must not pollute the developer's real runtime (production
     lives inside the vault under gitignored `cache/logs/run/`). Tests
     redirect `LLM_WIKI_STATE_ROOT` to a session-scoped pytest temp dir.

Override: set `LLM_WIKI_STATE_ROOT` before pytest AND
`LLM_WIKI_TEST_USE_EXTERNAL_STATE=1` if you need a custom location.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 1. Vault root — always pin to this checkout for hermetic subprocess hooks.
os.environ["LLM_WIKI_ROOT"] = str(VAULT_ROOT)

# 2. Isolated state root OUTSIDE the vault for hermetic tests (production
#    runtime lives inside the vault under gitignored cache/logs/run/, but
#    tests must not mutate those). Uses a session-scoped pytest temp dir
#    so state is fresh per session and cleaned up automatically.
#    Override: set LLM_WIKI_STATE_ROOT before pytest AND
#    LLM_WIKI_TEST_USE_EXTERNAL_STATE=1.
@pytest.fixture(scope="session", autouse=True)
def _isolate_test_state_root(tmp_path_factory):
    """Provide a hermetic, session-scoped state root for every test."""
    if os.environ.get("LLM_WIKI_TEST_USE_EXTERNAL_STATE", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        state_root = tmp_path_factory.mktemp("llm-wiki-test-state")
        os.environ["LLM_WIKI_STATE_ROOT"] = str(state_root)
    else:
        os.environ.setdefault("LLM_WIKI_STATE_ROOT", str(VAULT_ROOT))

    state_dir = Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "run"
    state_dir.mkdir(parents=True, exist_ok=True)
    (Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "logs").mkdir(parents=True, exist_ok=True)
    (Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "cache").mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "state.json"
    if not state_file.exists():
        state_file.write_text("{}\n", encoding="utf-8")


# Default fake provider for any accidental live LLM calls in unit tests.
os.environ.setdefault("MEMORY_LLM_PROVIDER", "fake")
