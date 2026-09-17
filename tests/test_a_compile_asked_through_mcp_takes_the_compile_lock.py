"""The in-process compile entry is guarded exactly as the command-line one.

Research: `docs/research/2026-09-17-every-compile-takes-the-compile-lock.md`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Runs in its own interpreter so the module-level paths come from the
# environment, as they do in the MCP server. The parent of that interpreter is
# this test process: a live PID that is not the compile's own.
HELD_BY_ANOTHER = """
import json, os, sys
sys.path.insert(0, sys.argv[1])
import compile_memory, maybe_compile, memory_state
maybe_compile._write_lock(os.getppid())
code = compile_memory.run_pending_compile()
state = memory_state.load_state()
held = maybe_compile._read_lock()["pid"] == os.getppid()
print(json.dumps([code, state.get("last_compile_status"), "last_compile_refused_reason" in state, held]))
"""

FREE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import compile_memory, llm_client, maybe_compile, memory_state
seen = []
def observed(args, **bounds):
    seen.extend([llm_client._timeout_s(), maybe_compile._lock_state()[0], memory_state.load_state()["last_compile_status"]])
    return 0
compile_memory._run = observed
code = compile_memory.run_pending_compile()
print(json.dumps([code, *seen, maybe_compile._lock_state()[0]]))
"""


def _in_a_fresh_vault(tmp_path: Path, program: str) -> list:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (tmp_path / "state" / "run").mkdir(parents=True)
    environment = {
        **os.environ,
        "LLM_WIKI_ROOT": str(root),
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"),
        "MEMORY_LLM_PROVIDER": "fake",
    }
    environment.pop("MEMORY_LLM_TIMEOUT_S", None)
    done = subprocess.run(
        [sys.executable, "-c", program, str(SCRIPTS)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_an_in_process_compile_is_refused_while_another_compile_holds_the_lock(tmp_path):
    outcome = _in_a_fresh_vault(tmp_path, HELD_BY_ANOTHER)

    # Refused, the holder's status untouched, the refusal recorded, the lock still the holder's.
    assert outcome == [1, None, True, True]


def test_an_in_process_compile_runs_locked_stamped_and_under_the_compile_ceiling(tmp_path):
    import compile_memory

    outcome = _in_a_fresh_vault(tmp_path, FREE)

    assert outcome == [0, compile_memory.COMPILE_PROVIDER_CEILING_S, "live", "running", "absent"]
