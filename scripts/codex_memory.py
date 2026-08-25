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
from collections.abc import Iterator
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
from integration_config_backup import publish_configuration  # noqa: E402
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

    hooks_state = sub.add_parser("hooks-state")
    hooks_state.add_argument("--source", required=True)
    hooks_state.add_argument("--config", required=True)

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


def _invalid_hook() -> ValueError:
    return ValueError("invalid Codex hook input")


def _require_known_event(raw: dict[str, Any]) -> str:
    event_name = raw.get("hook_event_name")
    allowed_fields = CODEX_HOOK_FIELDS.get(event_name)
    if allowed_fields is None or not set(raw).issubset(allowed_fields):
        raise _invalid_hook()
    return str(event_name)


def _common_fields_valid(raw: dict[str, Any]) -> bool:
    transcript_path = raw.get("transcript_path")
    required = {"session_id", "transcript_path", "cwd", "hook_event_name", "model"}
    typed = all(isinstance(raw.get(name), str) for name in ("session_id", "cwd", "model"))
    return (
        required <= set(raw)
        and typed
        and (transcript_path is None or isinstance(transcript_path, str))
    )


def _normalized_common(raw: dict[str, Any]) -> dict[str, Any]:
    if not _common_fields_valid(raw):
        raise _invalid_hook()
    return {
        "session_id": raw["session_id"],
        "cwd": raw["cwd"],
        "transcript_path": raw.get("transcript_path"),
    }


def _applied_turn_id(normalized: dict[str, Any], raw: dict[str, Any], event_name: str) -> None:
    if event_name == "SessionStart":
        return
    turn_id = raw.get("turn_id")
    if not isinstance(turn_id, str):
        raise _invalid_hook()
    normalized["event_id"] = turn_id


def _optional_agent_strings_valid(raw: dict[str, Any]) -> bool:
    return all(
        isinstance(raw[name], str)
        for name in ("agent_id", "agent_type")
        if name in raw
    )


def _session_start_reason(raw: dict[str, Any]) -> tuple[str, str]:
    source = raw.get("source")
    permitted = raw.get("permission_mode") in CODEX_PERMISSION_MODES
    if source not in {"startup", "resume", "clear", "compact"} or not permitted:
        raise _invalid_hook()
    return str(source), "session_start"


def _compact_trigger(raw: dict[str, Any]) -> str:
    trigger = raw.get("trigger")
    if trigger not in {"manual", "auto"} or not _optional_agent_strings_valid(raw):
        raise _invalid_hook()
    return str(trigger)


def _pre_compact_reason(raw: dict[str, Any]) -> tuple[str, str]:
    return _compact_trigger(raw), "pre_compact"


def _post_compact_reason(raw: dict[str, Any]) -> tuple[str, str]:
    _compact_trigger(raw)
    return "compact", "session_start"


def _last_message_valid(raw: dict[str, Any]) -> bool:
    if "last_assistant_message" not in raw:
        return False
    message = raw["last_assistant_message"]
    return message is None or isinstance(message, str)


def _stop_reason(raw: dict[str, Any]) -> tuple[str, str]:
    permitted = raw.get("permission_mode") in CODEX_PERMISSION_MODES
    if not permitted or not isinstance(raw.get("stop_hook_active"), bool):
        raise _invalid_hook()
    if not _last_message_valid(raw):
        raise _invalid_hook()
    return "stop", "session_end"


_CODEX_EVENT_READERS = {
    "SessionStart": _session_start_reason,
    "PreCompact": _pre_compact_reason,
    "PostCompact": _post_compact_reason,
}


def normalize_codex_hook(raw: dict[str, Any]):
    """Validate one official Codex hook payload and map it to shared lifecycle."""
    event_name = _require_known_event(raw)
    normalized = _normalized_common(raw)
    _applied_turn_id(normalized, raw, event_name)
    reader = _CODEX_EVENT_READERS.get(event_name, _stop_reason)
    normalized["reason"], event_type = reader(raw)
    return normalize_occurrence_event("codex", event_type, normalized)


