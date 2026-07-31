"""User-level SessionEnd hook — tag the day's daily log with the project slug.

Fires at session end from any cwd. Appends a minimal marker entry to
`knowledge/daily/YYYY-MM-DD.md` identifying the project slug and session
metadata. This lets cross-project sessions leave breadcrumbs in the
shared daily log.

Companion to the project-level `session_end_capture.py` hook, which spawns
`flush_memory.py` (heavy, LLM-driven, transcript-based) when cwd = vault.
To avoid duplicate work and noisy logs, this user-level hook **skips**
when the current directory is inside the vault — the project-level hook
already handles that case with richer content.

Contract (hard requirements, mirrors session_start_project_state.py):
    * Must exit 0 on ANY error. Breaking a session-end is worse than a
      missing log entry.
    * Must no-op if LLM_WIKI_ROOT is unset.
    * Reads the SessionEnd payload (session_id, transcript_path, reason)
      from stdin when available — forwards metadata into the daily entry.

Daily entry format (one append per session end):

    ## [HH:MM:SS] session-end | <session_id>
    - Trigger: `<reason>`
    - Project slug: `<slug>`
    - Project root: `<absolute path>`
    - Transcript: `<transcript path>`
    <!-- llm-wiki-record-complete -->

This format mirrors the existing project-level entries so downstream
tooling (flush_memory, compile_memory, session_start_context preview)
keeps working without changes.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from daily_log_append import locked_append  # noqa: E402
from memory_state import (  # noqa: E402
    MAX_HOOK_STDIN_BYTES,
    read_json_object_bounded,
)
from secret_redact import redact_secrets  # noqa: E402
from session_start_context import parse_daily_records  # noqa: E402
from session_start_project_state import (  # noqa: E402
    _path_comparison_key,
    confirm_project_identity,
    resolve_project_root,
)

DAILY_RECORD_COMPLETION_MARKER = "<!-- llm-wiki-record-complete -->"
MAX_PROVENANCE_CHARS = 500


def _resolve_state_root() -> Path | None:
    """Return $LLM_WIKI_STATE_ROOT or the vault root as fallback.

    Mirrors `memory_state.py` convention: if the env var is unset, default
    to the vault itself (runtime dirs cache/logs/run live inside the vault).
    """
    raw = os.environ.get("LLM_WIKI_STATE_ROOT")
    if raw:
        return Path(raw)
    vault = os.environ.get("LLM_WIKI_ROOT")
    if vault:
        return Path(vault).resolve()
    return None


def _safe_write_error(err: str) -> None:
    """Best-effort error log."""
    try:
        state_root = _resolve_state_root()
        if state_root is None:
            return
        log_path = state_root / "logs" / "hook-errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] session_end_project_tag: {err}\n")
    except Exception:  # noqa: BLE001
        pass


def _lookup_existing_slug(project_dir: Path, projects_dir: Path) -> str | None:
    """Return safe runtime identity only after ownership is confirmed."""
    try:
        confirmed = confirm_project_identity(project_dir, projects_dir)
    except Exception:  # noqa: BLE001 - SessionEnd must fail closed
        return None
    return confirmed[0] if confirmed is not None else None


def _compute_slug(project_dir: Path, projects_dir: Path) -> str | None:
    """Return the confirmed alias, never a folder-derived candidate."""
    return _lookup_existing_slug(project_dir, projects_dir)


def _resolve_project_dir(payload: dict) -> Path | None:
    return resolve_project_root(
        payload,
        env=os.environ,
        fallback_cwd=os.getcwd(),
    ).root


def _read_payload() -> dict | None:
    """Read the SessionEnd JSON payload, returning ``None`` on rejection."""
    return read_json_object_bounded(
        sys.stdin,
        max_bytes=MAX_HOOK_STDIN_BYTES,
    )


def _is_inside_vault(project_dir: Path, vault: Path) -> bool:
    """True if project_dir == vault or is a subdirectory of vault."""
    try:
        project_dir.relative_to(vault)
        return True
    except ValueError:
        return False


def _is_user_home(project_dir: Path) -> bool:
    """True if project_dir is exactly the user's $HOME.

    Same rationale as the HOME guard in session_start_project_state.py:
    $HOME is not a project, and our `.claude/` project marker would
    otherwise match `~/.claude/` user-level config. Prevents `user` slug
    entries in the daily log when Claude Code is launched from $HOME.
    """
    try:
        return project_dir.resolve() == Path.home().resolve()
    except (OSError, RuntimeError):
        return False


def _append_entry(daily_path: Path, entry: str) -> None:
    """Append entry to daily log via canonical locked writer."""
    locked_append(daily_path, entry)


def _sanitize_provenance(value: object, default: str) -> str:
    redacted = redact_secrets(str(value or default))
    one_line = " ".join(redacted.split())
    safe = one_line.replace("`", "'").replace("|", "/")
    return safe[:MAX_PROVENANCE_CHARS] or default


def _entry_matches_project(entry: str, slug: str, project_dir: Path) -> bool:
    try:
        records = parse_daily_records(entry)
        if len(records) != 1:
            return False
        record = records[0]
        return (
            record.kind == "heading"
            and record.meaningful is False
            and isinstance(record.slug, str)
            and record.slug.casefold() == slug.casefold()
            and isinstance(record.project_root, str)
            and _path_comparison_key(Path(record.project_root).resolve())
            == _path_comparison_key(project_dir.resolve())
        )
    except Exception:  # noqa: BLE001 - unverified records must never persist
        return False


def main() -> int:
    try:
        payload = _read_payload()
        if payload is None:
            return 0

        vault_root = os.environ.get("LLM_WIKI_ROOT")
        if not vault_root:
            return 0
        vault = Path(vault_root).resolve()
        daily_dir = vault / "knowledge" / "daily"
        if not daily_dir.parent.is_dir():
            _safe_write_error(f"knowledge/ dir missing under {vault}")
            return 0

        project_dir = _resolve_project_dir(payload)
        if project_dir is None:
            return 0

        # Skip if inside the vault — the project-level SessionEnd hook
        # (`session_end_capture.py`) handles that case with richer content
        # via flush_memory.py.
        if _is_inside_vault(project_dir, vault):
            return 0

        # Skip if cwd is $HOME — matches the SessionStart HOME guard.
        # Prevents `user` slug noise in daily log when Claude Code is
        # launched from the home directory.
        if _is_user_home(project_dir):
            return 0

        projects_dir = vault / "knowledge" / "projects"
        slug = _compute_slug(project_dir, projects_dir)
        if slug is None:
            return 0
        now = datetime.now()
        session_id = _sanitize_provenance(payload.get("session_id"), "unknown")
        reason = _sanitize_provenance(payload.get("reason"), "other")
        transcript = _sanitize_provenance(payload.get("transcript_path"), "")

        today_file = daily_dir / f"{now.strftime('%Y-%m-%d')}.md"
        entry = (
            f"## [{now.strftime('%H:%M:%S')}] session-end | {session_id}\n"
            f"- Trigger: `{reason}`\n"
            f"- Project slug: `{slug}`\n"
            f"- Project root JSON: {json.dumps(str(project_dir), ensure_ascii=False)}\n"
            + (f"- Transcript: `{transcript}`\n" if transcript else "")
            + f"{DAILY_RECORD_COMPLETION_MARKER}\n\n"
        )
        if not _entry_matches_project(entry, slug, project_dir):
            return 0
        _append_entry(today_file, entry)
        return 0

    except Exception:  # noqa: BLE001
        _safe_write_error("unhandled:\n" + traceback.format_exc())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
