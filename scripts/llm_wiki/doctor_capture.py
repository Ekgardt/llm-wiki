"""Doctor's checks of capture, vault size, hook failures and project checkpoints."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import generation_maintenance  # noqa: E402
from llm_wiki import doctor_generation
from llm_wiki.doctor_generation import CEILING_WARNING_SHARE, deferred_sentence, state_size_hint
from llm_wiki.doctor_schedule import (
    nightly_result,
    scheduled_program_verdict,
    unit_limit_verdict,
    update_verdict,
    weekly_verdict,
    with_findings,
)
from llm_wiki.install_inspection import installed_at
from settings import (
    DEFAULT_SOURCE,
    SettingsError,
    settings_path,
)
from settings import effective as effective_settings


# The totals are cumulative and the verdict is about the last seven days
# (`capture_diagnostics.CAPTURE_RECENT_SECONDS`): a degraded line that gave only the
# total read as a count that keeps the install in warning forever. It says both.
# See docs/research/2026-09-28-a-check-names-its-cause.md.
def _recent_loss_message(lost: int, details: dict) -> str:
    return (
        f"{lost} capture(s) have been lost in total; the last at {details.get('last_at')}, "
        f"within the last seven days.{deferred_sentence(details)}"
    )


def _capture_loss_result(lost: int, live: bool, details: dict) -> dict:
    """The capture verdict, once the diagnostics themselves have been read."""
    suffix = deferred_sentence(details)
    if live:
        return generation_maintenance.result("capture", "degraded", _recent_loss_message(lost, details), details)
    if lost:
        return generation_maintenance.result(
            "capture",
            "ok",
            f"{lost} capture(s) were lost, none recently.{suffix}",
            details,
        )
    return generation_maintenance.result("capture", "ok", f"No lost capture is recorded.{suffix}", details)


def tool_failure_check(state_root: Path, deadline: float) -> dict:
    """Report failed operations, including retained historical compile refusals.

    They share the capture trail and were counted as lost captures (audit B-25,
    docs/research/2026-09-25-a-tool-failure-is-not-a-lost-capture.md).
    """
    from capture_diagnostics import (
        last_operational_failure_at,
        operational_failure_is_live,
        operational_failure_totals,
    )

    state, state_error = doctor_generation.read_state(state_root, deadline)
    totals = operational_failure_totals(state)
    failed = sum(totals.values())
    details = {
        "failed": failed,
        "kinds": totals,
        "trail": "logs/capture-failures.jsonl",
        "last_at": last_operational_failure_at(state),
        "state_error": state_error,
    }
    if state_error:
        return generation_maintenance.result("tools", "degraded", "Operational failure diagnostics could not be read.", details)
    if not operational_failure_is_live(state):
        return generation_maintenance.result("tools", "ok", f"{failed} operation failure(s) recorded, none recently.", details)
    return generation_maintenance.result(
        "tools",
        "degraded",
        f"{failed} operation failure(s) recorded in total; the last at "
        f"{details['last_at']}, within the last seven days. See `logs/capture-failures.jsonl`.",
        details,
    )


def models_check() -> dict:
    """Name the pinned model weights the cache lacks, with the command that fetches them.

    Without them the vault answers by words alone and only the trace says so.
    Presence at the pinned revision is what is checked here; the digest is
    verified by `install_models.py` when it fetches.
    """
    from install_models import hub_library, missing_models, pinned_models

    hub = hub_library()
    if hub is None:
        return generation_maintenance.result(
            "models",
            "ok",
            "Semantic search is not installed; no model weights are expected.",
            {"installed": False, "missing": []},
        )
    missing = [f"{model.repo_id}@{model.revision[:12]}" for model in missing_models(hub)]
    details = {
        "installed": True,
        "missing": missing,
        "expected": [model.repo_id for model in pinned_models()],
        "command": "uv run python scripts/install_models.py",
    }
    if not missing:
        return generation_maintenance.result("models", "ok", "Pinned model weights are in the local cache.", details)
    return generation_maintenance.result(
        "models",
        "degraded",
        "Model weights missing: " + ", ".join(missing)
        + "; search answers by words alone until `uv run python scripts/install_models.py` runs.",
        details,
    )


# Which Markdown each vault-size ceiling counts, by the directories under
# `knowledge/` its pipeline reads (the research note's table).
_CEILING_SCOPES = {
    "index.max_pages": ("notes",),
    "index.max_total_bytes": ("notes",),
    "search.max_pages": ("notes",),
    "impact.max_note_files": ("notes",),
    "impact.max_total_note_bytes": ("notes",),
    "claims.max_pages": ("claims",),
    "claims.max_total_bytes": ("claims",),
    "compile.max_sources": ("compile",),
    "compile.max_total_source_bytes": ("compile",),
}


def _markdown_size(directory: Path) -> tuple[int, int]:
    """How many Markdown files a knowledge directory holds, and their bytes."""
    files = [path for path in directory.rglob("*.md") if path.is_file()] if directory.is_dir() else []
    return len(files), sum(path.stat().st_size for path in files)


def _vault_sizes(root: Path) -> dict[str, tuple[int, int]]:
    sizes = {"notes": _markdown_size(root / "knowledge/notes")}
    sizes["compile"] = _compile_sources_size(root)
    sizes["claims"] = _claim_sources_size(root)
    return sizes


def _compile_sources_size(root: Path) -> tuple[int, int]:
    from llm_wiki.compile_pipeline_sources import (
        compile_scope_paths,
    )

    files = compile_scope_paths(root)
    return len(files), sum(path.stat().st_size for path in files)


def _claim_sources_size(root: Path) -> tuple[int, int]:
    from claim_tree_manifest import claim_scope_paths

    files = claim_scope_paths(root)
    return len(files), sum(path.stat().st_size for path in files)


def _ceiling_use(name: str, ceiling: int, sizes: dict[str, tuple[int, int]]) -> dict:
    """The live count a ceiling bounds: files for a count, bytes for a byte ceiling."""
    measure = 1 if name.endswith("bytes") else 0
    used = sum(sizes[scope][measure] for scope in _CEILING_SCOPES[name])
    return {"used": used, "ceiling": ceiling, "share": round(used / ceiling, 3)}


def _settings_result(overridden: dict, near: dict) -> dict:
    details = {
        "overridden": overridden, "near_ceiling": near,
        "corpus_capacity": "not_measured; see full generation check",
    }
    if near:
        names = ", ".join(sorted(near))
        return generation_maintenance.result("settings", "degraded", f"The vault is past 80% of a size ceiling: {names}; measure resource capacity before changing it in llm-wiki.toml.", details)
    message = f"{len(overridden)} setting(s) differ from their defaults." if overridden else "Every setting has its default."
    return generation_maintenance.result("settings", "ok", message, details)


def settings_check(root: Path) -> dict:
    """Operator overrides, where each came from, and how near the vault is to each size ceiling."""
    try:
        values = effective_settings(root)
    except (SettingsError, OSError) as error:
        return generation_maintenance.result("settings", "error", f"Settings are invalid: {error}", {"path": str(settings_path(root))})
    return _settings_result(_overridden_settings(values), _near_ceilings(root, values))


def _overridden_settings(values: dict) -> dict:
    changed = {name: item for name, item in values.items() if item.source != DEFAULT_SOURCE}
    return {name: {"value": item.value, "source": item.source} for name, item in changed.items()}


def _near_ceilings(root: Path, values: dict) -> dict:
    sizes = _vault_sizes(root)
    uses = {name: _ceiling_use(name, values[name].value, sizes)
            for name in _CEILING_SCOPES if values[name].value > 0}
    return {name: use for name, use in uses.items() if use["share"] >= CEILING_WARNING_SHARE}


def capture_check(root: Path, state_root: Path, deadline: float, *, adoption=None) -> dict:
    """Report captures the hooks lost, so a silent loss is visible in health."""
    from capture_diagnostics import (
        capture_deferred_totals,
        capture_failure_is_live,
        capture_failure_totals,
        last_capture_failure_at,
    )

    state, state_error = doctor_generation.read_state(state_root, deadline)
    totals = capture_failure_totals(state)
    lost = sum(totals.values())
    live = capture_failure_is_live(state)
    details: dict[str, Any] = {
        "lost": lost,
        "kinds": totals,
        "deferred": sum(capture_deferred_totals(state).values()),
        "trail": "logs/capture-failures.jsonl",
        "state_error": state_error,
        "last_at": last_capture_failure_at(state),
        "live": live,
    }
    if state_error:
        return generation_maintenance.result(
            "capture",
            "degraded",
            "Capture diagnostics could not be read within safety bounds."
            + state_size_hint(state_root),
            details,
        )
    adoption, details["adoption_error"] = adoption or _adoption_state(root, state_root)
    details["adoption_state"] = adoption
    return _adoption_result(adoption, details) or _capture_loss_result(lost, live, details)


def _adoption_result(adoption: str, details: dict[str, Any]) -> dict | None:
    """The capture verdict the adoption state decides, or None when it decides nothing."""
    if adoption == "unreadable":
        return generation_maintenance.result("capture", "degraded", _adoption_unreadable_message(details), details)
    if adoption in {"adopted", "unknown"}:
        return None
    return generation_maintenance.result("capture", "degraded", _capture_disabled_message(adoption), details)


def _adoption_state(root: Path, state_root: Path) -> tuple[str, str | None]:
    """The Reliability V3 adoption state and the cause of a failed read, from run/."""
    from installed_memory_repair import describe_failure, inspect_installed_vault

    try:
        report = inspect_installed_vault(root=root, state_root=state_root)
    except Exception as exc:  # noqa: BLE001 - a health check never raises; it names the cause
        return "unknown", describe_failure(exc)
    details = report.get("details", {})
    return str(details.get("adoption_state") or "unknown"), details.get("error")


def _adoption_unreadable_message(details: dict[str, Any]) -> str:
    """A busy or locked record is not a verdict on adoption: never say capture is disabled.

    See docs/research/2026-09-28-a-check-names-its-cause.md.
    """
    return (
        "The Reliability V3 records could not be read just now "
        f"({details.get('adoption_error')}); this is not a finding that capture is "
        "disabled. Run doctor again."
    )


def _capture_disabled_message(adoption: str) -> str:
    """Plain words for what issue #17 found buried in a failure log: no capture until adoption."""
    return (
        f"Session capture is disabled: Reliability V3 state is '{adoption}'. Run "
        "uv run --locked --no-sync python scripts/repair_installed_memory.py "
        "--apply --adopt-ownership-v3 --confirm-all-agents-stopped"
    )


