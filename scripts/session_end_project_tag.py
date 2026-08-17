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
    - Agent: `<canonical agent>`
    - Project slug: `<slug>`
    - Project root: `<absolute path>`
    - Transcript: `<transcript path>`

This format mirrors the existing project-level entries so downstream
tooling (flush_memory, compile_memory, session_start_context preview)
keeps working without changes.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

SLUG_UNSAFE_RE = re.compile(r"[\s_/\\:*?\"<>|]+")

from daily_log_append import locked_append  # noqa: E402
from event_envelope import canonical_agent  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

# Match the Source line that session_start_project_state.py writes into
# newly-created state.md pages. Used to find the slug that SessionStart
# already assigned to this project, so SessionEnd tags with the same one.
STATE_SOURCE_LINE_RE = re.compile(
    r"^- Project root:\s*`([^`]+)`", re.MULTILINE
)


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


def _base_slug(project_dir: Path) -> str:
    """Sanitized parent folder name, or `root` fallback."""
    base = project_dir.name or "root"
    slug = base.lower()
    slug = SLUG_UNSAFE_RE.sub("-", slug)
    slug = slug.strip("-")
    if not slug or slug in {".", ".."}:
        return "root"
    return slug


def _lookup_existing_slug(project_dir: Path, projects_dir: Path) -> str | None:
    """If SessionStart already created a state.md for this project, return
    the slug it chose (may be collision-resolved to e.g. `backend-your-app`).

    Returns None if no matching state.md is found — caller falls back to
    the base slug. This keeps SessionStart and SessionEnd in sync without
    duplicating the collision-resolution logic.
    """
    if not projects_dir.is_dir():
        return None
    try:
        current_norm = project_dir.resolve().as_posix().lower()
    except (OSError, ValueError):
        return None
    for slug_dir in projects_dir.iterdir():
        if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
            continue
        state_md = slug_dir / "state.md"
        if not state_md.is_file():
            continue
        try:
            body = state_md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = STATE_SOURCE_LINE_RE.search(body)
        if not m:
            continue
        try:
            recorded_norm = Path(m.group(1).strip()).resolve().as_posix().lower()
        except (OSError, ValueError):
            continue
        if recorded_norm == current_norm:
            return slug_dir.name
    return None


def _compute_slug(project_dir: Path, projects_dir: Path) -> str:
    """Return the slug for this project — the one SessionStart picked if
    available, else the base slug.

    SessionEnd's slug is just a tag in the shared daily log; we don't
    create files, so collision detection here would just duplicate logic.
    Instead, defer to whatever SessionStart already recorded. Falls back
    to base slug when SessionStart hasn't run (unusual) or when the
    folder has no marker (SessionStart would have no-op'd).
    """
    existing = _lookup_existing_slug(project_dir, projects_dir)
    if existing:
        return existing
    return _base_slug(project_dir)


def _resolve_project_dir() -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(raw).resolve()


def _read_payload() -> dict:
    """Read the SessionEnd JSON payload from stdin. Return {} on any failure."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


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


def _append_entry(
    daily_path: Path, entry: str, operation_id: str | None = None
) -> None:
    """Append entry to daily log via canonical locked writer."""
    locked_append(daily_path, entry, operation_id=operation_id)


def _vault_paths() -> tuple[Path, Path] | None:
    vault_root = os.environ.get("LLM_WIKI_ROOT")
    if not vault_root:
        return None
    vault = Path(vault_root).resolve()
    daily_dir = vault / "knowledge" / "daily"
    if daily_dir.parent.is_dir():
        return vault, daily_dir
    _safe_write_error(f"knowledge/ dir missing under {vault}")
    return None


def _eligible_project(vault: Path) -> Path | None:
    project_dir = _resolve_project_dir()
    if _is_inside_vault(project_dir, vault):
        return None
    if _is_user_home(project_dir):
        return None
    return project_dir


def _transcript_line(transcript: str) -> str:
    if not transcript:
        return ""
    return f"- Transcript: `{transcript}`\n"


def _session_entry(payload: dict, slug: str, project_dir: Path, now: datetime) -> str:
    session_id = str(payload.get("session_id", "unknown"))
    reason = str(payload.get("reason", "other"))
    transcript = str(payload.get("transcript_path", ""))
    agent = canonical_agent(str(payload.get("agent") or "claude"))
    entry = (
        f"## [{now.strftime('%H:%M:%S')}] session-end | {session_id}\n"
        f"- Trigger: `{reason}`\n"
        f"- Agent: `{agent}`\n"
        f"- Project slug: `{slug}`\n"
        f"- Project root: `{project_dir}`\n"
        f"{_transcript_line(transcript)}\n"
    )
    return redact_secrets(entry)


def _session_operation_id(payload: dict) -> str | None:
    source_event_id = payload.get("event_id") or payload.get("source_event_id")
    if not isinstance(source_event_id, str) or not source_event_id:
        return None
    return f"session-end:{source_event_id}"


def _tag_session() -> None:
    paths = _vault_paths()
    if paths is None:
        return
    vault, daily_dir = paths
    project_dir = _eligible_project(vault)
    if project_dir is None:
        return
    payload = _read_payload()
    now = datetime.now()
    slug = _compute_slug(project_dir, vault / "knowledge" / "projects")
    today_file = daily_dir / f"{now.strftime('%Y-%m-%d')}.md"
    _append_entry(
        today_file,
        _session_entry(payload, slug, project_dir, now),
        operation_id=_session_operation_id(payload),
    )


def main() -> int:
    try:
        _tag_session()
    except Exception:  # noqa: BLE001
        _safe_write_error("unhandled:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
