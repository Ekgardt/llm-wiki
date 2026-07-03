"""Persistent queue for deferred memory-pipeline tasks.

When a memory script (compile_memory, flush_memory, etc.) needs an LLM
but no backend is currently available (no Codex CLI, no OpenCode server,
no Claude, no Ollama, no API key), the task is **enqueued** here instead
of being silently dropped.

The queue is drained by:
  - OpenCode plugin's session.created handler (uses OpenCode SDK)
  - Codex wrapper after memory capture (uses codex exec)
  - Claude Code SessionStart hook (uses claude-agent-sdk)
  - Manual `uv run python scripts/memory_queue.py drain`

Storage: `$LLM_WIKI_STATE_ROOT/memory-pipeline/queue/*.json`
Each file is one task, atomic via tmp+rename. Queue is crash-safe.

Task schema:
    {
      "id": "<uuid>",
      "type": "compile" | "classify" | "lint_contradictions" | "query",
      "enqueued_at": "<iso8601>",
      "attempts": 0,
      "last_attempt_at": null,
      "payload": { ... type-specific fields ... }
    }
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _queue_dir() -> Path:
    state_root = Path(
        os.environ.get("LLM_WIKI_STATE_ROOT", str(Path(__file__).resolve().parent.parent.parent / "LLM-wiki-state"))
    )
    q = state_root / "memory-pipeline" / "queue"
    q.mkdir(parents=True, exist_ok=True)
    return q


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Add a task to the persistent queue. Returns the task id.

    Safe to call from any process — atomic file write via tmp+rename.
    """
    task_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    task = {
        "id": task_id,
        "type": task_type,
        "enqueued_at": datetime.now().isoformat(timespec="seconds"),
        "attempts": 0,
        "last_attempt_at": None,
        "payload": payload,
    }
    target = _queue_dir() / f"{task_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return task_id


def list_pending(max_age_days: int | None = None) -> list[dict[str, Any]]:
    """Return all pending tasks, oldest first.

    `max_age_days` filters out tasks older than N days (avoid infinite
    buildup of unservable tasks). None = no filter.
    """
    out: list[dict[str, Any]] = []
    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now().timestamp() - (max_age_days * 86400)
    for p in sorted(_queue_dir().glob("*.json")):
        try:
            task = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if cutoff is not None:
            try:
                enq = datetime.fromisoformat(task["enqueued_at"]).timestamp()
                if enq < cutoff:
                    continue
            except (KeyError, ValueError):
                pass
        task["_path"] = str(p)
        out.append(task)
    return out


def mark_attempt(task_id: str, success: bool) -> None:
    """Update task's attempt counter (failure) or delete (success)."""
    qdir = _queue_dir()
    # Find task by id (filename starts with the id).
    candidates = list(qdir.glob(f"{task_id}*.json"))
    if not candidates:
        return
    path = candidates[0]
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if success:
        try:
            path.unlink()
        except OSError:
            pass
        return
    # Failure: bump attempts, stamp last_attempt_at, re-write atomically.
    task["attempts"] = int(task.get("attempts", 0)) + 1
    task["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def drain_with(processor: Callable[[dict], bool], max_tasks: int = 10) -> dict[str, int]:
    """Drain the queue using a caller-provided processor.

    `processor(task)` must return True on success (task will be deleted),
    False on failure (task will be re-queued with bumped attempt count).

    Stops after `max_tasks` (default 10) to bound work per drain session.
    Returns counts: {"ok": N, "failed": M, "skipped": K}.
    """
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    pending = list_pending()
    for task in pending[:max_tasks]:
        # Skip tasks that have failed too many times — they need human attention.
        if task.get("attempts", 0) >= 5:
            counts["skipped"] += 1
            continue
        # Skip tasks that were attempted in the last 60s (backoff).
        last = task.get("last_attempt_at")
        if last:
            try:
                age = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                if age < 60:
                    counts["skipped"] += 1
                    continue
            except (ValueError, TypeError):
                pass
        try:
            ok = bool(processor(task))
        except Exception as e:  # noqa: BLE001
            print(f"memory_queue: processor raised {type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        mark_attempt(task["id"], ok)
        if ok:
            counts["ok"] += 1
        else:
            counts["failed"] += 1
    return counts


def status() -> dict[str, Any]:
    """Snapshot of queue health — for the SessionStart metacognitive block."""
    pending = list_pending()
    by_type: dict[str, int] = {}
    failed_count = 0
    for t in pending:
        by_type[t.get("type", "unknown")] = by_type.get(t.get("type", "unknown"), 0) + 1
        if t.get("attempts", 0) >= 5:
            failed_count += 1
    return {
        "pending_total": len(pending),
        "by_type": by_type,
        "permanently_failed": failed_count,
        "queue_dir": str(_queue_dir()),
    }


# ---------------------------------------------------------------------------
# CLI for manual drain / inspection
# ---------------------------------------------------------------------------


def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["list", "status", "drain", "clear-failed"])
    args = p.parse_args()

    if args.command == "list":
        for t in list_pending():
            print(
                f"  {t['id']}  type={t['type']}  attempts={t.get('attempts', 0)}  "
                f"enqueued={t['enqueued_at']}"
            )
        return 0

    if args.command == "status":
        s = status()
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return 0

    if args.command == "drain":
        # Manual drain uses llm_client (auto-detect backend).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from llm_client import call_llm
        except ImportError:
            print("llm_client not available", file=sys.stderr)
            return 2

        def processor(task: dict) -> bool:
            task_type = task.get("type")
            payload = task.get("payload", {})
            if task_type == "query":
                # Free-form LLM call, store result back to payload path.
                prompt = payload.get("prompt", "")
                sys_prompt = payload.get("system_prompt", "")
                out_path = payload.get("output_path")
                if not (prompt and out_path):
                    return False
                result = call_llm(prompt, sys_prompt, max_tokens=4000)
                if not result:
                    return False
                try:
                    Path(out_path).write_text(result, encoding="utf-8")
                    return True
                except OSError:
                    return False
            # Other task types (compile/classify) have richer Python-side
            # logic — caller should drain via the specific script, not here.
            print(
                f"  skipping {task['id']}: type={task_type} not supported in manual drain",
                file=sys.stderr,
            )
            return False

        counts = drain_with(processor, max_tasks=20)
        print(f"drain complete: {counts}")
        return 0

    if args.command == "clear-failed":
        cleared = 0
        for t in list_pending():
            if t.get("attempts", 0) >= 5:
                try:
                    Path(t["_path"]).unlink()
                    cleared += 1
                except OSError:
                    pass
        print(f"cleared {cleared} permanently-failed task(s)")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
