r"""Codex-friendly wrapper around the LLM-wiki multi-project memory hooks.

This script reuses the existing Claude-oriented hook implementations so
Codex can pull the same per-project state and write the same daily-log
breadcrumbs without forking slug/state logic.

Usage examples:
    python scripts/codex_memory.py project-state
    python scripts/codex_memory.py project-state --cwd <your-projects-dir>/your-app --json
    python scripts/codex_memory.py state-path
    python scripts/codex_memory.py lookup-tier
    python scripts/codex_memory.py daily-log --reason codex-turn-end
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

ROOT = Path(os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))).resolve()
STATE_ROOT = Path(
    os.environ.get("LLM_WIKI_STATE_ROOT", str(Path(__file__).resolve().parent.parent.parent / "LLM-wiki-state"))
).resolve()
SCRIPTS_DIR = ROOT / "scripts"
PROJECTS_DIR = ROOT / "wiki" / "projects"

sys.path.insert(0, str(SCRIPTS_DIR))

from session_start_project_state import _compute_slug  # type: ignore  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=os.getcwd(), help="Project directory")
    common.add_argument("--json", action="store_true", help="Machine-readable output")

    sub.add_parser("project-state", parents=[common])
    sub.add_parser("state-path", parents=[common])
    sub.add_parser("lookup-tier", parents=[common])

    daily = sub.add_parser("daily-log", parents=[common])
    daily.add_argument(
        "--reason",
        default="codex-turn-end",
        help="Reason label stored in memory/daily",
    )
    daily.add_argument(
        "--session-id",
        default="",
        help="Optional session id override",
    )
    daily.add_argument(
        "--transcript",
        default="",
        help=(
            "Transcript file path (JSONL). If empty (default), no "
            "daily-log stub is written — only a heartbeat in state.json "
            "(Phase 0.5 anti-pollution behavior)."
        ),
    )
    daily.add_argument(
        "--trigger",
        default="codex",
        help="Trigger label passed through to flush_memory (default: codex).",
    )
    daily.add_argument(
        "--force-stub",
        action="store_true",
        help=(
            "Force writing a daily-log stub block even without a "
            "transcript. Rare — use only when you explicitly want a "
            "breadcrumb at the cost of daily-log noise."
        ),
    )
    return parser.parse_args()


def _project_dir(raw: str) -> Path:
    return Path(raw).resolve()


def _hook_env(project_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(ROOT)
    env["LLM_WIKI_STATE_ROOT"] = str(STATE_ROOT)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return env


def _run_script(name: str, project_dir: Path, stdin_text: str = "") -> subprocess.CompletedProcess[str]:
    script = SCRIPTS_DIR / name
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        env=_hook_env(project_dir),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _state_path(project_dir: Path) -> tuple[str, Path]:
    slug = _compute_slug(project_dir, PROJECTS_DIR)
    return slug, PROJECTS_DIR / slug / "state.md"


def command_project_state(args: argparse.Namespace) -> int:
    project_dir = _project_dir(args.cwd)
    result = _run_script("session_start_project_state.py", project_dir)
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return result.returncode

    payload = {}
    if result.stdout.strip():
        payload = json.loads(result.stdout)
    ctx = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    slug, state_path = _state_path(project_dir)
    out = {
        "cwd": str(project_dir),
        "slug": slug,
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
        "additional_context": ctx,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"Slug: {slug}")
    print(f"State path: {state_path}")
    if ctx:
        print()
        print(ctx)
    else:
        print()
        print("(no project-state context emitted)")
    return 0


def command_state_path(args: argparse.Namespace) -> int:
    project_dir = _project_dir(args.cwd)
    slug, state_path = _state_path(project_dir)
    out = {
        "cwd": str(project_dir),
        "slug": slug,
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"Slug: {slug}")
        print(f"State path: {state_path}")
        print(f"Exists: {state_path.exists()}")
    return 0


def command_lookup_tier(args: argparse.Namespace) -> int:
    del args
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "lookup_mode.py")],
        cwd=str(ROOT),
        env=_hook_env(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def command_daily_log(args: argparse.Namespace) -> int:
    """Tag the daily log for a Codex session event.

    Phase 0.5 fix: previously every `codex-turn-end` wrote a metadata-
    only block (slug + root) into the daily log even when no transcript
    was available. This produced ~15-30 stub blocks per day per project,
    drowning real content and triggering spurious compile passes.

    New behavior:
    - If `--transcript` is provided AND non-empty: forward to
      `session_end_project_tag.py` AND `flush_memory.py` (full path,
      same as Claude Code SessionEnd).
    - If `--transcript` is empty (Codex CLI doesn't expose transcripts
      the way Claude Code does): skip the daily-log write entirely and
      record only an activity heartbeat in state.json. This avoids
      stub pollution while preserving "this project was touched today"
      observability for the SessionStart context injector.

    The old behavior is available via `--force-stub` for callers that
    explicitly want a breadcrumb even without content (rare).
    """
    project_dir = _project_dir(args.cwd)
    session_id = args.session_id or f"codex-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Build the payload for session_end_project_tag.py
    payload = {
        "session_id": session_id,
        "reason": args.reason,
        "transcript_path": getattr(args, "transcript", "") or "",
    }

    transcript_path = payload["transcript_path"]
    force_stub = bool(getattr(args, "force_stub", False))

    if not transcript_path and not force_stub:
        # No transcript available — record heartbeat in state.json only,
        # do NOT pollute memory/daily/ with stub blocks.
        slug, _ = _state_path(project_dir)
        _record_heartbeat(slug, project_dir, args.reason, session_id)
        if args.json:
            print(
                json.dumps(
                    {
                        "cwd": str(project_dir),
                        "slug": slug,
                        "daily_log_written": False,
                        "heartbeat_recorded": True,
                        "reason": args.reason,
                        "session_id": session_id,
                        "note": "no transcript — daily log not polluted (Phase 0.5)",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"Heartbeat recorded for slug: {slug} (no daily-log stub)")
            print(f"Reason: {args.reason}")
        return 0

    # Transcript available — full path through session_end_project_tag.py
    # AND flush_memory.py to extract durable content.
    result = _run_script(
        "session_end_project_tag.py",
        project_dir,
        stdin_text=json.dumps(payload, ensure_ascii=False),
    )

    # If we have a real transcript, also kick off flush_memory to
    # extract durable content (decisions/lessons/gotchas). This is
    # the same path Claude Code takes on SessionEnd.
    if transcript_path and Path(transcript_path).exists():
        _spawn_flush_memory(session_id, args.reason, transcript_path, args.trigger)

    if args.json:
        slug, state_path = _state_path(project_dir)
        print(
            json.dumps(
                {
                    "cwd": str(project_dir),
                    "slug": slug,
                    "state_path": str(state_path),
                    "daily_log_written": result.returncode == 0,
                    "flush_spawned": bool(transcript_path),
                    "reason": args.reason,
                    "session_id": session_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return result.returncode

    if result.returncode == 0:
        slug, _ = _state_path(project_dir)
        print(f"Daily log tagged for slug: {slug}")
        print(f"Reason: {args.reason}")
        print(f"Session id: {session_id}")
        if transcript_path:
            print(f"Flush spawned for transcript: {transcript_path}")
    else:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
    return result.returncode


def _record_heartbeat(
    slug: str,
    project_dir: Path,
    reason: str,
    session_id: str,
) -> None:
    """Record a no-content heartbeat in state.json.

    Used when Codex turn-end fires without a transcript. Replaces the
    old behavior of writing empty stub blocks into memory/daily/. The
    heartbeat is visible in state.json under `codex_heartbeats` so the
    SessionStart context injector can still surface "this project was
    active N hours ago" — without polluting the daily log corpus.
    """
    try:
        from memory_state import update_state  # type: ignore
    except ImportError:
        return  # best-effort, never crash the hook

    now_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(state: dict) -> None:
        state.setdefault("codex_heartbeats", {})
        state["codex_heartbeats"][slug] = {
            "at": now_iso,
            "reason": reason,
            "session_id": session_id,
            "project_root": str(project_dir),
        }
        # Keep only the most recent 50 heartbeats across all projects.
        if len(state["codex_heartbeats"]) > 50:
            # Sort by timestamp and keep newest 50.
            items = sorted(
                state["codex_heartbeats"].items(),
                key=lambda kv: kv[1].get("at", ""),
                reverse=True,
            )[:50]
            state["codex_heartbeats"] = dict(items)

    try:
        update_state(_mutate)
    except Exception:  # noqa: BLE001
        pass  # never crash the hook on state-write failure


def _spawn_flush_memory(
    session_id: str,
    event: str,
    transcript: str,
    trigger: str,
) -> None:
    """Detach-spawn flush_memory.py for a Codex session.

    Best-effort: if spawn fails, the heartbeat is still recorded and
    the daily log will simply not have a content block for this event.
    """
    try:
        from memory_state import spawn_detached  # type: ignore
    except ImportError:
        return

    try:
        spawn_detached(
            [
                sys.executable,
                str(SCRIPTS_DIR / "flush_memory.py"),
                "--event",
                event,
                "--session-id",
                session_id,
                "--transcript",
                transcript,
                "--trigger",
                trigger,
            ],
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    args = parse_args()
    if args.command == "project-state":
        return command_project_state(args)
    if args.command == "state-path":
        return command_state_path(args)
    if args.command == "lookup-tier":
        return command_lookup_tier(args)
    if args.command == "daily-log":
        return command_daily_log(args)
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
