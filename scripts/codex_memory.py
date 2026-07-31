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
import threading
import time
from datetime import datetime
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
MAX_CHILD_STDIN_BYTES = 8 * 1024 * 1024
MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024
MAX_CHILD_STDERR_BYTES = 256 * 1024
CHILD_IO_CHUNK_BYTES = 64 * 1024
CHILD_OVERFLOW_RETURN_CODE = 1
CHILD_TIMEOUT_SECONDS = 30.0
CHILD_TIMEOUT_RETURN_CODE = 124
CHILD_THREAD_JOIN_SECONDS = 1.0

sys.path.insert(0, str(SCRIPTS_DIR))

from session_start_project_state import (  # type: ignore  # noqa: E402
    confirm_project_identity,
    resolve_project_root,
)


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


def _project_dir(raw: str) -> Path | None:
    return resolve_project_root(
        {},
        explicit_root=raw,
        env=os.environ,
    ).root


def _requested_project_dir(raw: str) -> Path:
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path.cwd()


def _hook_env(project_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LLM_WIKI_ROOT"] = str(ROOT)
    env["LLM_WIKI_STATE_ROOT"] = str(STATE_ROOT)
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    return env


def _run_script(name: str, project_dir: Path, stdin_text: str = "") -> subprocess.CompletedProcess[str]:
    script = SCRIPTS_DIR / name
    command = [sys.executable, str(script)]
    stdin_bytes = stdin_text.encode("utf-8")
    if len(stdin_bytes) > MAX_CHILD_STDIN_BYTES:
        return subprocess.CompletedProcess(
            command,
            CHILD_OVERFLOW_RETURN_CODE,
            "",
            f"{name} stdin exceeded {MAX_CHILD_STDIN_BYTES} byte limit",
        )

    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=_hook_env(project_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow: list[tuple[str, int]] = []
    overflow_lock = threading.Lock()

    def terminate_for_overflow(stream_name: str, limit: int) -> None:
        with overflow_lock:
            if overflow:
                return
            overflow.append((stream_name, limit))
            try:
                process.kill()
            except OSError:
                pass

    def drain_stream(
        stream,
        destination: bytearray,
        stream_name: str,
        limit: int,
    ) -> None:
        try:
            while True:
                read_size = min(
                    CHILD_IO_CHUNK_BYTES,
                    max(1, limit - len(destination) + 1),
                )
                chunk = stream.read(read_size)
                if not chunk:
                    break
                remaining = limit - len(destination)
                if len(chunk) > remaining:
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                    terminate_for_overflow(stream_name, limit)
                    break
                destination.extend(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def write_stdin() -> None:
        try:
            if stdin_bytes:
                process.stdin.write(stdin_bytes)
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    threads = (
        threading.Thread(
            target=drain_stream,
            args=(process.stdout, stdout, "stdout", MAX_CHILD_STDOUT_BYTES),
            name=f"codex-memory-{name}-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=drain_stream,
            args=(process.stderr, stderr, "stderr", MAX_CHILD_STDERR_BYTES),
            name=f"codex-memory-{name}-stderr",
            daemon=True,
        ),
        threading.Thread(
            target=write_stdin,
            name=f"codex-memory-{name}-stdin",
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + max(0.001, float(CHILD_TIMEOUT_SECONDS))
    timed_out = False
    returncode = CHILD_TIMEOUT_RETURN_CODE
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, CHILD_TIMEOUT_SECONDS)
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True

    if not timed_out:
        for thread in threads:
            if not thread.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            thread.join(timeout=remaining)
            if thread.is_alive() or time.monotonic() > deadline:
                timed_out = True
                break
        if time.monotonic() > deadline:
            timed_out = True

    if timed_out:
        cleanup_deadline = time.monotonic() + max(
            0.0, float(CHILD_THREAD_JOIN_SECONDS)
        )
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_thread = kernel32.OpenThread
            open_thread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_thread.restype = wintypes.HANDLE
            cancel_synchronous_io = kernel32.CancelSynchronousIo
            cancel_synchronous_io.argtypes = (wintypes.HANDLE,)
            cancel_synchronous_io.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            for thread in threads:
                if not thread.is_alive() or thread.native_id is None:
                    continue
                thread_handle = open_thread(0x0001, False, thread.native_id)
                if not thread_handle:
                    continue
                try:
                    cancel_synchronous_io(thread_handle)
                finally:
                    close_handle(thread_handle)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                os.close(stream.fileno())
            except (OSError, ValueError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        remaining = cleanup_deadline - time.monotonic()
        if process.poll() is None and remaining > 0:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
        for thread in threads:
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        returncode = CHILD_TIMEOUT_RETURN_CODE

    stdout_text = bytes(stdout).decode("utf-8", errors="replace")
    stderr_text = bytes(stderr).decode("utf-8", errors="replace")
    if overflow:
        stream_name, limit = overflow[0]
        return subprocess.CompletedProcess(
            command,
            CHILD_OVERFLOW_RETURN_CODE,
            stdout_text,
            f"{name} {stream_name} exceeded {limit} byte limit",
        )
    if timed_out:
        timeout_detail = (
            f"{name} timed out after {float(CHILD_TIMEOUT_SECONDS):g} seconds"
        )
        detail_bytes = timeout_detail.encode("utf-8")
        separator = b"\n" if stderr_text else b""
        available = max(
            0,
            MAX_CHILD_STDERR_BYTES - len(separator) - len(detail_bytes),
        )
        captured_bytes = stderr_text.encode("utf-8")[:available]
        captured_text = captured_bytes.decode("utf-8", errors="ignore")
        timeout_message = (
            f"{captured_text}{separator.decode()}{timeout_detail}"
            if captured_text
            else timeout_detail
        )
        return subprocess.CompletedProcess(
            command,
            CHILD_TIMEOUT_RETURN_CODE,
            stdout_text,
            timeout_message,
        )
    return subprocess.CompletedProcess(command, returncode, stdout_text, stderr_text)


def _state_path(project_dir: Path) -> tuple[str, Path] | None:
    confirmed = confirm_project_identity(project_dir, PROJECTS_DIR)
    return (confirmed[0], confirmed[1]) if confirmed is not None else None


def _unavailable_identity(project_dir: Path, as_json: bool) -> int:
    out = {
        "cwd": str(project_dir),
        "slug": None,
        "state_path": None,
        "state_exists": False,
        "identity_confirmed": False,
    }
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("Project identity unavailable")
    return 0


def command_project_state(args: argparse.Namespace) -> int:
    project_dir = _project_dir(args.cwd)
    if project_dir is None:
        return _unavailable_identity(_requested_project_dir(args.cwd), args.json)
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
    confirmed = _state_path(project_dir)
    if confirmed is None:
        return _unavailable_identity(project_dir, args.json)
    slug, state_path = confirmed
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
    if project_dir is None:
        return _unavailable_identity(_requested_project_dir(args.cwd), args.json)
    confirmed = _state_path(project_dir)
    if confirmed is None:
        return _unavailable_identity(project_dir, args.json)
    slug, state_path = confirmed
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
    if project_dir is None:
        if args.json:
            _unavailable_identity(_requested_project_dir(args.cwd), True)
        return 0
    occurred = datetime.now().astimezone()
    occurred_at = occurred.isoformat(timespec="microseconds")
    session_id = args.session_id or f"codex-{occurred.strftime('%Y%m%d-%H%M%S')}"
    confirmed = _state_path(project_dir)
    if confirmed is None:
        if args.json:
            _unavailable_identity(project_dir, True)
        return 0
    slug, state_path = confirmed

    # Build the payload for session_end_project_tag.py
    payload = {
        "session_id": session_id,
        "reason": args.reason,
        "transcript_path": getattr(args, "transcript", "") or "",
        "cwd": str(project_dir),
        "project_slug": slug,
        "project_root": str(project_dir),
        "occurred_at": occurred_at,
    }

    transcript_path = payload["transcript_path"]
    force_stub = bool(getattr(args, "force_stub", False))

    if not transcript_path and not force_stub:
        # No transcript available — record heartbeat in state.json only,
        # do NOT pollute knowledge/daily/ with stub blocks.
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
    flush_spawned = False
    if transcript_path and Path(transcript_path).exists():
        # flush_memory --event only accepts session-end|pre-compact.
        # Map Codex reasons into that enum; keep original reason in --trigger.
        flush_event = "session-end"
        reason = (args.reason or "").lower()
        if "compact" in reason:
            flush_event = "pre-compact"
        trigger = args.trigger or args.reason or "codex"
        _spawn_flush_memory(
            session_id,
            flush_event,
            transcript_path,
            trigger,
            slug,
            str(project_dir),
            occurred_at,
        )
        flush_spawned = True

    if args.json:
        print(
            json.dumps(
                {
                    "cwd": str(project_dir),
                    "slug": slug,
                    "state_path": str(state_path),
                    "daily_log_written": result.returncode == 0,
                    "flush_spawned": flush_spawned,
                    "reason": args.reason,
                    "session_id": session_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return result.returncode

    if result.returncode == 0:
        print(f"Daily log tagged for slug: {slug}")
        print(f"Reason: {args.reason}")
        print(f"Session id: {session_id}")
        if flush_spawned:
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
    old behavior of writing empty stub blocks into knowledge/daily/. The
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
    except Exception as e:  # noqa: BLE001
        print(f"codex_memory: {type(e).__name__}: {e}", file=sys.stderr)


def _spawn_flush_memory(
    session_id: str,
    event: str,
    transcript: str,
    trigger: str,
    project_slug: str,
    project_root: str,
    occurred_at: str,
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
                "--project-slug",
                project_slug,
                "--project-root",
                project_root,
                "--occurred-at",
                occurred_at,
            ],
        )
    except Exception as e:  # noqa: BLE001
        print(f"codex_memory: {type(e).__name__}: {e}", file=sys.stderr)


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
