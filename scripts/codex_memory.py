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
import tempfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

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
MAX_HOOK_INPUT_BYTES = 64 * 1024
MAX_HOOK_CONFIG_BYTES = 256 * 1024
CODEX_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)
CODEX_HOOK_FIELDS = {
    "SessionStart": frozenset(
        {
            "session_id",
            "transcript_path",
            "cwd",
            "hook_event_name",
            "model",
            "permission_mode",
            "source",
        }
    ),
    "PreCompact": frozenset(
        {
            "session_id",
            "turn_id",
            "agent_id",
            "agent_type",
            "transcript_path",
            "cwd",
            "hook_event_name",
            "model",
            "trigger",
        }
    ),
    "PostCompact": frozenset(
        {
            "session_id",
            "turn_id",
            "agent_id",
            "agent_type",
            "transcript_path",
            "cwd",
            "hook_event_name",
            "model",
            "trigger",
        }
    ),
    "Stop": frozenset(
        {
            "session_id",
            "turn_id",
            "transcript_path",
            "cwd",
            "hook_event_name",
            "model",
            "permission_mode",
            "stop_hook_active",
            "last_assistant_message",
        }
    ),
}

sys.path.insert(0, str(SCRIPTS_DIR))

from integration_adapter import (  # noqa: E402
    _observe_checkpoint_fail_open,
    ingest_event,
    normalize_occurrence_event,
)
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
    sub.add_parser("hook")

    merge_hooks = sub.add_parser("merge-hooks")
    merge_hooks.add_argument("--source", required=True)
    merge_hooks.add_argument("--destination", required=True)
    merge_hooks.add_argument("--config")

    config_state = sub.add_parser("config-state")
    config_state.add_argument("--config", required=True)
    config_state.add_argument("--vault-root", required=True)

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


def normalize_codex_hook(raw: dict[str, Any]):
    """Validate one official Codex hook payload and map it to shared lifecycle."""
    event_name = raw.get("hook_event_name")
    allowed_fields = CODEX_HOOK_FIELDS.get(event_name)
    if allowed_fields is None or not set(raw).issubset(allowed_fields):
        raise ValueError("invalid Codex hook input")
    transcript_path = raw.get("transcript_path")
    common_valid = (
        {"session_id", "transcript_path", "cwd", "hook_event_name", "model"}
        <= set(raw)
        and isinstance(raw.get("session_id"), str)
        and isinstance(raw.get("cwd"), str)
        and isinstance(raw.get("model"), str)
        and (transcript_path is None or isinstance(transcript_path, str))
    )
    if not common_valid:
        raise ValueError("invalid Codex hook input")

    normalized = {
        "session_id": raw["session_id"],
        "cwd": raw["cwd"],
        "transcript_path": transcript_path,
    }
    turn_id = raw.get("turn_id")
    if event_name != "SessionStart":
        if not isinstance(turn_id, str):
            raise ValueError("invalid Codex hook input")
        normalized["event_id"] = turn_id

    if event_name == "SessionStart":
        source = raw.get("source")
        if (
            source not in {"startup", "resume", "clear", "compact"}
            or raw.get("permission_mode") not in CODEX_PERMISSION_MODES
        ):
            raise ValueError("invalid Codex hook input")
        normalized["reason"] = source
        event_type = "session_start"
    elif event_name == "PreCompact":
        trigger = raw.get("trigger")
        if trigger not in {"manual", "auto"} or any(
            name in raw and not isinstance(raw[name], str)
            for name in ("agent_id", "agent_type")
        ):
            raise ValueError("invalid Codex hook input")
        normalized["reason"] = trigger
        event_type = "pre_compact"
    elif event_name == "PostCompact":
        trigger = raw.get("trigger")
        if trigger not in {"manual", "auto"} or any(
            name in raw and not isinstance(raw[name], str)
            for name in ("agent_id", "agent_type")
        ):
            raise ValueError("invalid Codex hook input")
        normalized["reason"] = "compact"
        event_type = "session_start"
    else:
        if (
            raw.get("permission_mode") not in CODEX_PERMISSION_MODES
            or not isinstance(raw.get("stop_hook_active"), bool)
            or "last_assistant_message" not in raw
            or raw["last_assistant_message"] is not None
            and not isinstance(raw["last_assistant_message"], str)
        ):
            raise ValueError("invalid Codex hook input")
        normalized["reason"] = "stop"
        event_type = "session_end"
    return normalize_occurrence_event("codex", event_type, normalized)


