"""Resources, tool registration and the stdio server."""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time

from llm_wiki import (
    mcp_server_limits,
    mcp_server_navigation,
    mcp_server_quality,
    mcp_server_tools,
    mcp_server_validation,
    mcp_server_vault,
    mcp_server_warmup,
    mcp_server_workers,
)
from llm_wiki.mcp_server_limits import (
    _OPERATION_DEADLINE,
    CONTEXT_RESOURCE_URI,
    HEALTH_RESOURCE_URI,
)
from llm_wiki.mcp_server_quality import (
    _resource_quality,
)
from llm_wiki.mcp_server_sanitize import (
    _is_rendered_navigation_data,
    _safe_exception_text,
)
from llm_wiki.mcp_server_tools import (
    _bounded_tool_call,
)
from llm_wiki.mcp_server_workers import (
    _busy_envelope_text,
    _call_with_deadline,
    _WorkerCapacityExhausted,
    bounded_operation_deadline,
    check_deadline,
    rendered_envelope,
    timeout_envelope_text,
)


def build_resource_definitions() -> list:
    """Build stable application resources when the installed SDK supports them."""
    if not mcp_server_limits.MCP_RESOURCES_AVAILABLE:
        return []
    return [
        mcp_server_limits.Resource(
            name="llm-wiki-health",
            uri=HEALTH_RESOURCE_URI,
            description="Local vault compile and index health.",
            mimeType="application/json",
        ),
        mcp_server_limits.Resource(
            name="llm-wiki-context",
            uri=CONTEXT_RESOURCE_URI,
            description="Local vault overview and current context status.",
            mimeType="application/json",
        ),
    ]


def _resource_data(uri: str, deadline: float) -> dict:
    if uri == HEALTH_RESOURCE_URI:
        return _call_with_deadline(mcp_server_vault.vault_status, deadline=deadline)
    if uri == CONTEXT_RESOURCE_URI:
        return {
            "overview": _call_with_deadline(mcp_server_vault.wiki_overview, deadline=deadline),
            "status": _call_with_deadline(mcp_server_vault.vault_status, deadline=deadline),
        }
    return {"error": f"Unknown resource: {uri}"}


def handle_resource_read(uri: str, deadline: float | None = None) -> str:
    """Return one resource as a JSON text envelope."""

    operation_deadline = bounded_operation_deadline(deadline)
    deadline_token = _OPERATION_DEADLINE.set(operation_deadline)
    try:
        data = _resource_data(uri, operation_deadline)
    except Exception as error:
        data = {"error": _safe_exception_text(error, f"mcp.resource:{uri}")}
    try:
        envelope = mcp_server_quality.build_operation_envelope(data, _resource_quality(data))
        return rendered_envelope(envelope)
    finally:
        _OPERATION_DEADLINE.reset(deadline_token)


def register_resources(server) -> bool:
    """Register resources only when both SDK types and methods are available."""
    if not mcp_server_limits.MCP_RESOURCES_AVAILABLE:
        return False
    list_resources_method = getattr(server, "list_resources", None)
    read_resource_method = getattr(server, "read_resource", None)
    if not callable(list_resources_method) or not callable(read_resource_method):
        return False
    resources = build_resource_definitions()

    @list_resources_method()
    async def list_resources():
        return resources

    @read_resource_method()
    async def read_resource(uri):
        operation_deadline = time.monotonic() + mcp_server_limits.MCP_OPERATION_SECONDS
        try:
            text = await mcp_server_workers.run_bounded(
                handle_resource_read,
                str(uri),
                operation_deadline,
                deadline=operation_deadline,
            )
        except _WorkerCapacityExhausted:
            text = _busy_envelope_text()
        except TimeoutError:
            text = timeout_envelope_text()
        return [mcp_server_limits.ReadResourceContents(content=text, mime_type="application/json")]

    return True


