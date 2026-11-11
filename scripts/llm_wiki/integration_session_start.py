"""The session-start maintenance and context an agent receives."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import memory_state
import project_checkpoint
from event_envelope import EventEnvelope
from llm_wiki import integration_delegates, runtime_roots
from llm_wiki.integration_settings import (
    MAINTENANCE_DRAIN_TIMEOUT_SECONDS,
    SCRIPTS_DIR,
    build_session_start_context,
)
from maybe_compile import spawn_compile_if_idle
from project_journal import ProjectStore, recover_project_handoff
from secret_redact import describe_error


def record_activity(
    envelope: EventEnvelope,
    slug: str | None,
    project_dir: Path | None,
) -> bool:
    if not slug or project_dir is None:
        return False
    heartbeat = integration_delegates.run_delegate(
        "heartbeat_record.py",
        {
            "slug": slug,
            "projectRoot": str(project_dir),
            "reason": envelope.payload.get("reason") or envelope.event_type,
            "sessionId": envelope.session,
        },
        project_dir=project_dir,
    )
    return getattr(heartbeat, "returncode", 0) == 0


def _flush_started(result: subprocess.CompletedProcess[str]) -> bool:
    if getattr(result, "returncode", 1) != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("flush_started") is True


def _cleanup_runtime_transient(path: Path) -> None:
    try:
        candidate = path.resolve()
        candidate.relative_to((runtime_roots.state_root() / "cache" / "transient-transcripts").resolve())
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def run_maintenance_command(script: str, argument: str) -> None:
    """Run one session-start drain; a failure is logged, never raised into the hook.

    The exit code and a spawn failure were both dropped, so a drain that failed on
    every session start left nothing to read (audit 2026-09-27 C-3,
    docs/research/2026-09-27-a-capture-that-fails-says-so.md).
    """
    try:
        completed = subprocess.run(
            [sys.executable, str(runtime_roots.vault_root() / "scripts" / script), argument],
            cwd=str(runtime_roots.vault_root()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=MAINTENANCE_DRAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        project_checkpoint.log_hook_error("session-start maintenance", f"{script} {argument}: {describe_error(error)}")
        return
    if completed.returncode != 0:
        project_checkpoint.log_hook_error("session-start maintenance", f"{script} {argument} exited {completed.returncode}")


def run_session_start_maintenance() -> int:
    run_maintenance_command("integration_adapter.py", "--capture-worker")
    run_maintenance_command("memory_queue.py", "work")
    catch_up_missed_nightly()
    try:
        # The open day waits for the nightly; see `memory_state.closed_daily_logs`.
        spawn_compile_if_idle(closed_days_only=True)
    except Exception as error:  # noqa: BLE001 - session start never fails over a compile spawn
        project_checkpoint.log_hook_error("session-start compile", describe_error(error))
    return 0


def catch_up_missed_nightly() -> None:
    """Ask for the nightly when the scheduler's run did not happen today.

    This pass is detached, so the claim and the spawn cost the hook nothing. The
    schedulers catch up where they can — a systemd timer with `Persistent=true`, a
    LaunchAgent at wake, a Windows task with `-StartWhenAvailable` after sign-in — but
    a machine signed out at 03:00 and the explicit cron fallback never do, and until
    now no shipped hook reached this code at all. See
    `docs/research/2026-09-17-a-missed-nightly-is-caught-up-and-codex-keeps-its-stop.md`.
    """
    try:
        from session_start_context import maybe_spawn_nightly_catchup

        maybe_spawn_nightly_catchup()
    except Exception as error:  # noqa: BLE001 - report without breaking session startup
        project_checkpoint.log_hook_error("session-start nightly catch-up", describe_error(error))


def recovered_project_handoff(slug: str | None, project_dir: Path | None) -> Sequence[Any]:
    if not slug or project_dir is None:
        return ()
    try:
        store = ProjectStore(runtime_roots.vault_root(), runtime_roots.state_root())
        return recover_project_handoff(
            store,
            slug,
            project_root=project_dir,
            render_context=False,
        ).items
    except Exception as exc:  # noqa: BLE001
        project_checkpoint.log_checkpoint_error(exc)
        return ()


def _global_context_items(value: Sequence[Any] | str, item_type: type) -> list[Any]:
    if not isinstance(value, str):
        return list(value)
    text = value.strip()
    if not text:
        return []
    return [
        item_type(
            item_id="session-start:unstructured",
            text=text,
            source="session-start",
            priority=5,
            relevance=0.5,
            confidence="medium",
            freshness="fresh",
            token_cost=len(text.encode("utf-8")),
            mandatory=False,
            representation="l1",
            parent_id="session-start",
            priority_class="evidence",
        )
    ]


def _append_handoff_item(items: list[Any], handoff: Sequence[Any] | str, item_type: type) -> None:
    if not isinstance(handoff, str):
        items.extend(handoff)
        return
    text = handoff.strip()
    if not text:
        return
    items.append(
        item_type(
            item_id="session-start:project-handoff",
            text=text,
            source="project-handoff",
            priority=3,
            relevance=1.0,
            confidence="high",
            freshness="fresh",
            token_cost=len(text.encode("utf-8")),
            mandatory=True,
            representation="l1",
            parent_id="project-handoff",
            priority_class="handoff",
        )
    )


def _compile_context(items: Sequence[Any], *, trailing_newline: bool = False) -> str:
    """Pack SessionStart items under the shared budget and the char ceiling."""
    from context_budget import DEFAULT_CONTEXT_BUDGET, BudgetExceededError
    from context_compiler import compile_context_items
    from session_start_context import fit_to_char_ceiling

    tail = "\n" if trailing_newline else ""

    def _render(kept: Sequence[Any]) -> str:
        try:
            return compile_context_items(
                kept,
                budget=DEFAULT_CONTEXT_BUDGET,
                emergency_byte_cap=DEFAULT_CONTEXT_BUDGET.available_input_tokens,
            ).text + tail
        except BudgetExceededError as error:
            return error.failure.render() + tail

    return fit_to_char_ceiling(list(items), _render)


def _append_code_graph_item(items: list[Any], reminder: str | None, item_type: type) -> None:
    """One line naming the code tools when this checkout is indexed (#24, C1).

    Every host reaches it: Claude Code and Codex through their SessionStart
    hook, OpenCode through the session context its plugin pushes."""
    if not reminder:
        return
    items.append(
        item_type(
            item_id="session-start:code-graph",
            text=reminder,
            source="code-graph",
            priority=4,
            relevance=0.8,
            confidence="high",
            freshness="fresh",
            token_cost=len(reminder.encode("utf-8")),
            mandatory=False,
            representation="l1",
            parent_id="code-graph",
            priority_class="evidence",
        )
    )


def _code_graph_reminder(project_dir: Path | None) -> str | None:
    """Silence, never an error: a session must start whatever the index says."""
    try:
        from graph_hint import reminder_for

        return reminder_for(project_dir)
    except Exception:  # noqa: BLE001
        return None


def append_context(
    context_items: Sequence[Any] | str,
    handoff: Sequence[Any] | str,
    *,
    trailing_newline: bool = False,
    code_graph: str | None = None,
) -> str:
    from context_budget import ContextItem

    items = _global_context_items(context_items, ContextItem)
    _append_handoff_item(items, handoff, ContextItem)
    _append_code_graph_item(items, code_graph, ContextItem)
    if not items:
        return ""
    return _compile_context(items, trailing_newline=trailing_newline)


def _ingest_result(slug: str | None, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "slug": slug,
        "heartbeat_recorded": False,
        "daily_log_written": False,
        "flush_spawned": False,
        "transcript_path": payload.get("transcript_path"),
        "returncode": 0,
    }


def write_session_start_debug(context: object) -> None:
    """The payload the hook returned, where `logs/session-start-last.txt` promises it.

    See `docs/research/2026-09-14-less-noise-at-session-start.md`.
    """
    from session_start_context import latest_daily, write_debug

    try:
        daily = latest_daily()
        write_debug(str(context or ""), getattr(daily, "name", "(none)"))
    except Exception:  # noqa: BLE001 - a debug copy must never cost the session its context
        return


def _ingest_session_start(
    envelope: EventEnvelope,
    payload: dict[str, Any],
    slug: str | None,
    project_dir: Path | None,
    result: dict[str, Any],
    force_stub: bool,
    trigger: str | None,
) -> None:
    result["heartbeat_recorded"] = record_activity(envelope, slug, project_dir)
    maintenance_pid = memory_state.spawn_detached(
        [sys.executable, str(SCRIPTS_DIR / "integration_adapter.py"), "--maintenance"]
    )
    result["maintenance_scheduled"] = maintenance_pid is not None
    result["context"] = append_context(
        build_session_start_context(slug),
        recovered_project_handoff(slug, project_dir),
        trailing_newline=True,
        code_graph=_code_graph_reminder(project_dir),
    )
    write_session_start_debug(result["context"])