def _read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1)
    if not raw or len(raw.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("invalid Codex hook input")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid Codex hook input")
    return value


def command_hook(args: argparse.Namespace) -> int:
    """Run one official Codex lifecycle callback without risking the host."""
    del args
    event_name = None
    try:
        raw = _read_hook_input()
        event_name = raw["hook_event_name"]
        result = ingest_event(normalize_codex_hook(raw), trigger="codex-hook")
        output: dict[str, Any] = {}
        if event_name == "SessionStart":
            context = result.get("context")
            if isinstance(context, str) and context:
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context,
                    }
                }
        print(json.dumps(output, ensure_ascii=False))
    except (Exception, SystemExit):  # noqa: BLE001
        if event_name == "Stop":
            print("{}")
        print("codex_memory: hook skipped", file=sys.stderr)
    return 0


def _is_llm_wiki_hook(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    commands = (handler.get("command"), handler.get("commandWindows"))
    return any(
        isinstance(command, str)
        and "codex_memory.py" in command
        and command.rstrip().endswith(" hook")
        for command in commands
    )


def _hook_signatures(document: dict[str, Any]) -> list[tuple[Any, ...]]:
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("invalid Codex hooks config")
    signatures = []
    for event_name, groups in hooks.items():
        if not isinstance(event_name, str) or not isinstance(groups, list):
            raise ValueError("invalid Codex hooks config")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("invalid Codex hooks config")
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise ValueError("invalid Codex hooks config")
                if not _is_llm_wiki_hook(handler):
                    continue
                signatures.append(
                    (
                        event_name,
                        group.get("matcher"),
                        handler.get("type"),
                        handler.get("command"),
                        handler.get("commandWindows", handler.get("command_windows")),
                        handler.get("timeout"),
                    )
                )
    return sorted(signatures, key=repr)


def _has_active_inline_hooks(document: dict[str, Any]) -> bool:
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("invalid Codex hooks config")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ValueError("invalid Codex hooks config")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("invalid Codex hooks config")
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise ValueError("invalid Codex hooks config")
                if handler.get("enabled", True) is not False:
                    return True
    return False


def _read_codex_toml(config: Path) -> dict[str, Any]:
    if config.is_symlink() or not config.is_file() or config.stat().st_size > MAX_HOOK_CONFIG_BYTES:
        raise ValueError("invalid Codex config")
    try:
        document = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("invalid Codex config") from exc
    if not isinstance(document, dict):
        raise ValueError("invalid Codex config")
    return document


def _codex_hooks_feature_state(document: dict[str, Any]) -> str:
    features = document.get("features", {})
    if not isinstance(features, dict):
        raise ValueError("invalid Codex config")
    if "hooks" in features:
        enabled = features["hooks"]
    else:
        enabled = features.get("codex_hooks", True)
    if not isinstance(enabled, bool):
        raise ValueError("invalid Codex config")
    return "enabled" if enabled else "disabled"


def codex_hooks_feature_state(config: Path) -> str:
    """Return the effective user-level lifecycle feature state."""
    if not config.exists():
        return "enabled"
    return _codex_hooks_feature_state(_read_codex_toml(config))


def _inline_hook_state(config: Path, template: dict[str, Any]) -> str:
    if not config.exists():
        return "absent"
    document = _read_codex_toml(config)
    if _codex_hooks_feature_state(document) == "disabled":
        return "disabled"
    if not _has_active_inline_hooks(document):
        return "absent"
    inline = _hook_signatures(document)
    return "equivalent" if inline == _hook_signatures(template) else "conflict"


def codex_mcp_config_state(config: Path, vault_root: Path) -> str:
    """Classify the existing Codex MCP entry without modifying TOML."""
    if not config.exists():
        return "absent"
    try:
        document = _read_codex_toml(config)
    except ValueError:
        return "invalid"
    servers = document.get("mcp_servers")
    if servers is None:
        return "absent"
    if not isinstance(servers, dict):
        return "conflict"
    table = servers.get("llm-wiki")
    if table is None:
        return "absent"
    if not isinstance(table, dict):
        return "conflict"
    expected_args = [
        "run",
        "--directory",
        str(vault_root),
        "python",
        "scripts/mcp_server.py",
    ]
    equivalent = (
        table.get("command") == "uv"
        and table.get("args") == expected_args
        and table.get("enabled", True) is True
    )
    return "equivalent" if equivalent else "conflict"


def _read_hooks_document(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HOOK_CONFIG_BYTES:
        raise ValueError("invalid Codex hooks config")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid Codex hooks config")
    return value


def _without_llm_wiki_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    existing_hooks = existing.get("hooks", {})
    if not isinstance(existing_hooks, dict):
        raise ValueError("invalid Codex hooks config")
    merged_hooks: dict[str, Any] = {}
    for event_name, groups in existing_hooks.items():
        if not isinstance(event_name, str) or not isinstance(groups, list):
            raise ValueError("invalid Codex hooks config")
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("invalid Codex hooks config")
            handlers = [
                handler for handler in group["hooks"] if not _is_llm_wiki_hook(handler)
            ]
            if handlers:
                kept_groups.append({**group, "hooks": handlers})
        if kept_groups:
            merged_hooks[event_name] = kept_groups
    return {**existing, "hooks": merged_hooks}


def _write_hooks_document(destination: Path, document: dict[str, Any]) -> None:
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("invalid Codex hooks config")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def merge_codex_hooks(
    source: Path, destination: Path, *, config: Path | None = None
) -> str:
    """Replace only LLM-Wiki hook handlers while preserving user configuration."""
    template = _read_hooks_document(source, missing_ok=False)
    if config is not None:
        inline_state = _inline_hook_state(config, template)
        if inline_state == "disabled":
            return "hooks-disabled"
        if inline_state == "equivalent":
            return "inline-equivalent"
        if inline_state == "conflict":
            raise ValueError("inline Codex hooks: manual merge and trust review required")
    existing = _read_hooks_document(destination, missing_ok=True)
    cleaned = _without_llm_wiki_hooks(existing)
    template_hooks = template.get("hooks")
    if not isinstance(template_hooks, dict):
        raise ValueError("invalid Codex hooks config")

    merged_hooks = cleaned["hooks"]
    for event_name, groups in template_hooks.items():
        if not isinstance(event_name, str) or not isinstance(groups, list):
            raise ValueError("invalid Codex hooks template")
        merged_hooks.setdefault(event_name, []).extend(groups)

    _write_hooks_document(destination, {**cleaned, "hooks": merged_hooks})
    return "json-merged"


def command_merge_hooks(args: argparse.Namespace) -> int:
    try:
        result = merge_codex_hooks(
            Path(args.source),
            Path(args.destination),
            config=Path(args.config) if args.config else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if "manual merge and trust review" in str(exc):
            print(
                "codex_memory: inline Codex hooks require manual merge and trust review; "
                "hooks.json unchanged",
                file=sys.stderr,
            )
            return 2
        print("codex_memory: hook install skipped", file=sys.stderr)
        return 1
    if result == "inline-equivalent":
        print(result)
        return 3
    if result == "hooks-disabled":
        print(result)
        return 4
    return 0


def command_config_state(args: argparse.Namespace) -> int:
    print(codex_mcp_config_state(Path(args.config), Path(args.vault_root)))
    return 0


def command_project_state(args: argparse.Namespace) -> int:
    project_dir = _project_dir(args.cwd)
    envelope = normalize_occurrence_event(
        "codex",
        "session_start",
        {"cwd": str(project_dir), "reason": "codex-session-start"},
    )
    _observe_checkpoint_fail_open(envelope)
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
        envelope = normalize_occurrence_event(
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
    if args.command == "hook":
        return command_hook(args)
    if args.command == "merge-hooks":
        return command_merge_hooks(args)
    if args.command == "config-state":
        return command_config_state(args)
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