# The hook error trail nothing ever read. Measured 2026-08-29: 5 682 failures
# between 2026-08-25T09:32 and 2026-08-29T21:04 — project checkpointing failing
# every ten seconds for five days — and health reported nothing at all, because
# no check opened this file. In the meantime the queue those checkpoints feed
# grew to 4 643 undrained events and `run/state.json` to 10 MB.
#
# A trail that only a person grepping can see is not a health signal. What made
# the outage long was not the defect; it was that nothing said so.
HOOK_ERROR_TAIL_BYTES = 64 * 1024


# Newer than this and the failure is happening now, not once upon a time. An
# hour, because these hooks fire on session lifecycle events: quieter than a
# heartbeat, far busier than a nightly pass.
HOOK_ERROR_LIVE_SECONDS = 3600.0


_HOOK_ERROR_LINE = re.compile(r"^\[(?P<at>[^\]]+)\]\s+(?P<kind>[^:]+):(?P<rest>.*)$")


# A lost race, by what the line says rather than by what wrote it. Reading the
# message means the six hundred lines already in the trail are classified too,
# instead of the fix only taking effect once the log rotates.
# A kind whose name says the writer lost a race. The event is carried by the
# next session end, so it is retried work and not lost work, and counting it as
# a failure made this check permanently red on a machine that runs several
# agents. Measured on this vault 2026-09-07: `another-project` logged four of these in
# four minutes while its committed sequence advanced from 836 to 838.
CONTENTION_KIND_MARKER = "contention"
CONTENTION_MESSAGES = (
    "owner_busy",
    "ProjectPendingPriorError",
    "operation_id is already bound to a different request",
    "writer is busy",
    "database is locked",
    # The state lock is held by another writer; the event is queued in
    # `project_checkpoint_pending` and the next drain carries it. Measured
    # 2026-09-07: the only two lines in the recent trail that were not already
    # contention were this, three seconds apart, while checkpoints kept
    # committing.
    "Could not acquire state lock",
)


