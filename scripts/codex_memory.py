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
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT  # noqa: E402

SCRIPTS_DIR = ROOT / "scripts"
PROJECTS_DIR = ROOT / "knowledge" / "projects"
SCRIPT_TIMEOUT_SECONDS = 10

sys.path.insert(0, str(SCRIPTS_DIR))

from integration_adapter import ingest_event, normalize_event  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402
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
        help="Reason label stored in knowledge/daily",
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
    try:
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
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess([sys.executable, str(script)], 124, "", "")


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
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            data = {}
        payload = data
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
    result = _run_script("lookup_mode.py", ROOT)
    if result.returncode == 124:
        print("codex_memory: lookup timed out", file=sys.stderr)
        return result.returncode
    if result.returncode != 0:
        print("codex_memory: lookup failed", file=sys.stderr)
        return result.returncode
    sys.stdout.write(result.stdout)
    return result.returncode


def command_daily_log(args: argparse.Namespace) -> int:
    """Normalize a Codex lifecycle event and present shared ingest results."""
    try:
        envelope = normalize_event(
            "codex",
            "session_end",
            {
                "session_id": getattr(args, "session_id", None),
                "cwd": getattr(args, "cwd", None),
                "reason": getattr(args, "reason", None),
                "transcript": getattr(args, "transcript", None),
            },
        )
    except Exception:  # noqa: BLE001
        print("codex_memory: capture skipped", file=sys.stderr)
        return 0

    session_id = envelope.session or ""
    reason = envelope.payload["reason"] or ""
    force_stub = bool(getattr(args, "force_stub", False))
    json_output = bool(getattr(args, "json", False))
    raw_trigger = getattr(args, "trigger", "")
    trigger = redact_secrets(raw_trigger) if isinstance(raw_trigger, str) else ""
    trigger = trigger or reason or "codex"
    try:
        result = ingest_event(envelope, force_stub=force_stub, trigger=trigger)
    except Exception:  # noqa: BLE001
        print("codex_memory: capture failed", file=sys.stderr)
        return 0

    project_dir = _project_dir(envelope.worktree or os.getcwd())
    slug = result.get("slug")
    transcript_path = result.get("transcript_path") or ""
    returncode = int(result.get("returncode", 0))

    if json_output:
        state_path = PROJECTS_DIR / str(slug) / "state.md" if slug else None
        print(
            json.dumps(
                {
                    "cwd": str(project_dir),
                    "slug": slug,
                    "state_path": str(state_path) if state_path else None,
                    "daily_log_written": result["daily_log_written"],
                    "heartbeat_recorded": result["heartbeat_recorded"],
                    "flush_spawned": result["flush_spawned"],
                    "reason": reason,
                    "session_id": session_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return returncode

    if returncode == 0:
        if result["heartbeat_recorded"]:
            print(f"Heartbeat recorded for slug: {slug} (no daily-log stub)")
            print(f"Reason: {reason}")
            return 0
        print(f"Daily log tagged for slug: {slug}")
        print(f"Reason: {reason}")
        print(f"Session id: {session_id}")
        if result["flush_spawned"]:
            print(f"Flush spawned for transcript: {transcript_path}")
    else:
        print("codex_memory: capture failed", file=sys.stderr)
    return returncode


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
