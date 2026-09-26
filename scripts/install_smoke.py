#!/usr/bin/env python3
"""Bounded production-profile smoke test for a local LLM-Wiki install."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from sync_memory import _run_process_tree

# Default wall-clock budget for one installer smoke run; distinct from the corpus collector's.
DEFAULT_DEADLINE_SECONDS = 120.0
MAX_CHILD_BYTES = 4 * 1024 * 1024
MAX_ERROR_BYTES = 512
EXPECTED_TOOL_NAMES = (
    "recall",
    "read_page",
    "wiki_overview",
    "vault_status",
    "get_decisions",
    "get_context",
    "check_contradiction",
    "log_decision",
    "compile",
    "find_dead_code",
    "get_architecture",
    "doctor",
)


# The doctor checks whose `error` means the install itself is broken. Any other
# `error` is the vault's history (a failed night, a dead task), which a reinstall
# must not be blocked by (audit 2026-09-26 A-10,
# docs/research/2026-09-26-a-smoke-checks-the-install-not-the-vault.md).
INSTALL_OWNED_CHECKS = frozenset({"environment", "filesystem", "adoption", "mcp", "integrations"})
_EXPECTED_EXIT = {"ok": 0, "degraded": 1, "error": 2}


class SmokeFailure(RuntimeError):
    """A smoke failure whose message is ours, so the installer may print it.

    The installer printed only the exception type, and a rerun stopped at
    "install smoke failed: RuntimeError" with nothing to act on (audit B-31,
    docs/research/2026-09-25-a-failed-reinstall-puts-the-old-one-back.md).
    """


def _production_imports() -> dict[str, bool]:
    """Import the packages required by the installed production profile."""
    importlib.import_module("mcp")
    mcp_server = importlib.import_module("mcp_server")
    if not bool(getattr(mcp_server, "MCP_AVAILABLE", False)):
        raise SmokeFailure("MCP server capability is unavailable")
    result = {"mcp": True, "mcp_server": True}
    if sys.version_info < (3, 11):
        importlib.import_module("tomli")
        result["tomli"] = True
    return result


def _has_required_shape(value: dict, required: dict[str, type]) -> bool:
    return all(
        key in value and isinstance(value[key], expected) for key, expected in required.items()
    )


def _validate_doctor_report(value: object) -> dict[str, object]:
    required = {
        "schema_version": str,
        "generated_at": str,
        "overall_status": str,
        "repaired": list,
        "checks": list,
        "counts": dict,
        "run_deletion": dict,
    }
    if not isinstance(value, dict) or not _has_required_shape(value, required):
        raise SmokeFailure("Doctor report does not satisfy the install smoke schema")
    status = value["overall_status"]
    if status not in {"ok", "degraded", "error"}:
        raise SmokeFailure("Doctor report does not satisfy the install smoke schema")
    return value


def _doctor_report(root: Path, state_root: Path, timeout: float) -> dict[str, object]:
    environment = os.environ.copy()
    environment.update(
        LLM_WIKI_ROOT=str(root),
        LLM_WIKI_STATE_ROOT=str(state_root),
    )
    command = [sys.executable, str(root / "scripts" / "doctor.py"), "--json"]
    completed = _run_process_tree(
        command,
        cwd=root,
        env=environment,
        timeout=max(0.001, timeout),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return _checked_doctor_report(completed)


def _checked_doctor_report(completed: subprocess.CompletedProcess) -> dict[str, object]:
    """The report, read before the exit code: an `error` exits 2 and must be named."""
    report = _validate_doctor_report(_parsed_doctor_output(completed.stdout))
    if _install_is_broken(report):
        raise SmokeFailure(_doctor_error_message(report))
    if completed.returncode != _EXPECTED_EXIT[report["overall_status"]]:
        raise SmokeFailure("Doctor failed during install smoke")
    return report


def _failing_checks(report: dict) -> list[str]:
    return [str(check.get("id")) for check in report.get("checks", []) if isinstance(check, dict) and check.get("status") == "error"]


def _install_is_broken(report: dict) -> bool:
    """An `error` in a check the installer owns, or an `error` that names no check."""
    if report["overall_status"] != "error":
        return False
    failing = _failing_checks(report)
    return not failing or bool(INSTALL_OWNED_CHECKS.intersection(failing))


def _doctor_error_message(report: dict) -> str:
    failing = _failing_checks(report)
    return (
        "Doctor reported error in: "
        + (", ".join(failing) or "unknown")
        + "; run `uv run python scripts/doctor.py` for the reasons"
    )


def _parsed_doctor_output(stdout: str) -> object:
    encoded = stdout.encode("utf-8", errors="replace")
    if len(encoded) > MAX_CHILD_BYTES:
        raise SmokeFailure("Doctor output exceeded the install smoke bound")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure("Doctor did not return valid JSON") from exc


async def _mcp_tools(root: Path, state_root: Path, timeout: float) -> tuple[str, ...]:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    environment = os.environ.copy()
    environment.update(
        LLM_WIKI_ROOT=str(root),
        LLM_WIKI_STATE_ROOT=str(state_root),
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "scripts" / "mcp_server.py")],
        cwd=str(root),
        env=environment,
    )
    with anyio.fail_after(max(0.001, timeout)):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return tuple(tool.name for tool in result.tools)


def _mcp_tool_names(root: Path, state_root: Path, timeout: float) -> tuple[str, ...]:
    import anyio

    return anyio.run(_mcp_tools, root, state_root, timeout)


def validate_tool_contract(tools: tuple[str, ...]) -> None:
    if len(tools) != len(EXPECTED_TOOL_NAMES) or set(tools) != set(EXPECTED_TOOL_NAMES):
        raise SmokeFailure("MCP smoke returned an unexpected tool contract")


def _require_deadline(deadline_seconds: object) -> float:
    if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)):
        raise ValueError("deadline_seconds must be a positive finite number")
    if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be a positive finite number")
    return float(deadline_seconds)


def _resolved_roots(root: Path, state_root: Path) -> tuple[Path, Path]:
    root = Path(root).resolve(strict=True)
    state_root = Path(state_root).resolve(strict=True)
    if not root.is_dir() or not state_root.is_dir():
        raise ValueError("install smoke roots must be directories")
    return root, state_root


def _remaining_before(deadline: float, stage: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"install smoke deadline expired before {stage}")
    return remaining


def _smoke_status(doctor: dict[str, object]) -> str:
    """`degraded` for any finding the install leaves to the vault, `ok` otherwise."""
    if doctor["overall_status"] == "ok":
        return "ok"
    return "degraded"


def run_smoke(
    root: Path,
    state_root: Path,
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> dict[str, object]:
    """Run imports, Doctor, and MCP under one absolute deadline."""
    seconds = _require_deadline(deadline_seconds)
    root, state_root = _resolved_roots(root, state_root)
    deadline = time.monotonic() + seconds

    imports = _production_imports()
    doctor = _doctor_report(root, state_root, _remaining_before(deadline, "Doctor"))
    names = _mcp_tool_names(root, state_root, _remaining_before(deadline, "MCP"))
    validate_tool_contract(names)
    return {
        "status": _smoke_status(doctor),
        "imports": imports,
        "doctor": doctor,
        "tool_count": len(names),
        "tools": list(names),
    }


def _error_text(error: BaseException) -> str:
    """Our own message in full; anything else by its type, since its text is not ours."""
    if isinstance(error, SmokeFailure):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


def _bounded_error(error: BaseException) -> str:
    message = f"install smoke failed: {_error_text(error)}\n"
    encoded = message.encode("utf-8")[:MAX_ERROR_BYTES]
    return encoded.decode("utf-8", errors="ignore")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path(os.environ.get("LLM_WIKI_STATE_ROOT", default_root)),
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
    )
    args = parser.parse_args(argv)
    try:
        report = run_smoke(
            args.root,
            args.state_root,
            deadline_seconds=args.deadline_seconds,
        )
    except BaseException as error:
        sys.stderr.write(_bounded_error(error))
        return 1
    sys.stderr.write(_vault_findings_note(report.get("doctor", {})))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


def _vault_findings_note(doctor: dict) -> str:
    """The vault errors the install left in place, named so the operator sees them."""
    failing = _failing_checks(doctor)
    if not failing:
        return ""
    return f"install smoke: the vault reports errors in: {', '.join(failing)}; run `uv run python scripts/doctor.py`\n"


if __name__ == "__main__":
    raise SystemExit(main())