def _hook_error_generations(path: Path, state_root: Path) -> list[Path]:
    """The rotated generation, then the live one: a rotation must not hide a burst.

    See `docs/research/2026-09-26-a-log-many-writers-append-to-is-rotated-not-trimmed.md`.
    """
    from maintenance_helpers import rotated_log_previous

    candidates = (rotated_log_previous(path), path)
    return [part for part in candidates if generation_maintenance.safe_kind(part, state_root)[0] != "missing"]


def _hook_error_trail(generations: list[Path]) -> list[str]:
    lines: list[str] = []
    for part in generations:
        lines.extend(hook_error_lines(part))
    return lines


def hook_error_lines(path: Path) -> list[str]:
    """The end of the trail, bounded, so an unbounded log cannot stall health.

    The first line is dropped only when the window actually began mid-file: a
    seek lands mid-line and half a record is not a record, but a file smaller
    than the window starts at byte zero and its first line is whole.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        start = max(0, handle.tell() - HOOK_ERROR_TAIL_BYTES)
        handle.seek(start)
        window = handle.read(HOOK_ERROR_TAIL_BYTES)
    lines = window.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines[1 if start else 0 :] if line.startswith("[")]


def _recorded_kind(kind: str, rest: str) -> str:
    """The kind this line belongs to, contention named apart from failure."""
    if CONTENTION_KIND_MARKER in kind:
        return kind
    if any(marker in rest for marker in CONTENTION_MESSAGES):
        return f"{kind} contention"
    return kind


def hook_error_records(lines: list[str]) -> list[tuple[str, str]]:
    matches = (_HOOK_ERROR_LINE.match(line) for line in lines)
    return [
        (match["at"], _recorded_kind(match["kind"].strip(), match["rest"]))
        for match in matches
        if match
    ]


def hook_error_kinds(records: list[tuple[str, str]]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for _at, kind in records:
        kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def hook_error_is_live(last_at: str, now: datetime) -> bool:
    try:
        seen = datetime.fromisoformat(last_at)
    except ValueError:
        return False
    # Writers stamped local wall-clock time without an offset until 2026-09-26;
    # read as UTC it was hours off (audit 2026-09-26 B-25). A line without an
    # offset is local time, which is what those writers meant.
    if seen.tzinfo is None:
        seen = seen.astimezone()
    return (now - seen).total_seconds() <= HOOK_ERROR_LIVE_SECONDS


def _is_contention_kind(kind: str) -> bool:
    return CONTENTION_KIND_MARKER in kind


def _failure_count(kinds: dict[str, int]) -> int:
    return sum(count for kind, count in kinds.items() if not _is_contention_kind(kind))


def hook_error_result(live: bool, count: int, details: dict) -> dict:
    """Retried contention is reported and does not degrade the check.

    `count` is every line in the trail and stays in the details, because a
    vault drowning in contention is worth seeing. What decides the status is
    how many of them were something a retry will not fix.
    """
    failures = _failure_count(details.get("kinds") or {})
    if live and failures:
        return generation_maintenance.result(
            "hooks",
            "degraded",
            f"{failures} hook failure(s) in the recent trail, still happening.",
            details,
        )
    return generation_maintenance.result("hooks", "ok", _quiet_hook_message(failures, count - failures), details)


def _quiet_hook_message(failures: int, contended: int) -> str:
    if failures:
        return f"{failures} hook failure(s) recorded, none recently."
    if contended:
        return f"No hook failure; {contended} lost race(s) the next session retries."
    return "No hook failure is recorded."


# A project's checkpoints are ordered, so one sequence that cannot finish holds
# every later one. Measured on this vault 2026-08-30: one project's sequence 320
# stood `reserved` behind a `discarded` transaction from 08-28 and refused every
# checkpoint for that project for two days, while 744 of them queued in
# `run/state.json`. Nothing reported it. An hour is long enough that no live
# drain is still working on the sequence.
CHECKPOINT_STUCK_SECONDS = 3600.0


def checkpoint_head_rows(path: Path) -> list[dict[str, Any]]:
    """The lowest unfinished sequence per project, aged from its first attempt.

    From the first, not the latest: the nightly's own re-attempt used to reset the
    age, so the report written at 03:09 said `ok` about a project stuck for a day.
    See `docs/research/2026-09-24-a-vanished-project-is-rebuilt-by-the-night.md`.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT c.project AS project, c.sequence AS sequence, c.state AS state, "
            "COALESCE(("
            "  SELECT MIN(a.created_at) FROM project_checkpoint_attempts a"
            "   WHERE a.project = c.project AND a.sequence = c.sequence"
            "), ("
            '  SELECT t.created_at FROM "transaction" t'
            "   WHERE t.operation_id = c.operation_id"
            ")) AS created_at "
            "FROM project_checkpoints c WHERE c.state != 'committed' "
            "AND c.sequence = ("
            "  SELECT MIN(sequence) FROM project_checkpoints"
            "   WHERE project = c.project AND state != 'committed'"
            ")"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def checkpoint_stuck(row: Mapping[str, Any], now: datetime) -> bool:
    created = generation_maintenance.parse_utc(row.get("created_at"))
    if created is None:
        return True
    return (now - created).total_seconds() > CHECKPOINT_STUCK_SECONDS


