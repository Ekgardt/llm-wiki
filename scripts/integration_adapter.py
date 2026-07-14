"""Normalize native host lifecycle events before existing capture pipelines."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from event_envelope import EventEnvelope, build_event_envelope
from maybe_compile import spawn_compile_if_idle
from memory_state import ROOT, STATE_ROOT, spawn_detached, update_state
from project_journal import CheckpointReducer, ProjectStore
from secret_redact import redact_secrets
from session_start_context import build_context as build_session_start_context
from session_start_project_state import _compute_slug

SCRIPTS_DIR = Path(__file__).resolve().parent
DELEGATE_TIMEOUT_SECONDS = 10
MAINTENANCE_DRAIN_TIMEOUT_SECONDS = 600
MAX_TRANSCRIPT_TEXT_CHARS = 8000
TRANSIENT_CREATE_ATTEMPTS = 10
SOURCES = frozenset({"claude", "opencode", "codex"})
EVENTS = frozenset(
    {"session_start", "session_end", "pre_compact", "user_prompt", "post_tool_use"}
)
CHECKPOINT_SIGNAL_FIELDS = frozenset(
    {
        "checkpoint_type",
        "dirty",
        "changed",
        "significant",
        "decision",
        "correction",
        "blocker_opened",
        "blocker_closed",
        "task_completed",
        "task_cancelled",
        "ownership_transferred",
        "significant_failure",
        "public_contract_changed",
        "test_result_changed",
        "token_percent",
        "compaction_confirmed",
        "project_delta",
        "branch",
    }
)
DELEGATES = frozenset(
    {
        "session_start_context.py",
        "session_start_project_state.py",
        "precompact_capture.py",
        "session_end_capture.py",
        "session_end_project_tag.py",
        "user_prompt_capture.py",
        "post_tool_capture.py",
        "heartbeat_record.py",
    }
)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str)), None)


def _safe_string(value: str | None) -> str | None:
    return redact_secrets(value) if value is not None else None


def _session(source: str, raw: Mapping[str, Any]) -> str | None:
    info = raw.get("sessionInfo")
    nested = info.get("id") if isinstance(info, Mapping) else None
    if source == "opencode":
        return _first_string(nested, raw.get("sessionId"), raw.get("sessionID"))
    return _string(raw.get("session_id"))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid integration event")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid integration event") from exc


def _tool_payload(source: str, raw: Mapping[str, Any]) -> dict[str, str]:
    tool_name = _first_string(raw.get("tool_name"), raw.get("tool")) or ""
    tool_name = {
        "edit": "Edit",
        "write": "Write",
        "multi_edit": "MultiEdit",
        "multiedit": "MultiEdit",
        "notebook_edit": "NotebookEdit",
        "notebookedit": "NotebookEdit",
        "bash": "Bash",
    }.get(tool_name.lower(), tool_name)
    tool_input = raw.get("tool_input") if source != "opencode" else raw.get("input")
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    target = _first_string(
        tool_input.get("filePath"),
        tool_input.get("file_path"),
        tool_input.get("command"),
        raw.get("target"),
    ) or ""
    return {"tool_name": tool_name, "target": target}


def normalize_event(
    source: str,
    event: str,
    raw: Mapping[str, Any],
    *,
    occurred_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> EventEnvelope:
    """Map one host-shaped event to the canonical, redacted envelope."""
    if source not in SOURCES or event not in EVENTS or not isinstance(raw, Mapping):
        raise ValueError("invalid integration event")

    if event == "session_start":
        payload: dict[str, Any] = {
            "reason": _first_string(
                raw.get("reason"), raw.get("trigger"), raw.get("source")
            )
        }
    elif event in {"session_end", "pre_compact"}:
        payload = {
            "reason": _first_string(raw.get("reason"), raw.get("trigger")),
            "transcript_path": _first_string(
                raw.get("transcript_path"),
                raw.get("transcriptPath"),
                raw.get("transcript"),
            ),
        }
        if "transcript_text" in raw:
            transcript_text = raw.get("transcript_text")
            if not isinstance(transcript_text, str) or len(transcript_text) > MAX_TRANSCRIPT_TEXT_CHARS:
                raise ValueError("invalid integration event")
            payload["transcript_text"] = transcript_text
    elif event == "user_prompt":
        payload = {"prompt": _string(raw.get("prompt"))}
    else:
        payload = _tool_payload(source, raw)

    for name in CHECKPOINT_SIGNAL_FIELDS:
        if name in raw:
            payload[name] = raw[name]
    if event == "post_tool_use":
        if payload["tool_name"] in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
            payload.setdefault("changed", True)
            payload.setdefault("dirty", True)
        if payload.get("checkpoint_type") == "significant_failure":
            payload["significant_failure"] = True
    payload["host_progress_signals"] = source in {"claude", "opencode"}
    if event == "session_start" and payload.get("reason") == "compact":
        payload["compaction_confirmed"] = True

    source_time = occurred_at or _parse_timestamp(raw.get("timestamp"))
    return build_event_envelope(
        event_type=event,
        payload=payload,
        occurred_at=source_time,
        captured_at=captured_at,
        agent=_safe_string(_string(raw.get("agent"))),
        session=_safe_string(_session(source, raw)),
        project=_safe_string(_string(raw.get("project"))),
        worktree=_safe_string(
            _first_string(raw.get("cwd"), raw.get("directory"), raw.get("projectRoot"))
        ),
        severity=_safe_string(_string(raw.get("severity"))),
        parent_event_id=_safe_string(_string(raw.get("parent_event_id"))),
        source_event_id=_safe_string(
            _first_string(
                raw.get("event_id"),
                raw.get("eventId"),
                raw.get("tool_use_id"),
                raw.get("toolCallID"),
                raw.get("callID"),
            )
        ),
        redact=redact_secrets,
    )


def _run_delegate(
    name: str,
    payload: Mapping[str, Any],
    *,
    forward_stdout: bool = False,
    project_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if name not in DELEGATES:
        raise ValueError("invalid integration delegate")
    env = os.environ.copy()
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name)],
        cwd=str(ROOT),
        env=env,
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=DELEGATE_TIMEOUT_SECONDS,
    )
    if result.returncode == 0 and forward_stdout and _is_hook_output(result.stdout):
        sys.stdout.write(result.stdout)
    return result


def _is_hook_output(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    hook_output = parsed.get("hookSpecificOutput")
    return isinstance(hook_output, dict) and isinstance(
        hook_output.get("additionalContext"), str
    )


def _canonical_capture_payload(envelope: EventEnvelope) -> dict[str, Any]:
    payload = envelope.to_dict()["payload"]
    common = {
        "session_id": envelope.session,
        "cwd": envelope.worktree,
        "agent": envelope.agent,
        "severity": envelope.severity,
        "parent_event_id": envelope.parent_event_id,
        "event_id": envelope.event_id,
    }
    if envelope.event_type == "post_tool_use":
        common.update(
            {
                "tool_name": payload["tool_name"],
                "tool_input": {"filePath": payload["target"], "command": payload["target"]},
            }
        )
    elif envelope.event_type == "user_prompt":
        common["prompt"] = payload["prompt"]
    else:
        common.update(
            {
                "reason": payload.get("reason"),
                "transcript_path": payload.get("transcript_path"),
            }
        )
        if envelope.event_type == "pre_compact":
            common["trigger"] = payload.get("reason")
    for name in CHECKPOINT_SIGNAL_FIELDS | {"host_progress_signals"}:
        if name in payload:
            common[name] = payload[name]
    return common


def _checkpoint_observation(envelope: EventEnvelope) -> dict[str, object]:
    payload = envelope.payload
    event_type = str(payload.get("checkpoint_type") or envelope.event_type)
    if payload.get("compaction_confirmed") is True:
        event_type = "compaction_confirmed"
    elif isinstance(payload.get("token_percent"), (int, float)):
        event_type = "token_usage"
    else:
        for signal in (
            "decision",
            "correction",
            "blocker_opened",
            "blocker_closed",
            "task_completed",
            "task_cancelled",
            "ownership_transferred",
            "significant_failure",
            "public_contract_changed",
            "test_result_changed",
        ):
            if payload.get(signal) is True:
                event_type = signal
                break
    if envelope.event_type == "post_tool_use" and event_type == "post_tool_use":
        if envelope.severity in {"error", "fatal"}:
            event_type = "significant_failure"
        elif payload.get("changed") is True:
            event_type = "mutation"
    observation: dict[str, object] = {
        "type": event_type,
        "event_id": envelope.event_id,
    }
    for name in ("dirty", "changed", "significant"):
        if name in payload:
            observation[name] = payload[name]
    if event_type == "token_usage":
        observation["percent"] = payload["token_percent"]
    return observation


def _empty_delta() -> dict[str, object]:
    close = {"id": "checkpoint-none", "action": "close", "value": ""}
    return {
        "goal": dict(close),
        "phase": dict(close),
        "current_task": dict(close),
        "next_actions": [],
        "decisions": [],
        "blockers": [],
        "changed_files": [],
        "commands": [],
        "verification": [],
    }


def _checkpoint_event(
    envelope: EventEnvelope,
    slug: str,
    reason: str,
) -> dict[str, object]:
    raw_delta = envelope.to_dict()["payload"].get("project_delta")
    delta = dict(raw_delta) if isinstance(raw_delta, Mapping) else _empty_delta()
    return {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": envelope.event_id,
        "idempotency_key": f"{envelope.event_id}:{reason}",
        "provenance": {
            "agent": envelope.agent or "unknown",
            "session": envelope.session or "unknown",
            "worktree": envelope.worktree or "unknown",
            "branch": str(envelope.payload.get("branch") or "unknown"),
            "source_event": envelope.source_event_id or envelope.event_id,
        },
        "trigger": str(_checkpoint_observation(envelope)["type"]),
        "reason": reason,
        "delta": delta,
        "evidence_event_ids": [envelope.event_id],
    }


def _observe_project_checkpoint(envelope: EventEnvelope) -> None:
    """Observe one envelope once and persist any resulting project checkpoint."""
    slug, project_dir = _project_context(envelope)
    if not slug or project_dir is None:
        return
    observation = _checkpoint_observation(envelope)
    decision_box: list[object] = []
    session_key = envelope.session or "unknown"
    state_key = f"{slug}:{session_key}"

    def mutate(state: dict[str, Any]) -> None:
        reducers = state.setdefault("project_checkpoint_reducers", {})
        reducer_state = reducers.get(state_key)
        reducer = CheckpointReducer.from_state(
            reducer_state if isinstance(reducer_state, Mapping) else None
        )
        if not reducer_state:
            reducer.host_progress_signals = (
                envelope.payload.get("host_progress_signals") is True
            )
        decision_box.append(reducer.observe(observation, now=envelope.occurred_at))
        reducers[state_key] = reducer.to_state()
        if len(reducers) > 128:
            reducers.pop(next(iter(reducers)))

    update_state(mutate, lock_timeout=0.5)
    decision = decision_box[0] if decision_box else None
    if decision is None or getattr(decision, "reason", None) == "session_start_recovery":
        return
    ProjectStore(ROOT, STATE_ROOT).checkpoint(
        slug,
        _checkpoint_event(envelope, slug, decision.reason),
        f"lifecycle:{envelope.event_id[:16]}",
    )


def _project_context(envelope: EventEnvelope) -> tuple[str | None, Path | None]:
    if not envelope.worktree:
        return None, None
    try:
        project_dir = Path(envelope.worktree).resolve()
        slug = _compute_slug(project_dir, ROOT / "knowledge" / "projects")
    except (OSError, ValueError):
        return None, None
    return slug, project_dir


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _validate_transient_dir(
    state_root: Path,
    path: Path,
    expected: os.stat_result | None = None,
) -> os.stat_result:
    info = path.lstat()
    if _is_reparse_point(path) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("transient directory is not secure")
    try:
        path.resolve(strict=True).relative_to(state_root)
    except (OSError, ValueError) as exc:
        raise PermissionError("transient directory escaped state root") from exc
    if expected is not None and not _same_identity(info, expected):
        raise PermissionError("transient directory identity changed")
    return info


def _secure_transient_dir() -> tuple[Path, Path, os.stat_result]:
    state_root = Path(STATE_ROOT).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    current = state_root
    for name in ("cache", "transient-transcripts"):
        current = current / name
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = _validate_transient_dir(state_root, current)
    if os.name == "posix":
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o022:
            raise PermissionError("transient directory is not private")
        if mode != 0o700:
            current.chmod(0o700)
        info = _validate_transient_dir(state_root, current)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("transient directory is not private")
    else:
        _restrict_file_permissions(current)
        info = _validate_transient_dir(state_root, current, info)
    return state_root, current, info


def _same_file(path: Path, opened: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(current.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _cleanup_created_transient(path: Path, opened: os.stat_result) -> None:
    if _same_file(path, opened):
        try:
            path.unlink()
        except OSError:
            pass


def _write_all(descriptor: int, text: str) -> None:
    remaining = memoryview(text.encode("utf-8"))
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("transient write failed")
        remaining = remaining[written:]


def _same_file_at(directory_fd: int, name: str, opened: os.stat_result) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and _same_identity(current, opened)


def _write_posix_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    directory_fd = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    descriptor = -1
    opened: os.stat_result | None = None
    name: str | None = None
    succeeded = False
    try:
        directory_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_info.st_mode) or not _same_identity(
            directory_info, parent_info
        ):
            raise PermissionError("transient directory identity changed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        for _ in range(TRANSIENT_CREATE_ATTEMPTS):
            name = f"{envelope.event_id}-{secrets.token_hex(16)}.txt"
            try:
                descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("could not create unique transient transcript")

        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("transient file is not regular")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, text)
        os.fsync(descriptor)
        _validate_transient_dir(state_root, parent, directory_info)
        if not _same_file_at(directory_fd, name, opened):
            raise PermissionError("transient file identity changed")
        succeeded = True
        return parent / name
    finally:
        if not succeeded and opened is not None and name is not None:
            if _same_file_at(directory_fd, name, opened):
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _windows_path_from_fd(descriptor: int) -> Path | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        get_final_path.restype = ctypes.c_uint32
        length = get_final_path(handle, None, 0, 0)
        if not length:
            return None
        buffer = ctypes.create_unicode_buffer(length + 1)
        if not get_final_path(handle, buffer, len(buffer), 0):
            return None
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)
    except (ImportError, OSError, ValueError):
        return None


def _write_windows_transient(
    envelope: EventEnvelope,
    text: str,
    state_root: Path,
    parent: Path,
    parent_info: os.stat_result,
) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    path: Path | None = None
    cleanup_path: Path | None = None
    opened: os.stat_result | None = None
    for _ in range(TRANSIENT_CREATE_ATTEMPTS):
        path = parent / f"{envelope.event_id}-{secrets.token_hex(16)}.txt"
        try:
            descriptor = os.open(path, flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("could not create unique transient transcript")

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("transient file is not regular")
        resolved = path.resolve(strict=True)
        resolved.relative_to(state_root)
        if resolved.parent != parent.resolve(strict=True) or not _same_file(path, opened):
            raise PermissionError("transient file containment changed")
        _restrict_file_permissions(path)
        if not _same_file(path, opened):
            raise PermissionError("transient file identity changed")
        _write_all(descriptor, text)
        os.fsync(descriptor)
        cleanup_path = _windows_path_from_fd(descriptor) or path
        _validate_transient_dir(state_root, parent, parent_info)
        if not _same_file(path, opened):
            raise PermissionError("transient file identity changed")
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if path is not None and opened is not None:
            _cleanup_created_transient(cleanup_path or path, opened)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return path


def _write_transient_transcript(envelope: EventEnvelope, text: str) -> Path:
    state_root, parent, parent_info = _secure_transient_dir()
    if os.name == "posix":
        return _write_posix_transient(
            envelope, text, state_root, parent, parent_info
        )
    return _write_windows_transient(envelope, text, state_root, parent, parent_info)


def _restrict_file_permissions(path: Path) -> None:
    if os.name == "nt":
        username = os.environ.get("USERNAME")
        if not username:
            raise PermissionError("transient permissions unavailable")
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:(R,W)",
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            raise PermissionError("transient permissions unavailable")
        return
    path.chmod(0o600)


def _record_activity(
    envelope: EventEnvelope,
    slug: str | None,
    project_dir: Path | None,
) -> bool:
    if not slug or project_dir is None:
        return False
    heartbeat = _run_delegate(
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
        candidate.relative_to((STATE_ROOT / "cache" / "transient-transcripts").resolve())
        candidate.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _run_session_start_maintenance() -> int:
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "drain"],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=MAINTENANCE_DRAIN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        spawn_compile_if_idle()
    except Exception:  # noqa: BLE001
        pass
    return 0


def ingest_event(
    envelope: EventEnvelope,
    *,
    force_stub: bool = False,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Apply shared lifecycle persistence policy to a normalized envelope."""
    _observe_project_checkpoint(envelope)
    payload = _canonical_capture_payload(envelope)
    slug, project_dir = _project_context(envelope)
    result: dict[str, Any] = {
        "slug": slug,
        "heartbeat_recorded": False,
        "daily_log_written": False,
        "flush_spawned": False,
        "transcript_path": payload.get("transcript_path"),
        "returncode": 0,
    }
    if envelope.event_type == "session_start":
        result["heartbeat_recorded"] = _record_activity(envelope, slug, project_dir)
        maintenance_pid = spawn_detached([
            sys.executable,
            str(SCRIPTS_DIR / "integration_adapter.py"),
            "--maintenance",
        ])
        result["maintenance_scheduled"] = maintenance_pid is not None
        result["context"] = build_session_start_context()
    elif envelope.event_type == "user_prompt":
        _run_delegate("user_prompt_capture.py", payload, forward_stdout=True, project_dir=project_dir)
    elif envelope.event_type == "post_tool_use":
        _run_delegate("post_tool_capture.py", payload, project_dir=project_dir)
    elif envelope.event_type == "pre_compact":
        transient_path: Path | None = None
        ownership_transferred = False
        try:
            transcript_text = envelope.payload.get("transcript_text")
            if isinstance(transcript_text, str) and transcript_text:
                transient_path = _write_transient_transcript(envelope, transcript_text)
                payload["transcript_path"] = str(transient_path)
                payload["ephemeral_transcript"] = True
                result["transcript_path"] = str(transient_path)
            if payload.get("transcript_path"):
                captured = _run_delegate(
                    "precompact_capture.py", payload, project_dir=project_dir
                )
                ownership_transferred = _flush_started(captured)
                result["flush_spawned"] = ownership_transferred
                result["returncode"] = getattr(captured, "returncode", 0)
            else:
                result["heartbeat_recorded"] = _record_activity(
                    envelope, slug, project_dir
                )
        finally:
            if transient_path is not None and not ownership_transferred:
                _cleanup_runtime_transient(transient_path)
    elif envelope.event_type == "session_end":
        transient_path: Path | None = None
        ownership_transferred = False
        try:
            transcript_text = envelope.payload.get("transcript_text")
            if isinstance(transcript_text, str) and transcript_text:
                transient_path = _write_transient_transcript(envelope, transcript_text)
                payload["transcript_path"] = str(transient_path)
                payload["ephemeral_transcript"] = True
                result["transcript_path"] = str(transient_path)
            safe_trigger = redact_secrets(trigger) if isinstance(trigger, str) else None
            payload["trigger"] = safe_trigger or payload.get("reason")
            has_transcript = bool(payload.get("transcript_path"))
            if has_transcript or force_stub:
                tagged = _run_delegate(
                    "session_end_project_tag.py", payload, project_dir=project_dir
                )
                result["daily_log_written"] = getattr(tagged, "returncode", 0) == 0
                result["returncode"] = getattr(tagged, "returncode", 0)
            if has_transcript:
                captured = _run_delegate(
                    "session_end_capture.py", payload, project_dir=project_dir
                )
                ownership_transferred = _flush_started(captured)
                result["flush_spawned"] = ownership_transferred
                result["returncode"] = getattr(captured, "returncode", 0)
            elif not force_stub and slug and project_dir:
                result["heartbeat_recorded"] = _record_activity(
                    envelope, slug, project_dir
                )
        finally:
            if transient_path is not None and not ownership_transferred:
                _cleanup_runtime_transient(transient_path)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source")
    parser.add_argument("--event")
    parser.add_argument("--delegate")
    parser.add_argument("--checkpoint-type")
    parser.add_argument("--maintenance", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Host-safe CLI: invalid input and capture failures never escape."""
    try:
        args = _parser().parse_args(argv)
        if args.maintenance:
            return _run_session_start_maintenance()
        if not args.source or not args.event:
            raise ValueError("invalid integration event")
        raw = json.loads(sys.stdin.read() or "{}")
        if not isinstance(raw, dict):
            raise ValueError("invalid integration event")
        if args.checkpoint_type:
            raw["checkpoint_type"] = args.checkpoint_type
        envelope = normalize_event(args.source, args.event, raw)
        if args.delegate:
            _observe_project_checkpoint(envelope)
            _run_delegate(
                args.delegate,
                _canonical_capture_payload(envelope),
                forward_stdout=args.delegate
                in {
                    "session_start_context.py",
                    "session_start_project_state.py",
                    "user_prompt_capture.py",
                },
            )
        else:
            result = ingest_event(envelope)
            if envelope.event_type == "session_start":
                print(json.dumps(result, ensure_ascii=False))
    except (Exception, SystemExit):  # noqa: BLE001
        print("integration_adapter: capture skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
