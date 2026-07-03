"""User-level SessionEnd hook — tag the day's daily log with the project slug.

Fires at session end from any cwd. Appends a minimal marker entry to
`memory/daily/YYYY-MM-DD.md` identifying the project slug and session
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

# Match the Source line that session_start_project_state.py writes into
# newly-created state.md pages. Used to find the slug that SessionStart
# already assigned to this project, so SessionEnd tags with the same one.
STATE_SOURCE_LINE_RE = re.compile(
    r"^- Project root:\s*`([^`]+)`", re.MULTILINE
)


def _resolve_state_root() -> Path | None:
    """Return $LLM_WIKI_STATE_ROOT or a sibling-of-vault fallback.

    Mirrors `memory_state.py` convention: if the env var is unset, default
    to `$LLM_WIKI_ROOT/../LLM-wiki-state`.
    """
    raw = os.environ.get("LLM_WIKI_STATE_ROOT")
    if raw:
        return Path(raw)
    vault = os.environ.get("LLM_WIKI_ROOT")
    if vault:
        return Path(vault).resolve().parent / "LLM-wiki-state"
    return None


def _safe_write_error(err: str) -> None:
    """Best-effort error log."""
    try:
        state_root = _resolve_state_root()
        if state_root is None:
            return
        log_path = state_root / "hook-errors.log"
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
        return json.loads(raw)
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


def _append_entry(daily_path: Path, entry: str) -> None:
    """Append entry to daily log, creating the file if needed.

    Not atomic-write because daily logs are append-only and a truncated
    append recovers on the next write. A malformed entry at most loses
    one marker — harmless.
    """
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not daily_path.exists()
    with daily_path.open("a", encoding="utf-8") as f:
        if header_needed:
            today = datetime.now().strftime("%Y-%m-%d")
            f.write(f"# Daily Session Memory — {today}\n\n")
        f.write(entry)


def main() -> int:
    try:
        vault_root = os.environ.get("LLM_WIKI_ROOT")
        if not vault_root:
            return 0
        vault = Path(vault_root).resolve()
        daily_dir = vault / "memory" / "daily"
        if not daily_dir.parent.is_dir():
            _safe_write_error(f"memory/ dir missing under {vault}")
            return 0

        project_dir = _resolve_project_dir()

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

        projects_dir = vault / "wiki" / "projects"
        slug = _compute_slug(project_dir, projects_dir)
        payload = _read_payload()
        now = datetime.now()
        session_id = str(payload.get("session_id", "unknown"))
        reason = str(payload.get("reason", "other"))
        transcript = str(payload.get("transcript_path", ""))

        today_file = daily_dir / f"{now.strftime('%Y-%m-%d')}.md"
        entry = (
            f"## [{now.strftime('%H:%M:%S')}] session-end | {session_id}\n"
            f"- Trigger: `{reason}`\n"
            f"- Project slug: `{slug}`\n"
            f"- Project root: `{project_dir}`\n"
            + (f"- Transcript: `{transcript}`\n" if transcript else "")
            + "\n"
        )
        _append_entry(today_file, entry)
        return 0

    except Exception:  # noqa: BLE001
        _safe_write_error("unhandled:\n" + traceback.format_exc())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
