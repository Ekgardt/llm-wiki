"""One pipeline phase of the zero-silent-loss durability stand, in its own process.

The orchestrator (`benchmark/run_durability.py`) launches this driver with the
trial vault named in the environment (`LLM_WIKI_ROOT`, `LLM_WIKI_STATE_ROOT`,
`MEMORY_LLM_PROVIDER=fake`) and, for a killed trial, a stage name at which the
process shoots itself with SIGKILL. SIGKILL cannot be caught, so every
try/except boundary in the product is bypassed exactly the way a real process
death bypasses it.

The driver calls product entry points only and reimplements none of them:

- ``produce`` runs ``integration_adapter.publish_capture_intent_from_payload``,
  the adapter's durable session_end publication (intent files, intent index,
  capture task enqueue).
- ``work`` runs ``integration_adapter.main(["--capture-worker"])`` — the same
  host-safe boundary the detached capture worker runs behind, including its
  swallowed-exception capture-failure trace.

A crash point wraps one product function at its public module or class
attribute: ``before`` dies on entry, ``after`` dies once the call has returned,
before the caller can resume. Both simulate process death at that boundary,
not a power failure — nothing here tests fsync against the disk.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

#: stage name -> (module, dotted attribute) of the boundary the kill interrupts.
#: `_QueueV3CandidateReader` is the class `active_memory_queue` actually returns
#: on an adopted vault; wrapping `MemoryQueue` would miss the live path.
_QUEUE = "_QueueV3CandidateReader"
STAGE_TARGETS = {
    "publish-intent": ("memory_queue", f"{_QUEUE}.index_capture_intent_pending"),
    "intent-ready": ("memory_queue", f"{_QUEUE}.mark_capture_intent_ready"),
    "enqueue": ("memory_queue", f"{_QUEUE}.enqueue_capture_task_replay_safe"),
    "claim": ("memory_queue", f"{_QUEUE}.claim_capture"),
    "record-write": ("session_evidence", "write_session_evidence"),
    "classifier": ("llm_client", "call_llm_result"),
    "decision-publish": ("memory_queue", f"{_QUEUE}.publish_semantic_decision"),
    "markdown-commit": ("markdown_transaction", "append_captured_knowledge"),
    "terminal-publish": ("memory_queue", f"{_QUEUE}.complete_capture_terminal"),
    # The whole fenced publication: "before" dies with nothing durable yet
    # (only the host transcript holds the content), "after" dies with intent,
    # task, and released ownership — the crash the queue is built to replay.
    "publish-return": ("integration_adapter", "_publish_capture_files_and_task"),
}
#: stages that fire inside the producer process; the rest fire in the worker.
PRODUCER_STAGES = frozenset(
    {"publish-intent", "intent-ready", "enqueue", "publish-return"}
)
CRASH_POINTS = ("before", "after")
STAGE_ENV = "LLMWIKI_DURABILITY_CRASH_STAGE"
POINT_ENV = "LLMWIKI_DURABILITY_CRASH_POINT"


# What the kill is on each host, stated rather than assumed. On POSIX it is
# SIGKILL: uncatchable, no finalisers, no flush. Windows has no such signal —
# `signal.SIGKILL` does not exist there, and `_die` used to raise
# `AttributeError` and let the child carry on, so on 2026-08-30 every Windows
# shard reported `kill_observed=False` while nothing had been killed at all.
# `os._exit` is the nearest honest equivalent: no Python finaliser, no atexit,
# no buffered flush. It is not the same thing — the OS still closes handles in
# its own orderly way — so a Windows run of this stand is weaker evidence than a
# POSIX one and must not be reported as equal to it.
KILL_EXIT_CODE = 137  # 128 + 9, the shell's spelling of "killed by SIGKILL"


def _die() -> None:
    if hasattr(signal, "SIGKILL"):
        os.kill(os.getpid(), signal.SIGKILL)
    os._exit(KILL_EXIT_CODE)


def _holder(module_name: str, dotted: str) -> tuple[object, str]:
    module = __import__(module_name)
    *heads, attribute = dotted.split(".")
    target: object = module
    for head in heads:
        target = getattr(target, head)
    return target, attribute


def _crashing(original: object, point: str) -> object:
    def wrapper(*args: object, **kwargs: object) -> object:
        if point == "before":
            _die()
        result = original(*args, **kwargs)  # type: ignore[operator]
        _die()
        return result

    return wrapper


def arm_crash(stage: str | None, point: str) -> None:
    """Replace one product boundary with a self-SIGKILL wrapper."""
    if stage is None:
        return
    if point not in CRASH_POINTS:
        raise ValueError(f"unknown crash point: {point}")
    target, attribute = _holder(*STAGE_TARGETS[stage])
    setattr(target, attribute, _crashing(getattr(target, attribute), point))


def _run_produce(payload: dict) -> dict:
    import integration_adapter

    intent_id = integration_adapter.publish_capture_intent_from_payload(
        "claude", "session_end", payload
    )
    return {"intent_id": intent_id}


def _run_work(_payload: dict) -> dict:
    import integration_adapter

    return {"worker_rc": integration_adapter.main(["--capture-worker"])}


_MODES = {"produce": _run_produce, "work": _run_work}


def main(argv: list[str]) -> int:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    mode = _MODES[argv[1]]
    payload = json.loads(sys.stdin.read() or "{}")
    arm_crash(os.environ.get(STAGE_ENV) or None, os.environ.get(POINT_ENV, "before"))
    print(json.dumps(mode(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