def _checkpoint_queue_depths(state_root: Path) -> dict[str, int]:
    pending = _checkpoint_pending_state(Path(state_root) / "run" / "state.json")
    if not isinstance(pending, dict):
        return {}
    return {
        slug: len(queue)
        for slug, queue in pending.items()
        if isinstance(queue, list) and queue
    }


def _checkpoint_pending_state(path: Path) -> object:
    """The pending queues, or nothing — a depth is context, never the finding."""
    try:
        return json.loads(path.read_bytes().decode("utf-8")).get(
            "project_checkpoint_pending"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None


def _checkpoint_result(stuck: list[dict[str, Any]], details: dict) -> dict:
    if not stuck:
        return generation_maintenance.result(
            "checkpoints", "ok", "No project checkpoint is stuck.", details
        )
    projects = ", ".join(sorted(str(row["project"]) for row in stuck))
    return generation_maintenance.result(
        "checkpoints",
        "degraded",
        f"{len(stuck)} project(s) cannot advance their checkpoints: {projects}.",
        details,
    )


# A checkpoint event waits in `run/state.json` until a drain reserves it. The
# nightly reclaim step drains every queue, so an event older than one nightly
# interval plus half of one outlived a drain that should have taken it. Before
# 2026-09-26 the queue was only a depth beside the finding, and a project whose
# events never reached the database was reported as fine (audit C-12). See
# `docs/research/2026-09-26-a-checkpoint-that-never-reached-the-database-is-seen.md`.
CHECKPOINT_QUEUE_STUCK_SECONDS = 36 * 3600.0


def _queued_instant(item: object) -> datetime | None:
    if not isinstance(item, dict):
        return None
    return generation_maintenance.parse_utc(item.get("occurred_at"))


def _queued_instants(queue: object) -> list[datetime]:
    if not isinstance(queue, list):
        return []
    return [instant for instant in map(_queued_instant, queue) if instant is not None]


def _queued_head(slug: str, queue: object) -> dict[str, Any] | None:
    """The oldest event a project still holds in the queue, as a head row."""
    known = _queued_instants(queue)
    if not known:
        return None
    return {"project": slug, "state": "queued", "created_at": min(known).isoformat()}


def _queued_checkpoint_heads(state_root: Path) -> list[dict[str, Any]]:
    pending = _checkpoint_pending_state(Path(state_root) / "run" / "state.json")
    if not isinstance(pending, dict):
        return []
    heads = [_queued_head(str(slug), queue) for slug, queue in pending.items()]
    return [head for head in heads if head is not None]


def _queue_stuck(row: Mapping[str, Any], now: datetime) -> bool:
    created = generation_maintenance.parse_utc(row.get("created_at"))
    return created is not None and (now - created).total_seconds() > CHECKPOINT_QUEUE_STUCK_SECONDS


def _stuck_checkpoint_heads(rows: list[dict[str, Any]], state_root: Path, now: datetime) -> list[dict[str, Any]]:
    """Heads stuck in the database, then projects whose events never reached it."""
    queued = [row for row in _queued_checkpoint_heads(state_root) if _queue_stuck(row, now)]
    return [row for row in rows if checkpoint_stuck(row, now)] + queued


def _checkpoint_database_heads(path: Path) -> list[dict[str, Any]]:
    """The unfinished head rows; none before the database exists."""
    if not path.is_file():
        return []
    return checkpoint_head_rows(path)


def checkpoint_check(state_root: Path, now: datetime) -> dict:
    """Report a project whose checkpoints stopped moving, in the database or before it."""
    path = Path(state_root) / "run" / "markdown-transactions-v3.sqlite3"
    details: dict[str, Any] = {"database": "run/markdown-transactions-v3.sqlite3"}
    try:
        rows = _checkpoint_database_heads(path)
    except sqlite3.Error as error:
        details["error"] = str(error)[:200]
        return generation_maintenance.result(
            "checkpoints", "degraded", "The checkpoint database could not be read.", details
        )
    stuck = _stuck_checkpoint_heads(rows, state_root, now)
    details.update({"unfinished": rows, "queued": _checkpoint_queue_depths(state_root)})
    return _checkpoint_result(stuck, details)


def hook_error_check(state_root: Path, now: datetime) -> dict:
    """Report what the lifecycle hooks failed at, so a silent outage is visible."""
    path = Path(state_root) / "logs" / "hook-errors.log"
    details: dict[str, Any] = {"trail": "logs/hook-errors.log", "window_bytes": 0}
    generations = _hook_error_generations(path, state_root)
    if not generations:
        return generation_maintenance.result("hooks", "ok", "No hook failure trail exists.", details)
    records = hook_error_records(_hook_error_trail(generations))
    if not records:
        return generation_maintenance.result("hooks", "ok", "No hook failure is recorded.", details)
    last_at = max(at for at, _kind in records)
    details.update(
        {
            "recent": len(records),
            "kinds": hook_error_kinds(records),
            "last_at": last_at,
            "window_bytes": HOOK_ERROR_TAIL_BYTES,
        }
    )
    live = hook_error_is_live(last_at, now)
    return hook_error_result(live, len(records), details)


def scheduler_check(
    root: Path, state_root: Path, now: datetime, deadline: float, home: Path | None = None
) -> dict:
    scripts = {
        "scheduled_nightly": (root / "scripts" / "scheduled_nightly.py").is_file(),
        "search_memory": (root / "scripts" / "search_memory.py").is_file(),
    }
    state, state_error = doctor_generation.read_state(state_root, deadline)
    details: dict[str, Any] = {
        "scripts": scripts,
        "last_nightly_date": state.get("last_nightly_date"),
        "last_nightly_status": state.get("last_nightly_status", "unknown"),
        "last_nightly_skip": state.get("last_nightly_skip"),
        "nightly_deferred_compile": state.get("nightly_deferred_compile"),
        "state_error": state_error,
    }
    if state_error in {"budget", "oversized"}:
        return generation_maintenance.result(
            "scheduler",
            "degraded",
            "Maintenance state could not be fully checked within safety bounds."
            + state_size_hint(state_root),
            details,
        )
    if not all(scripts.values()) or state_error:
        return generation_maintenance.result(
            "scheduler", "error", "Maintenance source or local state is invalid.", details
        )
    details["last_weekly_status"] = state.get("last_weekly_status")
    details["last_weekly_at"] = state.get("last_weekly_at")
    details["last_update"] = state.get("last_update")
    verdicts = _scheduler_verdicts(state, now, home or Path.home(), root)
    nightly = nightly_result(state, now, details, installed_at(state_root))
    return with_findings(nightly, verdicts)


def _scheduler_verdicts(state: dict, now: datetime, home: Path, root: Path | None = None) -> tuple[tuple[str, str] | None, ...]:
    return (
        weekly_verdict(state, now),
        update_verdict(state.get("last_update")),
        unit_limit_verdict(home, root),
        scheduled_program_verdict(home),
    )