def format_tool_result(result: str):
    """Return text-only output or text plus structured content by capability."""

    text_content = [mcp_server_limits.TextContent(type="text", text=result)]
    structured = json.loads(result)
    if mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE:
        return mcp_server_limits.CallToolResult(
            content=text_content,
            structuredContent=structured,
            isError=_result_is_error(structured.get("data")),
        )
    if mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE:
        return text_content, structured
    return text_content


def _repair_failed(data: dict) -> bool:
    return any(
        isinstance(check, dict)
        and bool(check.get("details", {}).get("repair_errors"))
        for check in data.get("checks", [])
    )


def _plain_result_is_error(data: dict) -> bool:
    if "error" in data or _repair_failed(data):
        return True
    return data.get("status") == "error" or data.get("overall_status") == "error"


def _result_is_error(data) -> bool:
    if not isinstance(data, dict):
        return False
    if _is_rendered_navigation_data("get_architecture", data):
        return data.get("status") in {"error", "timeout"}
    return _plain_result_is_error(data)


def _execute_formatted_tool_call(name: str, arguments, operation_deadline: float):
    result = mcp_server_tools.execute_tool_call(name, arguments, operation_deadline)
    check_deadline(operation_deadline)
    return format_tool_result(result)


def register_tools(server, tools):
    """Register tool callbacks while disabling SDK-side validation when supported."""
    @server.list_tools()
    async def list_tools():
        return tools

    call_tool_method = server.call_tool
    try:
        parameters = inspect.signature(call_tool_method).parameters.values()
        supports_validate_input = any(
            parameter.name == "validate_input"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_validate_input = False
    decorator = (
        call_tool_method(validate_input=False)
        if supports_validate_input
        else call_tool_method()
    )
    @decorator
    async def call_tool(name: str, arguments):
        return await _bounded_tool_call(
            name, arguments, _execute_formatted_tool_call, format_tool_result
        )

    return call_tool


def build_server():
    """Build the one MCP server every transport serves.

    OPS-01 added a second transport. Both call this, so the HTTP path reaches
    tools through the same `register_tools` callback as stdio - the same
    `validate_tool_arguments`, the same `execute_tool_call` deadline, the
    same envelope. A transport that built its own dispatch would be a second
    validation boundary, which is the defect `4494d8c` already cost us once.
    """
    server = mcp_server_limits.Server("llm-wiki")
    register_resources(server)
    register_tools(server, mcp_server_validation.build_tool_definitions())
    return server


def run_server() -> int:
    """Start the MCP server (stdio transport). Returns exit code."""
    if not mcp_server_limits.MCP_AVAILABLE:
        print(
            "MCP package not installed. Run: uv sync --locked --inexact",
            file=sys.stderr,
        )
        return 1

    server = build_server()
    mcp_server_warmup.start_encoder_warmup()

    async def main():
        async with mcp_server_limits.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    exit_code = 0
    try:
        asyncio.run(main())
    finally:
        try:
            mcp_server_navigation.close_navigation_session_manager(
                time.monotonic() + mcp_server_limits.MCP_OPERATION_SECONDS
            )
        except BaseException as error:
            print(
                f"navigation session manager close failed: {type(error).__name__}",
                file=sys.stderr,
            )
            exit_code = 1
        settle_inference()
    return exit_code


# How long a closing server waits for model inference to reach a safe point. Basis
# unknown: value predates measurement; a warm question costs about 1.3 s and a cold
# one about 12 s (WARMUP_LIMIT_SECONDS above), so 30 s outlasts one of each.
SHUTDOWN_INFERENCE_SECONDS = 30.0


def settle_inference() -> None:
    """No model may be mid-inference when the interpreter finalizes; say if one is."""
    import inference_threads

    left = inference_threads.settle(SHUTDOWN_INFERENCE_SECONDS)
    if left:
        print(f"inference still running at exit: {', '.join(left)}", file=sys.stderr)
