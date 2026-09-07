#!/usr/bin/env python3
"""Call one tool on an MCP server over stdio, and time it honestly.

`codebase-memory-mcp` has a `cli --json <tool> <args>` mode, which is why the
code stand could call it as a subprocess. trace-mcp and Serena do not: they
speak MCP over stdin and stdout and nothing else. This is the smallest client
that gets an answer out of them — initialize, `tools/call`, read, shut down —
so a competitor can be measured without writing a harness per competitor.

What it measures is what the stand measures for every side: the wall time of
the call and the text the tool handed back, because that text is what an agent
pays for. Server startup is inside that time, and so is startup for the sides
that already had a CLI. Neither side is amortized; the note in
`run_code_parity` says the same.

It is deliberately not a general MCP client. No sampling, no resources, no
notifications, no reconnection — one call and out. A benchmark that needs more
than that is measuring the harness.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "llm-wiki-code-parity", "version": "1"}
READ_LIMIT_BYTES = 8 * 1024 * 1024


class McpCallFailed(RuntimeError):
    """The server never produced a usable result for this call."""


def _frame(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def _request(identifier: int, method: str, params: dict[str, Any]) -> bytes:
    return _frame(
        {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
    )


def _notification(method: str) -> bytes:
    return _frame({"jsonrpc": "2.0", "method": method, "params": {}})


def _decoded(line: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _reply_to(stream, identifier: int) -> dict[str, Any]:
    """The first message answering this id; anything else on the way is noise.

    A server may interleave notifications and log lines with its replies, and
    several of them print banners before the protocol starts. Skipping what we
    did not ask for is the difference between a client that works against one
    server and one that works against three.
    """
    read = 0
    while read < READ_LIMIT_BYTES:
        line = stream.readline()
        if not line:
            raise McpCallFailed("server closed the connection before replying")
        read += len(line)
        message = _decoded(line)
        if message is not None and message.get("id") == identifier:
            return message
    raise McpCallFailed("server produced more preamble than the read limit allows")


def _block_text(block: object) -> str:
    if not isinstance(block, dict) or block.get("type") != "text":
        return ""
    return str(block.get("text", ""))


def _text_of(result: dict[str, Any]) -> str:
    """Everything the tool said, joined — the same bytes an agent would read."""
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return json.dumps(result, ensure_ascii=False)
    said = [_block_text(block) for block in blocks]
    return "\n".join(part for part in said if part)


def _require_result(message: dict[str, Any]) -> dict[str, Any]:
    error = message.get("error")
    if error is not None:
        raise McpCallFailed(str(error)[:400])
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpCallFailed("reply carried no result object")
    return result


def _handshake(process: subprocess.Popen) -> None:
    process.stdin.write(
        _request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
    )
    process.stdin.flush()
    _require_result(_reply_to(process.stdout, 1))
    process.stdin.write(_notification("notifications/initialized"))
    process.stdin.flush()


def _tool_result(process: subprocess.Popen, tool: str, arguments: dict) -> dict:
    process.stdin.write(
        _request(2, "tools/call", {"name": tool, "arguments": arguments})
    )
    process.stdin.flush()
    return _require_result(_reply_to(process.stdout, 2))


def _listed_tools(process: subprocess.Popen) -> dict:
    process.stdin.write(_request(2, "tools/list", {}))
    process.stdin.flush()
    return _require_result(_reply_to(process.stdout, 2))


def _stopped(process: subprocess.Popen) -> None:
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def call_tool(
    command: list[str], tool: str, arguments: dict, *, timeout: float, cwd: str | None = None
) -> dict[str, object]:
    """One tool call against a freshly started server, with its cost.

    Returns the same shape the other sides of the stand return: a status, the
    seconds it took, and the text the tool produced.
    """
    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - the command is stand configuration
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
    )
    try:
        return _completed(process, tool, arguments, started, timeout)
    finally:
        _stopped(process)


def _completed(
    process: subprocess.Popen,
    tool: str,
    arguments: dict,
    started: float,
    timeout: float,
) -> dict[str, object]:
    try:
        _handshake(process)
        result = _tool_result(process, tool, arguments)
    except McpCallFailed as failure:
        return {
            "status": "error",
            "seconds": round(time.monotonic() - started, 3),
            "text": f"error: {failure}",
        }
    status = "tool_error" if result.get("isError") else "answered"
    return {
        "status": status,
        "seconds": round(time.monotonic() - started, 3),
        "text": _text_of(result)[: 512 * 1024],
    }


def list_tools(command: list[str], *, cwd: str | None = None) -> list[dict]:
    """What this server offers, so a call list is written from fact not guess."""
    process = subprocess.Popen(  # noqa: S603 - the command is stand configuration
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
    )
    try:
        _handshake(process)
        listed = _listed_tools(process).get("tools")
    finally:
        _stopped(process)
    return listed if isinstance(listed, list) else []


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True, help="JSON array: the server argv")
    parser.add_argument("--tool", default="", help="omit with --list")
    parser.add_argument("--list", action="store_true", help="print the tool names")
    parser.add_argument("--arguments", default="{}", help="JSON object")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--cwd", default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.list:
        for tool in list_tools(json.loads(args.command), cwd=args.cwd):
            print(f"{tool.get('name')}: {str(tool.get('description') or '')[:110]}")
        return 0
    outcome = call_tool(
        json.loads(args.command),
        args.tool,
        json.loads(args.arguments),
        timeout=args.timeout,
        cwd=args.cwd,
    )
    print(json.dumps(outcome, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
