"""Shared pytest fixtures and environment bootstrap.

Makes the suite **hermetic** — runs green from a fresh clone without
any pre-set environment variables or pre-existing runtime state.

  1. Subprocess-invoked hooks read `LLM_WIKI_ROOT`; absent env → no-op.
  2. State must not pollute the developer's real `$LLM_WIKI_STATE_ROOT`
     sibling (e.g. `../LLM-wiki-state`). Tests use a vault-local
     `.pytest_cache/llm-wiki-state/` tree by default.

Override: set `LLM_WIKI_STATE_ROOT` **before** pytest if you need a
custom location; setenv is only applied via setdefault.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# 1. Vault root — always pin to this checkout for hermetic subprocess hooks.
os.environ["LLM_WIKI_ROOT"] = str(VAULT_ROOT)

# 2. Isolated state root under .pytest_cache (gitignored). Does not touch
#    the developer's real <parent>/LLM-wiki-state unless they pre-set env
#    AND set LLM_WIKI_TEST_USE_EXTERNAL_STATE=1.
if os.environ.get("LLM_WIKI_TEST_USE_EXTERNAL_STATE", "").lower() not in (
    "1",
    "true",
    "yes",
):
    os.environ["LLM_WIKI_STATE_ROOT"] = str(
        VAULT_ROOT / ".pytest_cache" / "llm-wiki-state"
    )
else:
    os.environ.setdefault(
        "LLM_WIKI_STATE_ROOT",
        str(VAULT_ROOT.parent / "LLM-wiki-state"),
    )

_state_dir = Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "run"
_state_dir.mkdir(parents=True, exist_ok=True)
(Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "logs").mkdir(parents=True, exist_ok=True)
(Path(os.environ["LLM_WIKI_STATE_ROOT"]) / "cache").mkdir(parents=True, exist_ok=True)
_state_file = _state_dir / "state.json"
if not _state_file.exists():
    _state_file.write_text("{}\n", encoding="utf-8")

# Default fake provider for any accidental live LLM calls in unit tests.
os.environ.setdefault("MEMORY_LLM_PROVIDER", "fake")
