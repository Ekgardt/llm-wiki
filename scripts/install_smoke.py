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


def _production_imports() -> dict[str, bool]:
    """Import the packages required by the installed production profile."""
    importlib.import_module("mcp")
    mcp_server = importlib.import_module("mcp_server")
    if not bool(getattr(mcp_server, "MCP_AVAILABLE", False)):
        raise RuntimeError("MCP server capability is unavailable")
    result = {"mcp": True, "mcp_server": True}
    if sys.version_info < (3, 11):
        importlib.import_module("tomli")
        result["tomli"] = True
    return result


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
    if not isinstance(value, dict) or any(
        key not in value or not isinstance(value[key], expected)
        for key, expected in required.items()
    ):
        raise RuntimeError("Doctor report does not satisfy the install smoke schema")
    status = value["overall_status"]
    if status not in {"ok", "degraded", "error"}:
        raise RuntimeError("Doctor report does not satisfy the install smoke schema")
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
    if completed.returncode not in {0, 1}:
        raise RuntimeError("Doctor failed during install smoke")
    encoded = completed.stdout.encode("utf-8", errors="replace")
    if len(encoded) > MAX_CHILD_BYTES:
        raise RuntimeError("Doctor output exceeded the install smoke bound")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Doctor did not return valid JSON") from exc
    report = _validate_doctor_report(parsed)
    status = report["overall_status"]
    expected_returncode = {"ok": 0, "degraded": 1, "error": 2}[status]
    if status == "error" or completed.returncode != expected_returncode:
        raise RuntimeError("Doctor failed during install smoke")
    return report


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
        raise RuntimeError("MCP smoke returned an unexpected tool contract")


def run_smoke(
    root: Path,
    state_root: Path,
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> dict[str, object]:
    """Run imports, Doctor, and MCP under one absolute deadline."""
    if (
        isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or not math.isfinite(deadline_seconds)
        or deadline_seconds <= 0
    ):
        raise ValueError("deadline_seconds must be a positive finite number")
    root = Path(root).resolve(strict=True)
    state_root = Path(state_root).resolve(strict=True)
    if not root.is_dir() or not state_root.is_dir():
        raise ValueError("install smoke roots must be directories")
    deadline = time.monotonic() + float(deadline_seconds)

    imports = _production_imports()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("install smoke deadline expired before Doctor")
    doctor = _doctor_report(root, state_root, remaining)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("install smoke deadline expired before MCP")
    names = _mcp_tool_names(root, state_root, remaining)
    validate_tool_contract(names)
    return {
        "status": "degraded" if doctor["overall_status"] == "degraded" else "ok",
        "imports": imports,
        "doctor": doctor,
        "tool_count": len(names),
        "tools": list(names),
    }


def _bounded_error(error: BaseException) -> str:
    message = f"install smoke failed: {type(error).__name__}\n"
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
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
