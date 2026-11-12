"""Doctor's checks of agent hook integrations and the Codex hook probe."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import generation_maintenance  # noqa: E402
import integration_hook_config as _hook_config
from llm_wiki import doctor_schedule, monotonic_deadline, runtime_roots
from llm_wiki.doctor_base import (
    MAX_CONFIG_BYTES,
    RUNTIME_DIRECTORIES,
    SCHEMA_VERSION,
    read_bounded_json,
)
from llm_wiki.doctor_schedule import PROBE_INCOMPLETE, contains_markers

# The Codex hook probe starts `codex app-server` and asks for its hook list: the
# whole probe gets 2 s of doctor's 5 s budget, and it is not started with less than
# 0.25 s left. Basis unknown: values predate measurement (2026-07-17); review when
# the probe is seen timing out on a healthy install.
CODEX_HOOK_PROBE_SECONDS = 2.0
CODEX_HOOK_PROBE_STARTUP_SECONDS = 0.25


# How long a probe that gave up waits for the peer it killed to be reaped.
# Independent of the probe deadline, which by then has passed: with the wait
# bounded by that deadline, Windows returned while the peer was still exiting
# (PR #16, three jobs). The Pyright probe keeps the same 0.5 s budget.
CODEX_HOOK_PROBE_CLEANUP_SECONDS = 0.5


# Output kept from the probed Codex process (untrusted); past it the probe sets its
# overflow flag and counts as failed rather than parsing a cut answer.
# Basis unknown: value predates measurement; review if a real hook list is refused.
MAX_CODEX_HOOK_PROBE_BYTES = 256 * 1024
_CODEX_PROBE_NOT_COMPLETED = object()


class _CodexProbeStreams(NamedTuple):
    readers: list
    captured: dict
    overflow: threading.Event


def _codex_probe_payload(root: Path) -> bytes:
    requests = (
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "llm-wiki-doctor",
                    "title": "LLM-Wiki Doctor",
                    "version": SCHEMA_VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}},
    )
    return "".join(
        json.dumps(item, separators=(",", ":")) + "\n" for item in requests
    ).encode("utf-8")


def _kill_and_reap(tree: Any) -> None:
    """Kill the peer's whole process tree and reap it, within the cleanup budget.

    The tree, not the direct child: on Windows a venv `python.exe` is a
    trampoline that starts the interpreter as its child, so killing the child
    alone left the peer running (PR #16, three Windows jobs). `ProcessTree`
    owns a Job Object there and a process group on POSIX.
    """
    try:
        tree.terminate(deadline=time.monotonic() + CODEX_HOOK_PROBE_CLEANUP_SECONDS)
    except (OSError, TimeoutError, subprocess.SubprocessError, RuntimeError):
        pass


def _codex_pipes_missing(process: Any) -> bool:
    return (
        process.stdin is None or process.stdout is None or process.stderr is None
    )


def _capture_codex_chunk(name, chunk, process, captured, overflow) -> bool:
    remaining = MAX_CODEX_HOOK_PROBE_BYTES - len(captured[name])
    captured[name].extend(chunk[: max(0, remaining)])
    if len(chunk) > remaining:
        overflow.set()
        process.kill()
        return False
    return True


def _pump_codex_stream(
    name: str,
    stream: Any,
    process: Any,
    captured: dict[str, bytearray],
    overflow: threading.Event,
) -> None:
    while chunk := stream.read1(8192):
        if not _capture_codex_chunk(name, chunk, process, captured, overflow):
            return


def _start_codex_readers(process: Any) -> _CodexProbeStreams:
    """Drain available chunks while the peer waits for the next request."""
    overflow = threading.Event()
    captured = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream: Any) -> None:
        try:
            _pump_codex_stream(name, stream, process, captured, overflow)
        except OSError:
            overflow.set()
            process.kill()

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    return _CodexProbeStreams(readers, captured, overflow)


def _codex_response_line(line: bytes, request_id: int) -> bool:
    response = json.loads(line)
    if not isinstance(response, dict) or response.get("id") != request_id:
        return False
    if "error" in response:
        raise ValueError("Codex probe request failed")
    return "result" in response


def _codex_response_received(streams: _CodexProbeStreams, request_id: int) -> bool:
    complete = bytes(streams.captured["stdout"]).split(b"\n")[:-1]
    return any(_codex_response_line(line, request_id) for line in complete)


def _await_codex_response(streams, request_id: int, probe_deadline: float) -> None:
    while not _codex_response_received(streams, request_id):
        if streams.overflow.is_set() or not _readers_still_running(streams.readers):
            raise ValueError("Codex probe ended before its response")
        if monotonic_deadline.deadline_reached(probe_deadline):
            raise subprocess.TimeoutExpired("Codex hooks probe", 0)
        time.sleep(min(0.01, max(0.0, probe_deadline - time.monotonic())))


def _send_codex_payload(process, payload: bytes) -> None:
    process.stdin.write(payload)
    process.stdin.flush()


def _finish_codex_process(tree: Any, probe_deadline: float) -> bool:
    try:
        tree.process.wait(timeout=max(0.0, probe_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill_and_reap(tree)
        return False
    return True


def _await_codex_process(tree, payload, probe_deadline, streams) -> bool:
    """Initialize, await acknowledgement, then request hooks before closing stdin."""
    first, _, remainder = payload.partition(b"\n")
    process = tree.process
    try:
        _send_codex_payload(process, first + b"\n")
        _await_codex_response(streams, 1, probe_deadline)
        _send_codex_payload(process, remainder)
        _await_codex_response(streams, 2, probe_deadline)
        process.stdin.close()
        return _finish_codex_process(tree, probe_deadline)
    except (OSError, ValueError, subprocess.SubprocessError):
        _kill_and_reap(tree)
        return False


def _readers_still_running(readers: list) -> bool:
    return any(reader.is_alive() for reader in readers)


def _clean_codex_exit(process: Any, streams: _CodexProbeStreams) -> bool:
    return process.returncode == 0 and not streams.overflow.is_set()


def _drained_codex_output(
    tree: Any, streams: _CodexProbeStreams, probe_deadline: float
) -> bytes | object | None:
    for reader in streams.readers:
        reader.join(timeout=max(0.0, probe_deadline - time.monotonic()))
    if _readers_still_running(streams.readers):
        _kill_and_reap(tree)
        return PROBE_INCOMPLETE
    if not _clean_codex_exit(tree.process, streams):
        return None
    return bytes(streams.captured["stdout"])


def run_codex_probe(
    command: list[str], root: Path, home: Path, probe_deadline: float
) -> bytes | object | None:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home / ".codex")
    payload = _codex_probe_payload(root)
    tree = _spawned_codex_tree(command, root, env, probe_deadline)
    if tree is None:
        return PROBE_INCOMPLETE
    try:
        return _probed_codex_tree(tree, payload, probe_deadline)
    except (OSError, PermissionError, subprocess.SubprocessError, ValueError):
        _kill_and_reap(tree)
        return PROBE_INCOMPLETE
    finally:
        _close_codex_tree(tree)


def _close_codex_tree(tree: Any) -> None:
    """Release the tree's handles; a tree still live after its cleanup budget
    is the probe's failure to report, never a reason to raise out of health."""
    try:
        tree.close()
    except (OSError, RuntimeError):
        pass


def _spawned_codex_tree(command: list[str], root: Path, env: dict, probe_deadline: float):
    """The peer as an owned process tree (Job Object / process group), or None."""
    from lsp_process_tree import ProcessTree

    try:
        return ProcessTree.spawn_with_deadline(
            command, cwd=root, env=env, deadline=probe_deadline
        )
    except (OSError, RuntimeError, TimeoutError, ValueError, subprocess.SubprocessError):
        return None


def _probed_codex_tree(tree: Any, payload: bytes, probe_deadline: float) -> bytes | object | None:
    if _codex_pipes_missing(tree.process):
        _kill_and_reap(tree)
        return PROBE_INCOMPLETE
    streams = _start_codex_readers(tree.process)
    if not _await_codex_process(tree, payload, probe_deadline, streams):
        _drained_codex_output(tree, streams, time.monotonic() + 0.2)
        return PROBE_INCOMPLETE
    return _drained_codex_output(tree, streams, probe_deadline)


def _codex_hooks_message(line: str) -> tuple[bool, dict | None]:
    message = json.loads(line)
    if not isinstance(message, dict) or message.get("id") != 2:
        return False, None
    result = message.get("result")
    if isinstance(result, dict):
        return True, result
    return True, None


def codex_hooks_result(raw: bytes) -> dict | None:
    try:
        return _first_codex_hooks_result(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _first_codex_hooks_result(raw: bytes) -> dict | None:
    for line in raw.decode("utf-8").splitlines():
        found, result = _codex_hooks_message(line)
        if found:
            return result
    return None


def _codex_deadline_result(deadline: float) -> object | None:
    if monotonic_deadline.deadline_reached(deadline):
        return _CODEX_PROBE_NOT_COMPLETED
    return None


def _codex_probe_command(available_seconds: float) -> list[str] | None:
    """The probe command, when there is time left to start it at all."""
    if available_seconds < CODEX_HOOK_PROBE_STARTUP_SECONDS:
        return None
    return doctor_schedule.codex_app_server_command()


def probe_codex_hooks_list(
    root: Path, home: Path, *, deadline: float = float("inf")
) -> dict[str, Any] | object | None:
    started_at = time.monotonic()
    probe_deadline = min(deadline, started_at + CODEX_HOOK_PROBE_SECONDS)
    command = _codex_probe_command(probe_deadline - started_at)
    if command is None:
        return None
    raw = run_codex_probe(command, root, home, probe_deadline)
    if raw is PROBE_INCOMPLETE:
        return _codex_deadline_result(deadline)
    return codex_hooks_result(raw) if raw is not None else None


def _template_dicts(values: object) -> list[dict]:
    """A non-empty list of objects, or a template error."""
    if not isinstance(values, list) or not values:
        raise ValueError("invalid Codex hook template")
    if any(not isinstance(value, dict) for value in values):
        raise ValueError("invalid Codex hook template")
    return values


def _template_handlers(groups: object) -> list[tuple[dict, dict]]:
    """Every (group, handler) of one event: an event may hold several groups,
    as `PostToolUse` holds the graph hint on `Bash` and the edit capture
    (docs/research/2026-09-11-codex-leaves-breadcrumbs-too.md)."""
    return [
        (group, handler)
        for group in _template_dicts(groups)
        for handler in _template_dicts(group.get("hooks"))
    ]


def _template_hook_command(handler: dict) -> str:
    command_key = "commandWindows" if os.name == "nt" else "command"
    command = handler.get(command_key)
    if not isinstance(command, str):
        raise ValueError("invalid Codex hook template")
    return command


def _template_hooks_table(template_path: Path) -> dict:
    value, problem = read_bounded_json(
        template_path,
        template_path.parent,
        max_bytes=MAX_CONFIG_BYTES,
    )
    if problem or not isinstance(value, dict):
        raise ValueError("invalid Codex hook template")
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("invalid Codex hook template")
    return hooks


def expected_codex_runtime_hooks(template_path: Path,
                                  provider_bundle: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    expected = []
    table = _template_hooks_table(template_path)
    if provider_bundle is not None:
        table = _hook_config.codex_rendered_template(template_path.parents[2],
            {"hooks": table}, provider_bundle)["hooks"]
    for event_name, groups in table.items():
        if not isinstance(event_name, str):
            raise ValueError("invalid Codex hook template")
        expected.extend(
            {
                "eventName": event_name,
                "matcher": group.get("matcher"),
                "command": _template_hook_command(handler),
            }
            for group, handler in _template_handlers(groups)
        )
    return expected


def _single_probe_entry(data: object) -> dict | None:
    if not isinstance(data, list) or len(data) != 1:
        return None
    if not isinstance(data[0], dict):
        return None
    return data[0]


def _codex_probe_absence(response: object) -> str | None:
    """The code explaining an absent probe response, or None when there is one."""
    if response is _CODEX_PROBE_NOT_COMPLETED:
        return "runtime_hooks_not_completed"
    if response is None:
        return "runtime_hooks_unverified"
    return None


def _codex_probe_entry(response: object) -> tuple[dict | None, str]:
    """The one probe entry, or the code explaining why there is none."""
    absent = _codex_probe_absence(response)
    if absent is not None:
        return None, absent
    assert isinstance(response, dict)
    entry = _single_probe_entry(response.get("data"))
    if entry is None:
        return None, "runtime_hooks_invalid"
    return entry, ""


def _codex_entry_problem(entry: dict, root: Path) -> str:
    """The failure code for this entry, or an empty string when it is usable."""
    try:
        if Path(entry.get("cwd", "")).resolve() != root.resolve():
            return "runtime_hooks_wrong_cwd"
    except (OSError, TypeError, ValueError):
        return "runtime_hooks_invalid"
    if entry.get("warnings") or entry.get("errors"):
        return "runtime_hooks_warning_or_error"
    return ""


def _codex_hook_list(entry: dict) -> list | None:
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or any(
        not isinstance(item, dict) for item in hooks
    ):
        return None
    return hooks


def _codex_owned_hook(hook: dict) -> bool:
    """The installer's own ownership rule, not a second one (#24, C2)."""
    from codex_hook_identity import is_our_codex_command

    return is_our_codex_command(hook.get("command"))


def codex_owned_hooks(hooks: list) -> list:
    return [hook for hook in hooks if _codex_owned_hook(hook)]


def _expected_codex_hooks(root: Path, ours: list,
                          provider_bundle: Mapping[str, str] | None = None) -> tuple[list | None, str]:
    try:
        expected = expected_codex_runtime_hooks(
            root / "integrations" / "codex" / "hooks.json", provider_bundle
        )
    except ValueError:
        return None, "runtime_hooks_template_invalid"
    if len(ours) != len(expected):
        return None, "runtime_hooks_mismatch"
    return expected, ""


def rendered_codex_hook_command(root: Path, command: str) -> str:
    """Recognize one explicit POSIX installation, without executing its shell."""
    root = root.resolve()
    prefix = shlex.join([
        "env", f"LLM_WIKI_ROOT={root}", f"LLM_WIKI_STATE_ROOT={root}",
        str(Path.home() / ".local" / "bin" / "uv"),
    ])
    rendered = command.removeprefix("uv ")
    rendered = rendered.replace('"$LLM_WIKI_ROOT"', shlex.quote(str(root)))
    return f"{prefix} {_rendered_script_paths(rendered, root)}"


def _rendered_script_paths(rendered: str, root: Path) -> str:
    """Each of our scripts as the installer spells it: its quoted absolute path."""
    from codex_hook_identity import OUR_CODEX_SCRIPTS

    for name in OUR_CODEX_SCRIPTS:
        script = shlex.quote(str(root / "scripts" / name))
        rendered = rendered.replace(f'"$LLM_WIKI_ROOT/scripts/{name}"', script)
    return rendered


def _codex_hook_commands(root: Path, command: str) -> set[str]:
    if os.name == "nt" or not command.startswith("uv ") or "exec(__import__" in command:
        return {command}
    return {command, rendered_codex_hook_command(root, command)}


def _canonical_codex_event(event: object) -> object:
    return {"sessionStart": "SessionStart", "preCompact": "PreCompact",
            "postCompact": "PostCompact", "stop": "Stop",
            "postToolUse": "PostToolUse", "subagentStart": "SubagentStart",
            "userPromptSubmit": "UserPromptSubmit"}.get(str(event), event)


def _codex_hook_matches(wanted: dict, hook: dict, root: Path) -> bool:
    if hook.get("command") not in _codex_hook_commands(root, wanted["command"]):
        return False
    comparable = dict(hook, command=wanted["command"],
                      eventName=_canonical_codex_event(hook.get("eventName")))
    return all(comparable.get(field) == value for field, value in wanted.items())


def matching_hooks(wanted: dict, ours: list, root: Path) -> list:
    return [hook for hook in ours if _codex_hook_matches(wanted, hook, root)]


def _codex_hook_trust_code(trust: object) -> str:
    if trust in {"untrusted", "modified"}:
        return f"runtime_hooks_{trust}"
    return "runtime_hooks_trust_unknown"


def codex_hook_problem(wanted: dict, ours: list, root: Path) -> str:
    matches = matching_hooks(wanted, ours, root)
    if len(matches) != 1:
        return "runtime_hooks_mismatch"
    hook = matches[0]
    if hook.get("enabled") is not True:
        return "runtime_hooks_disabled"
    trust = hook.get("trustStatus")
    trusted = trust in {"trusted", "managed"}
    return "" if trusted else _codex_hook_trust_code(trust)


def codex_hooks_verdict(root: Path, ours: list,
                         provider_bundle: Mapping[str, str] | None = None) -> tuple[bool, str]:
    expected, problem = _expected_codex_hooks(root, ours, provider_bundle)
    if expected is None:
        return False, problem
    for wanted in expected:
        problem = codex_hook_problem(wanted, ours, root)
        if problem:
            return False, problem
    return True, "runtime_hooks_active"


def _codex_entry_hooks_verdict(entry: dict, root: Path,
                               provider_bundle: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """The hook verdict for one probe entry that was read successfully."""
    problem = _codex_entry_problem(entry, root)
    if problem:
        return False, problem
    hooks = _codex_hook_list(entry)
    if hooks is None:
        return False, "runtime_hooks_invalid"
    return codex_hooks_verdict(root, codex_owned_hooks(hooks), provider_bundle)


def codex_runtime_hooks_state(
    root: Path, home: Path, *, deadline: float = float("inf"),
    provider_bundle: Mapping[str, str] | None = None
) -> tuple[bool, str]:
    if deadline - time.monotonic() < CODEX_HOOK_PROBE_STARTUP_SECONDS:
        return False, "runtime_hooks_not_completed"
    response = probe_codex_hooks_list(root, home, deadline=deadline)
    entry, problem = _codex_probe_entry(response)
    if entry is None:
        return False, problem
    return _codex_entry_hooks_verdict(entry, root, provider_bundle)


def _codex_wrapper_configured(root: Path, home: Path) -> bool:
    if not (root / "scripts" / "codex-memory-wrapper.ps1").is_file():
        return False
    profiles = (
        home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
        home / ".config" / "powershell" / "Microsoft.PowerShell_profile.ps1",
    )
    return any(
        contains_markers(profile, ("codex-memory-wrapper.ps1", "LLM_WIKI_ROOT"))
        for profile in profiles
    )


def _integration_sources(root: Path) -> dict[str, Path]:
    return {
        "claude": root / "integrations" / "claude-code" / "settings.json",
        "opencode": root / "scripts" / "llm-wiki-memory-opencode.js",
        "codex": root / "integrations" / "codex" / "hooks.json",
    }


def integration_host_configs(
    home: Path,
) -> dict[str, tuple[Path, list[tuple[Path, tuple[str, ...]]]]]:
    return {
        "claude": (
            home / ".claude",
            [
                (
                    home / ".claude" / "settings.json",
                    ("LLM_WIKI_ROOT", "integration_adapter.py"),
                )
            ],
        ),
        "opencode": (
            home / ".config" / "opencode",
            [
                (
                    home / ".config" / "opencode" / "plugins" / "llm-wiki-memory.js",
                    ("session.created", "LLM_WIKI_ROOT"),
                ),
            ],
        ),
        "codex": (
            home / ".codex",
            [
                (
                    home / ".codex" / "config.toml",
                    ("mcp_servers.llm-wiki", "mcp_server.py"),
                )
            ],
        ),
    }


# Codex runs a hook only after a person approves it in Codex (`/hooks`); nothing an
# installer writes stands in for that approval. Said once, in the words of the step,
# instead of a reason code (2026-09-28, docs/research/2026-09-28-a-check-names-its-cause.md).
_CODEX_TRUST_STEP = (
    "Codex has not been told to trust the LLM-Wiki hooks: open Codex, run /hooks, and "
    "trust the LLM-Wiki commands. Codex asks a person for this; the installer cannot."
)
_CODEX_TRUST_REASONS = frozenset({"runtime_hooks_untrusted", "runtime_hooks_modified"})


def _codex_trust_message(reason: str, message: str) -> str:
    if reason not in _CODEX_TRUST_REASONS:
        return message
    return f"{_CODEX_TRUST_STEP} {message}"


def codex_degraded_result(root: Path, home: Path, reason: str) -> dict[str, object]:
    wrapper = _codex_wrapper_configured(root, home)
    message = "Official Codex hooks are not verified and no capture fallback is configured."
    capture_mode = "none"
    if wrapper:
        message = "Official Codex hooks are not verified; wrapper fallback is heartbeat-only."
        capture_mode = "wrapper-fallback-heartbeat-only"
    result: dict[str, object] = {
        "status": "degraded",
        "message": _codex_trust_message(reason, message),
        "reason": reason,
        "capture_mode": capture_mode,
    }
    if reason == "runtime_hooks_not_completed":
        result["not_completed"] = True
    return result


def codex_host_result(root: Path, home: Path, deadline: float,
                       state_root: Path | None = None) -> dict[str, object]:
    from install_state import (
        InstallControlError,
    )
    from llm_wiki.install_provider_proof import installed_codex_provider_bundle

    if not (home / ".codex").exists():
        return {"status": "skipped", "message": "Optional host not installed."}
    state = state_root or runtime_roots.state_root_for(root)
    try:
        bundle = installed_codex_provider_bundle(root, state, home)
    except (InstallControlError, OSError, ValueError):
        return _codex_unqualified_host_result(root, home, deadline)
    return _codex_qualified_host_result(root, home, deadline, bundle)


def _codex_unqualified_host_result(root: Path, home: Path, deadline: float) -> dict[str, object]:
    active, reason = codex_runtime_hooks_state(root, home, deadline=deadline)
    result = _codex_legacy_hook_result(root, home, active, reason)
    result.update(provider_bundle="unknown", provider_transport={"status": "degraded",
                  "reason": "runtime_provider_bundle_unverified"})
    return result


def _codex_legacy_hook_result(root: Path, home: Path, active: bool, reason: str) -> dict[str, object]:
    if active:
        return {"status": "degraded", "reason": "runtime_provider_bundle_unverified",
                "message": "Legacy native hooks are trusted; installed provider choice is unverified.",
                "capture_mode": "official-hooks", "native_hook_status": "ok"}
    return dict(codex_degraded_result(root, home, reason), native_hook_status="degraded")


def _codex_qualified_host_result(root: Path, home: Path, deadline: float,
                                 bundle: dict[str, str]) -> dict[str, object]:
    hooks_active, reason = codex_runtime_hooks_state(root, home, deadline=deadline,
                                                    provider_bundle=bundle)
    if not hooks_active:
        return codex_degraded_result(root, home, reason)
    problem = _codex_mcp_provider_problem(root, home, bundle)
    return {"status": "ok", "message": "Official Codex hooks are active and trusted; review changes in /hooks.",
            "capture_mode": "official-hooks", "trust": "review-with-/hooks",
            "provider_bundle": "verified-installed-desired",
            "provider_transport": _codex_mcp_transport_result(problem)}


def _codex_mcp_transport_result(problem: str) -> dict[str, str]:
    if problem:
        return {"status": "degraded", "reason": problem}
    return {"status": "ok", "mcp_entry": "equivalent"}


def _codex_mcp_provider_problem(root: Path, home: Path, bundle: dict[str, str]) -> str:
    from codex_config_state import (
        codex_mcp_config_state,
    )

    state = codex_mcp_config_state(home / ".codex/config.toml", root, bundle)
    return "" if state == "equivalent" else f"runtime_mcp_provider_{state}"


# Delegates deleted on 2026-09-17 that an older install's hooks still name; the
# adapter tolerates them, the installer replaces them (audit C-1,
# docs/research/2026-09-25-doctor-names-hooks-older-than-the-release.md).
RETIRED_HOOK_DELEGATES = ("precompact_capture.py", "session_end_capture.py")


def _names_a_retired_delegate(configs: list[tuple[Path, tuple[str, ...]]]) -> bool:
    return any(
        contains_markers(path, (name,)) for path, _markers in configs for name in RETIRED_HOOK_DELEGATES
    )


def generic_host_result(
    host_dir: Path, configs: list[tuple[Path, tuple[str, ...]]]
) -> dict[str, object]:
    if not host_dir.exists():
        return {"status": "skipped", "message": "Optional host not installed."}
    if not any(contains_markers(path, markers) for path, markers in configs):
        return {"status": "degraded", "message": "Host detected without LLM-Wiki config."}
    return _configured_host_result(configs)


def _configured_host_result(configs: list[tuple[Path, tuple[str, ...]]]) -> dict[str, object]:
    if _names_a_retired_delegate(configs):
        return {
            "status": "degraded",
            "message": "Installed hooks predate this release; rerun the installer to refresh them.",
        }
    return {"status": "ok", "message": "User integration config detected."}


def _required_host_config(
    config: tuple[Path, list[tuple[Path, tuple[str, ...]]]] | None,
) -> tuple[Path, list[tuple[Path, tuple[str, ...]]]]:
    if config is None:
        raise ValueError("missing integration host configuration")
    return config


def _integration_host_result(
    root: Path,
    home: Path,
    name: str,
    config: tuple[Path, list[tuple[Path, tuple[str, ...]]]] | None,
    deadline: float,
    state_root: Path | None = None,
) -> dict[str, object]:
    if name == "codex":
        return codex_host_result(root, home, deadline, state_root)
    return generic_host_result(*_required_host_config(config))


def _integration_hosts(root: Path, home: Path, deadline: float,
                       state_root: Path | None = None) -> dict[str, dict[str, object]]:
    configs = integration_host_configs(home)
    names = ("claude", "opencode", "codex")
    return {
        name: _integration_host_result(root, home, name, configs.get(name), deadline, state_root)
        for name in names
    }


def integration_summary(
    source_details: Mapping[str, bool], hosts: Mapping[str, Mapping[str, object]]
) -> tuple[str, str]:
    missing_sources = sum(not available for available in source_details.values())
    if missing_sources:
        return "error", f"{missing_sources} integration source adapter(s) are missing."
    configured_missing = sum(_integration_host_degraded(host) for host in hosts.values())
    if configured_missing:
        return "degraded", f"{configured_missing} installed host(s) lack current integration config."
    return "ok", "Integration sources are available; optional hosts were checked."


def _integration_host_degraded(host: Mapping[str, object]) -> bool:
    provider = host.get("provider_transport", {})
    return host.get("status") == "degraded" or provider.get("status") == "degraded"


def integration_check(root: Path, home: Path, *, deadline: float = float("inf"),
                       state_root: Path | None = None) -> dict:
    source_details = {name: path.is_file() for name, path in _integration_sources(root).items()}
    hosts = _integration_hosts(root, home, deadline, state_root)
    status, message = integration_summary(source_details, hosts)
    return generation_maintenance.result("integrations", status, message, {"sources": source_details, "hosts": hosts})


def repair_runtime(state_root: Path, repaired: list[dict]) -> None:
    for relative in RUNTIME_DIRECTORIES:
        path = state_root / relative
        kind, _ = generation_maintenance.safe_kind(path, state_root)
        if kind not in {"missing", "directory"}:
            raise OSError(f"unsafe runtime path: {relative}")
        if kind == "missing":
            path.mkdir(parents=True, exist_ok=True)
            repaired.append({"action": "create_runtime_directory", "directory": relative})


DEFERRED_BY_ACTION = {
    "runtime": {"runtime"},
    "transactions": {"transactions"},
    "queue": {"queue"},
    "indexes": {"claims"},
    "archives": {"archives"},
    "generations": {"generation"},
}


class RepairContext(NamedTuple):
    """Everything a repair step is allowed to read or record."""

    root_path: Path
    state_path: Path
    generated_at: datetime
    deadline: float
    rebuild_generation: bool
    selected_repairs: set[str]
    repaired: list[dict]
    repair_errors: dict[str, list[str]]
    repair_deferred: set[str]