def _read_hook_input() -> dict[str, Any]:
    raw = sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1)
    if not raw or len(raw.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        raise ValueError("invalid Codex hook input")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid Codex hook input")
    return value


def _session_start_output(result: dict[str, Any]) -> dict[str, Any]:
    context = result.get("context")
    if not isinstance(context, str) or not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _hook_output(event_name: object, result: dict[str, Any]) -> dict[str, Any]:
    if event_name != "SessionStart":
        return {}
    return _session_start_output(result)


def _dispatch_hook(seen: dict[str, object]) -> None:
    """`seen` carries the event name out, so the failure path can answer Stop."""
    raw = _read_hook_input()
    seen["event_name"] = raw["hook_event_name"]
    result = ingest_event(normalize_codex_hook(raw), trigger="codex-hook")
    print(json.dumps(_hook_output(seen["event_name"], result), ensure_ascii=False))


def _report_hook_failure(event_name: object) -> None:
    if event_name == "Stop":
        print("{}")
    print("codex_memory: hook skipped", file=sys.stderr)


def command_hook(args: argparse.Namespace) -> int:
    """Run one official Codex lifecycle callback without risking the host."""
    del args
    seen: dict[str, object] = {}
    try:
        _dispatch_hook(seen)
    except (Exception, SystemExit):  # noqa: BLE001
        _report_hook_failure(seen.get("event_name"))
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


def _invalid_hooks_config() -> ValueError:
    return ValueError("invalid Codex hooks config")


def _hooks_table(document: dict[str, Any]) -> dict[str, Any]:
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        raise _invalid_hooks_config()
    return hooks


def _valid_group(group: object) -> dict[str, Any]:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        raise _invalid_hooks_config()
    return group


def _valid_groups(event_name: object, groups: object) -> list[dict[str, Any]]:
    if not isinstance(event_name, str) or not isinstance(groups, list):
        raise _invalid_hooks_config()
    return [_valid_group(group) for group in groups]


def _valid_handlers(group: dict[str, Any]) -> list[dict[str, Any]]:
    handlers = group["hooks"]
    for handler in handlers:
        if not isinstance(handler, dict):
            raise _invalid_hooks_config()
    return handlers


def _iter_groups(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for event_name, groups in _hooks_table(document).items():
        for group in _valid_groups(event_name, groups):
            yield str(event_name), group


def _iter_handlers(
    document: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    for event_name, group in _iter_groups(document):
        for handler in _valid_handlers(group):
            yield event_name, group, handler


def _handler_signature(
    event_name: str, group: dict[str, Any], handler: dict[str, Any]
) -> tuple[Any, ...]:
    return (
        event_name,
        group.get("matcher"),
        handler.get("type"),
        handler.get("command"),
        handler.get("commandWindows", handler.get("command_windows")),
        handler.get("timeout"),
    )


def _hook_signatures(document: dict[str, Any]) -> list[tuple[Any, ...]]:
    signatures = [
        _handler_signature(event_name, group, handler)
        for event_name, group, handler in _iter_handlers(document)
        if _is_llm_wiki_hook(handler)
    ]
    return sorted(signatures, key=repr)


def _has_active_inline_hooks(document: dict[str, Any]) -> bool:
    return any(
        handler.get("enabled", True) is not False
        for _event_name, _group, handler in _iter_handlers(document)
    )


def _require_safe_config(path: Path, error: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HOOK_CONFIG_BYTES:
        raise ValueError(error)


def _read_codex_toml(config: Path) -> dict[str, Any]:
    _require_safe_config(config, "invalid Codex config")
    try:
        document = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("invalid Codex config") from exc
    if not isinstance(document, dict):
        raise ValueError("invalid Codex config")
    return document


def _codex_hooks_feature_value(features: dict[str, Any]) -> object:
    if "hooks" in features:
        return features["hooks"]
    return features.get("codex_hooks", True)


def _codex_hooks_feature_state(document: dict[str, Any]) -> str:
    features = document.get("features", {})
    if not isinstance(features, dict):
        raise ValueError("invalid Codex config")
    enabled = _codex_hooks_feature_value(features)
    if not isinstance(enabled, bool):
        raise ValueError("invalid Codex config")
    if enabled:
        return "enabled"
    return "disabled"


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


def _mcp_servers(document: dict[str, Any]) -> dict[str, Any] | str:
    servers = document.get("mcp_servers")
    if servers is None:
        return "absent"
    if not isinstance(servers, dict):
        return "conflict"
    return servers


def _mcp_entry(document: dict[str, Any]) -> dict[str, Any] | str:
    servers = _mcp_servers(document)
    if isinstance(servers, str):
        return servers
    table = servers.get("llm-wiki")
    if table is None:
        return "absent"
    if not isinstance(table, dict):
        return "conflict"
    return table


def _mcp_expected_args(vault_root: Path) -> list[str]:
    return [
        "run",
        "--locked",
        "--no-sync",
        "--directory",
        str(vault_root),
        "python",
        "scripts/mcp_server.py",
    ]


def _mcp_entry_state(table: dict[str, Any], vault_root: Path) -> str:
    equivalent = (
        table.get("command") == "uv"
        and table.get("args") == _mcp_expected_args(vault_root)
        and table.get("enabled", True) is True
    )
    if equivalent:
        return "equivalent"
    return "conflict"


def _read_codex_document(config: Path) -> dict[str, Any] | str:
    try:
        return _read_codex_toml(config)
    except ValueError:
        return "invalid"


def codex_mcp_config_state(config: Path, vault_root: Path) -> str:
    """Classify the existing Codex MCP entry without modifying TOML."""
    if not config.exists():
        return "absent"
    document = _read_codex_document(config)
    if isinstance(document, str):
        return document
    entry = _mcp_entry(document)
    if isinstance(entry, str):
        return entry
    return _mcp_entry_state(entry, vault_root)


def _read_hooks_document(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    _require_safe_config(path, "invalid Codex hooks config")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise _invalid_hooks_config()
    return value


def _kept_group(group: dict[str, Any]) -> dict[str, Any] | None:
    handlers = [handler for handler in _valid_handlers(group) if not _is_llm_wiki_hook(handler)]
    if not handlers:
        return None
    return {**group, "hooks": handlers}


def _kept_groups(event_name: object, groups: object) -> list[dict[str, Any]]:
    kept = [_kept_group(group) for group in _valid_groups(event_name, groups)]
    return [group for group in kept if group is not None]


def _without_llm_wiki_hooks(existing: dict[str, Any]) -> dict[str, Any]:
    merged_hooks: dict[str, Any] = {}
    for event_name, groups in _hooks_table(existing).items():
        kept = _kept_groups(event_name, groups)
        if kept:
            merged_hooks[event_name] = kept
    return {**existing, "hooks": merged_hooks}


def _write_hooks_document(destination: Path, document: dict[str, Any]) -> None:
    rendered = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    publish_configuration(destination, rendered)


_INLINE_RESULTS = {"disabled": "hooks-disabled", "equivalent": "inline-equivalent"}
_MERGE_EXIT_CODES = {"inline-equivalent": 3, "hooks-disabled": 4}


def _inline_outcome(config: Path | None, template: dict[str, Any]) -> str | None:
    """What the inline `config.toml` hooks decide, or None to write the file."""
    if config is None:
        return None
    state = _inline_hook_state(config, template)
    if state == "conflict":
        raise ValueError("inline Codex hooks: manual merge and trust review required")
    return _INLINE_RESULTS.get(state)


def _template_hooks(template: dict[str, Any]) -> dict[str, Any]:
    hooks = template.get("hooks")
    if not isinstance(hooks, dict):
        raise _invalid_hooks_config()
    return hooks


def _merged_hook_table(cleaned: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    merged = cleaned["hooks"]
    for event_name, groups in _template_hooks(template).items():
        if not isinstance(event_name, str) or not isinstance(groups, list):
            raise ValueError("invalid Codex hooks template")
        merged.setdefault(event_name, []).extend(groups)
    return merged


def merge_codex_hooks(
    source: Path, destination: Path, *, config: Path | None = None
) -> str:
    """Replace only LLM-Wiki hook handlers while preserving user configuration."""
    template = _read_hooks_document(source, missing_ok=False)
    outcome = _inline_outcome(config, template)
    if outcome is not None:
        return outcome
    cleaned = _without_llm_wiki_hooks(_read_hooks_document(destination, missing_ok=True))
    merged = _merged_hook_table(cleaned, template)
    _write_hooks_document(destination, {**cleaned, "hooks": merged})
    return "json-merged"


def _merge_failure_code(error: Exception) -> int:
    if "manual merge and trust review" in str(error):
        print(
            "codex_memory: inline Codex hooks require manual merge and trust review; "
            "hooks.json unchanged",
            file=sys.stderr,
        )
        return 2
    print("codex_memory: hook install skipped", file=sys.stderr)
    return 1


def command_merge_hooks(args: argparse.Namespace) -> int:
    try:
        result = merge_codex_hooks(
            Path(args.source),
            Path(args.destination),
            config=Path(args.config) if args.config else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _merge_failure_code(exc)
    code = _MERGE_EXIT_CODES.get(result)
    if code is None:
        return 0
    print(result)
    return code


def _script_payload(stdout: str) -> dict[str, Any]:
    if not stdout.strip():
        return {}
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _project_state_report(
    project_dir: Path, slug: str, state_path: Path, context: str
) -> dict[str, Any]:
    return {
        "cwd": str(project_dir),
        "slug": slug,
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
        "additional_context": context,
    }


def _print_project_state(slug: str, state_path: Path, context: str) -> None:
    print(f"Slug: {slug}")
    print(f"State path: {state_path}")
    print()
    print(context or "(no project-state context emitted)")


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
    payload = _script_payload(result.stdout)
    context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    slug, state_path = _state_path(project_dir)
    if args.json:
        report = _project_state_report(project_dir, slug, state_path, context)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    _print_project_state(slug, state_path, context)
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


def _daily_envelope(args: argparse.Namespace):
    try:
        return normalize_occurrence_event(
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
        return None


def _daily_trigger(args: argparse.Namespace, reason: str) -> str:
    raw_trigger = getattr(args, "trigger", "")
    trigger = redact_secrets(raw_trigger) if isinstance(raw_trigger, str) else ""
    return trigger or reason or "codex"


def _daily_ingest(envelope, args: argparse.Namespace, reason: str):
    try:
        return ingest_event(
            envelope,
            force_stub=bool(getattr(args, "force_stub", False)),
            trigger=_daily_trigger(args, reason),
        )
    except Exception:  # noqa: BLE001
        return None


def _daily_state_path(slug: object) -> str | None:
    if not slug:
        return None
    return str(PROJECTS_DIR / str(slug) / "state.md")


def _daily_report(
    project_dir: Path, result: dict[str, Any], reason: str, session_id: str
) -> str:
    slug = result.get("slug")
    return json.dumps(
        {
            "cwd": str(project_dir),
            "slug": slug,
            "state_path": _daily_state_path(slug),
            "daily_log_written": result["daily_log_written"],
            "heartbeat_recorded": result["heartbeat_recorded"],
            "flush_spawned": result["flush_spawned"],
            "reason": reason,
            "session_id": session_id,
        },
        ensure_ascii=False,
        indent=2,
    )


def _print_heartbeat(result: dict[str, Any], reason: str) -> None:
    print(f"Heartbeat recorded for slug: {result.get('slug')} (no daily-log stub)")
    print(f"Reason: {reason}")


def _print_daily_tag(result: dict[str, Any], reason: str, session_id: str) -> None:
    print(f"Daily log tagged for slug: {result.get('slug')}")
    print(f"Reason: {reason}")
    print(f"Session id: {session_id}")
    if result["flush_spawned"]:
        print(f"Flush spawned for transcript: {result.get('transcript_path') or ''}")


def _print_daily_result(result: dict[str, Any], reason: str, session_id: str) -> None:
    if result["heartbeat_recorded"]:
        _print_heartbeat(result, reason)
        return
    _print_daily_tag(result, reason, session_id)


def _daily_outcome(
    args: argparse.Namespace, envelope, result: dict[str, Any], reason: str
) -> int:
    session_id = envelope.session or ""
    project_dir = _project_dir(envelope.worktree or os.getcwd())
    returncode = int(result.get("returncode", 0))
    if bool(getattr(args, "json", False)):
        print(_daily_report(project_dir, result, reason, session_id))
        return returncode
    if returncode != 0:
        print("codex_memory: capture failed", file=sys.stderr)
        return returncode
    _print_daily_result(result, reason, session_id)
    return returncode


def command_daily_log(args: argparse.Namespace) -> int:
    """Normalize a Codex lifecycle event and present shared ingest results."""
    envelope = _daily_envelope(args)
    if envelope is None:
        print("codex_memory: capture skipped", file=sys.stderr)
        return 0
    reason = envelope.payload["reason"] or ""
    result = _daily_ingest(envelope, args, reason)
    if result is None:
        print("codex_memory: capture failed", file=sys.stderr)
        return 0
    return _daily_outcome(args, envelope, result, reason)


def command_hooks_state(args: argparse.Namespace) -> int:
    """Say what the inline `config.toml` hooks decide, writing nothing.

    The installer asks this before the ownership transaction: it may only own
    `hooks.json` when the inline configuration neither disables the feature,
    already carries our handlers, nor contradicts them.
    """
    try:
        template = _read_hooks_document(Path(args.source), missing_ok=False)
        print(_inline_hook_state(Path(args.config), template))
    except (OSError, ValueError, json.JSONDecodeError):
        print("unknown")
        return 1
    return 0


def command_config_state(args: argparse.Namespace) -> int:
    print(codex_mcp_config_state(Path(args.config), Path(args.vault_root)))
    return 0


_COMMANDS = {
    "project-state": command_project_state,
    "state-path": command_state_path,
    "lookup-tier": command_lookup_tier,
    "daily-log": command_daily_log,
    "hook": command_hook,
    "merge-hooks": command_merge_hooks,
    "hooks-state": command_hooks_state,
    "config-state": command_config_state,
}


def main() -> int:
    args = parse_args()
    handler = _COMMANDS.get(args.command)
    if handler is None:
        raise ValueError(f"Unknown command: {args.command}")
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
