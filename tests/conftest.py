"""Shared pytest fixtures and environment bootstrap.

Makes the suite **hermetic** — runs green from a fresh clone without
any pre-set environment variables or pre-existing runtime state. The
colleague audit flagged `pytest -q tests` failing on clean clone because:

  1. Subprocess-invoked hooks (e.g. `session_end_project_tag.py`) read
     `LLM_WIKI_ROOT` to decide whether to operate at all; absent env
     → silent no-op → hook never writes → test expecting a write fails.
  2. `state.json` might not exist in a fresh runtime-state tree.

This conftest fixes both:
  - Injects `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT` into the process env
    (and therefore all subprocess children) for the duration of the
    test session.
  - Ensures the state-root directory and a skeleton `state.json` exist.

Does NOT override values the user explicitly sets — so a CI job that
wants a fully isolated runtime can still point `LLM_WIKI_STATE_ROOT`
at a temp dir before invoking pytest.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT_ROOT / "scripts"

# Make scripts/ importable — avoids needing to install the vault as a package.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# --- Environment bootstrap (session-wide, before any test runs) --------------

# 1. LLM_WIKI_ROOT — hook scripts check this before doing any work.
#    Without it, session_end_project_tag.py silently no-ops, and tests
#    that assert "the hook wrote to daily log" fail.
os.environ.setdefault("LLM_WIKI_ROOT", str(VAULT_ROOT))

# 2. LLM_WIKI_STATE_ROOT — default to <vault>/../LLM-wiki-state/ (matches
#    memory_state.py fallback) and ensure it + state.json exist. This
#    lets compile-failure test snapshot/restore even on a fresh clone
#    that has never run a compile.
_default_state_root = VAULT_ROOT.parent / "LLM-wiki-state"
os.environ.setdefault("LLM_WIKI_STATE_ROOT", str(_default_state_root))

_state_dir = Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "memory-state"
_state_dir.mkdir(parents=True, exist_ok=True)
_state_file = _state_dir / "state.json"
if not _state_file.exists():
    _state_file.write_text("{}\n", encoding="utf-8")
