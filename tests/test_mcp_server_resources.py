"""MCP resources, SDK callback shapes and the stdio server lifecycle."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bounded_io
import pytest
from llm_wiki import mcp_server_limits

from tests.mcp_server_support import (
    ENVELOPE_FIELDS,
    DrainAwareSet,
    _assert_resource_timeout,
)
from tests.slow_machine import SHORT_TIMEOUT


class TestResources:
    def test_resource_inventory_remains_exactly_two(self, monkeypatch):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_RESOURCES_AVAILABLE", True)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.Resource", Model)

        resources = mcp_server.build_resource_definitions()

        assert len(resources) == 2
        assert {resource.uri for resource in resources} == {
            mcp_server.HEALTH_RESOURCE_URI,
            mcp_server.CONTEXT_RESOURCE_URI,
        }

    def test_resource_definitions_degrade_when_sdk_support_is_unavailable(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_RESOURCES_AVAILABLE", False)

        assert mcp_server.build_resource_definitions() == []

    def test_health_and_context_resource_definitions_when_supported(self):
        import mcp_server

        resources = mcp_server.build_resource_definitions()
        if mcp_server.MCP_RESOURCES_AVAILABLE:
            assert {str(resource.uri) for resource in resources} == {
                "llm-wiki://health",
                "llm-wiki://context",
            }
        else:
            assert resources == []

    def test_health_and_context_handlers_return_enveloped_json(self):
        from mcp_server import handle_resource_read

        for uri in ("llm-wiki://health", "llm-wiki://context"):
            envelope = json.loads(handle_resource_read(uri))
            assert set(envelope) == ENVELOPE_FIELDS
            assert isinstance(envelope["data"], dict)

    def test_unknown_health_status_degrades_resource_quality(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda: {
                "last_compile": "never",
                "last_compile_status": "unknown",
                "compile_backlog": 0,
            },
        )

        envelope = json.loads(mcp_server.handle_resource_read("llm-wiki://health"))

        assert envelope["partial"] is True
        assert envelope["coverage"] < 1
        assert envelope["confidence"] < 1
        assert envelope["warnings"]

    def test_compile_backlog_degrades_health_resource(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda: {
                "last_compile": datetime.now(UTC).isoformat(),
                "last_compile_status": "ok",
                "compile_backlog": 2,
            },
        )

        envelope = json.loads(mcp_server.handle_resource_read("llm-wiki://health"))

        assert envelope["partial"] is True
        assert any("backlog" in warning.lower() for warning in envelope["warnings"])

    def test_resource_text_is_strict_json_for_non_finite_data(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda: {
                "last_compile": "never",
                "last_compile_status": "unknown",
                "compile_backlog": float("inf"),
            },
        )

        text = mcp_server.handle_resource_read("llm-wiki://health")

        assert "Infinity" not in text
        assert json.loads(text)["data"]["compile_backlog"] is None

    def test_stale_legacy_index_does_not_degrade_context_resource_quality(
        self, monkeypatch, tmp_path
    ):
        import mcp_contract
        import mcp_server

        index = tmp_path / "cache" / "index.sqlite"
        index.parent.mkdir()
        index.touch()
        old = datetime.now(UTC) - timedelta(days=2)
        os.utime(index, (old.timestamp(), old.timestamp()))
        real_build = mcp_contract.build_envelope
        monkeypatch.setattr(
            "llm_wiki.mcp_server_quality.build_envelope",
            lambda data, **kwargs: real_build(data, root=tmp_path, **kwargs),
        )
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda: {
                "last_compile": datetime.now(UTC).isoformat(),
                "last_compile_status": "ok",
                "compile_backlog": 0,
            },
        )
        monkeypatch.setattr("llm_wiki.mcp_server_vault.wiki_overview", lambda: {"page_count": 1})

        envelope = json.loads(mcp_server.handle_resource_read("llm-wiki://context"))

        assert envelope["freshness"] == "unknown"
        assert envelope["partial"] is False
        assert envelope["coverage"] > 0.6

    def test_unknown_resource_returns_enveloped_error(self):
        from mcp_server import handle_resource_read

        envelope = json.loads(handle_resource_read("llm-wiki://unknown"))

        assert set(envelope) == ENVELOPE_FIELDS
        assert "error" in envelope["data"]

    def test_resource_exception_text_is_safe_and_bounded(self, monkeypatch):
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\prompt.txt"
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda: (_ for _ in ()).throw(RuntimeError(sensitive * 100)),
        )

        envelope = json.loads(mcp_server.handle_resource_read("llm-wiki://health"))

        assert sensitive not in json.dumps(envelope)
        assert len(envelope["data"]["error"]) <= 256

    def test_resource_registration_degrades_without_sdk_support(self, monkeypatch):
        import mcp_server

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_RESOURCES_AVAILABLE", False)
        assert mcp_server.register_resources(object()) is False

    def test_resource_registration_exposes_working_callbacks(self, monkeypatch):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callbacks = {}

            def list_resources(self):
                return lambda function: self.callbacks.setdefault("list", function) or function

            def read_resource(self):
                return lambda function: self.callbacks.setdefault("read", function) or function

        server = FakeServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_RESOURCES_AVAILABLE", True)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.Resource", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.ReadResourceContents", Model)

        assert mcp_server.register_resources(server) is True
        resources = asyncio.run(server.callbacks["list"]())
        contents = asyncio.run(server.callbacks["read"]("llm-wiki://health"))

        assert {resource.uri for resource in resources} == {
            "llm-wiki://health",
            "llm-wiki://context",
        }
        assert json.loads(contents[0].content)["data"]["last_compile_status"]

    @pytest.mark.parametrize(
        "uri", ["llm-wiki://health", "llm-wiki://context"]
    )
    def test_registered_resources_share_one_deadline_and_run_off_loop(
        self, monkeypatch, uri
    ):
        import mcp_server

        seen = []
        release = threading.Event()
        finished = threading.Event()
        drained = threading.Event()
        workers = DrainAwareSet(drained)

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callbacks = {}

            def list_resources(self):
                return lambda function: self.callbacks.setdefault("list", function) or function

            def read_resource(self):
                return lambda function: self.callbacks.setdefault("read", function) or function

        async def exercise(callback):
            loop = asyncio.get_running_loop()
            started = asyncio.Event()

            def blocked(*, deadline):
                seen.append(deadline)
                loop.call_soon_threadsafe(started.set)
                try:
                    release.wait(60.0)
                finally:
                    finished.set()
                return {"last_compile": "never", "last_compile_status": "unknown"}

            monkeypatch.setattr("llm_wiki.mcp_server_vault.vault_status", blocked)
            task = asyncio.create_task(callback(uri))
            try:
                await asyncio.wait_for(started.wait(), timeout=0.5)
                await asyncio.sleep(0.01)
                loop_progressed = not finished.is_set()
                return loop_progressed, await task
            finally:
                release.set()
                assert finished.wait(SHORT_TIMEOUT)
                assert drained.wait(SHORT_TIMEOUT)
                with mcp_server_limits.MCP_WORKERS_LOCK:
                    assert not workers

        server = FakeServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_RESOURCES_AVAILABLE", True)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.Resource", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.ReadResourceContents", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 3.0)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS", workers)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_WORKERS_LOCK", threading.Lock())
        monkeypatch.setattr("llm_wiki.mcp_server_vault.wiki_overview", lambda **kwargs: {"ok": True})
        assert mcp_server.register_resources(server) is True
        loop_progressed, contents = asyncio.run(exercise(server.callbacks["read"]))

        _assert_resource_timeout(
            json.loads(contents[0].content), loop_progressed, seen
        )

        status = {"last_compile": "fresh", "last_compile_status": "ok"}
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.vault_status",
            lambda *, deadline: status,
        )
        sentinel_contents = asyncio.run(server.callbacks["read"](uri))
        sentinel = json.loads(sentinel_contents[0].content)
        expected = status
        if uri == mcp_server.CONTEXT_RESOURCE_URI:
            expected = {"overview": {"ok": True}, "status": status}
        assert sentinel["data"] == expected
        with mcp_server_limits.MCP_WORKERS_LOCK:
            assert not workers


class TestCallbackCompatibility:
    def test_registered_callback_formats_large_result_off_event_loop(self, monkeypatch):
        import threading

        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callback = None

            def list_tools(self):
                return lambda callback: callback

            def call_tool(self, **kwargs):
                return lambda callback: setattr(self, "callback", callback) or callback

        server = FakeServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
        mcp_server.register_tools(server, [])
        payload = mcp_server.timeout_envelope_text().replace(
            '"operation_timeout"', json.dumps("x" * (bounded_io.MAX_KNOWLEDGE_PAGE_BYTES - 1024)), 1
        )
        real_format = mcp_server.format_tool_result
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_format(text):
            started.set()
            try:
                release.wait(60.0)
                return real_format(text)
            finally:
                finished.set()

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 1.0)
        monkeypatch.setattr("llm_wiki.mcp_server_tools.execute_tool_call", lambda *args: payload)
        monkeypatch.setattr("llm_wiki.mcp_server_runtime.format_tool_result", slow_format)

        async def exercise():
            task = asyncio.create_task(server.callback("read_page", {"slug": "page"}))
            await asyncio.sleep(0.02)
            heartbeat = started.is_set() and not finished.is_set()
            release.set()
            return heartbeat, await task

        heartbeat, result = asyncio.run(exercise())

        assert heartbeat is True
        assert len(result[0].text) >= bounded_io.MAX_KNOWLEDGE_PAGE_BYTES - 2048

    def test_registered_callback_answers_while_the_payload_formatter_is_stuck(
        self, monkeypatch
    ):
        """The timeout answer never waits for the formatting of the late payload."""
        import threading

        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callback = None

            def list_tools(self):
                return lambda callback: callback

            def call_tool(self, **kwargs):
                return lambda callback: setattr(self, "callback", callback) or callback

        server = FakeServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", False)
        mcp_server.register_tools(server, [])
        release = threading.Event()
        finished = threading.Event()
        real_format = mcp_server.format_tool_result
        payload = json.dumps({"schema_version": "1.0", "data": {"page": "late"}})

        def blocked_format(text):
            if text != payload:
                return real_format(text)
            release.wait(SHORT_TIMEOUT)
            finished.set()
            return real_format(text)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_OPERATION_SECONDS", 0.001)
        monkeypatch.setattr("llm_wiki.mcp_server_tools.execute_tool_call", lambda *args: payload)
        monkeypatch.setattr("llm_wiki.mcp_server_runtime.format_tool_result", blocked_format)
        try:
            result = asyncio.run(server.callback("read_page", {"slug": "page"}))
            answered_first = not finished.is_set()
        finally:
            release.set()

        assert (answered_first, json.loads(result[0].text)["data"]) == (
            True,
            {"error": "operation_timeout"},
        )

    @pytest.mark.parametrize(
        ("arguments", "report", "expected_error"),
        [
            (
                {"action": "transaction-recover", "repair": True},
                {
                    "overall_status": "error",
                    "checks": [
                        {
                            "id": "index",
                            "status": "error",
                            "message": "repair failed",
                            "details": {"repair_errors": ["Index repair failed"]},
                        }
                    ],
                },
                True,
            ),
            (
                {"action": "status"},
                {
                    "overall_status": "error",
                    "checks": [
                        {
                            "id": "environment",
                            "status": "error",
                            "message": "missing",
                            "details": {},
                        }
                    ],
                },
                True,
            ),
        ],
    )
    def test_registered_doctor_callback_signals_only_repair_failures(
        self, monkeypatch, arguments, report, expected_error
    ):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.callback = None

            def list_tools(self):
                return lambda callback: callback

            def call_tool(self, **kwargs):
                def register(callback):
                    self.callback = callback
                    return callback

                return register

        server = FakeServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.CallToolResult", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        monkeypatch.setattr("llm_wiki.mcp_server_doctor.doctor", lambda **kwargs: report)
        mcp_server.register_tools(server, [])

        result = asyncio.run(server.callback("doctor", arguments))

        assert result.isError is expected_error

    def test_call_tool_result_marks_protocol_errors(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeCallToolResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.CallToolResult", FakeCallToolResult)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        text = json.dumps({"schema_version": "1.0", "data": {"error": "bad"}})

        result = mcp_server.format_tool_result(text)

        assert isinstance(result, FakeCallToolResult)
        assert result.content[0].text == text
        assert result.structuredContent == json.loads(text)
        assert result.isError is True

    def test_call_tool_result_marks_operator_overall_error(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeCallToolResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.CallToolResult", FakeCallToolResult)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        text = json.dumps(
            {"data": {"overall_status": "error", "codes": ["operation_failed"]}}
        )

        assert mcp_server.format_tool_result(text).isError is True

    def test_call_tool_result_marks_success_as_non_error(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeCallToolResult:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.CallToolResult", FakeCallToolResult)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        text = json.dumps({"schema_version": "1.0", "data": {"ok": True}})

        result = mcp_server.format_tool_result(text)

        assert result.isError is False

    @pytest.mark.parametrize(
        ("status", "expected_error"),
        [
            ("error", True),
            ("timeout", True),
            ("unsupported", False),
            ("not_ready", False),
            ("partial", False),
            ("stale", False),
            ("ok", False),
        ],
    )
    def test_call_tool_result_uses_precise_navigation_failure_semantics(
        self,
        monkeypatch,
        status,
        expected_error,
    ):
        import mcp_server

        class Model:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.CallToolResult", Model)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", True)
        data = mcp_server.normalized_navigation_failure(
            directory=None,
            mode="definition",
            status=status,
            warning="navigation_warning",
            offset=0,
            limit=10,
        )

        result = mcp_server.format_tool_result(json.dumps({"data": data}))

        assert result.isError is expected_error

    def test_modern_callback_returns_text_and_structured_envelope(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", True)
        text = json.dumps({"schema_version": "1.0", "data": {"ok": True}})

        content, structured = mcp_server.format_tool_result(text)

        assert content[0].text == text
        assert structured == json.loads(text)

    def test_legacy_callback_returns_text_only(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", False)

        content = mcp_server.format_tool_result('{"data": {}}')

        assert content[0].text == '{"data": {}}'

    def test_registered_modern_callback_disables_sdk_validation_and_envelopes_input(
        self, monkeypatch
    ):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeServer:
            def __init__(self):
                self.validate_input = None
                self.callback = None
                self.tools_callback = None

            def list_tools(self):
                def register(callback):
                    self.tools_callback = callback
                    return callback

                return register

            def call_tool(self, *, validate_input=True):
                self.validate_input = validate_input

                def register(callback):
                    self.callback = callback
                    return callback

                return register

        server = FakeServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", False)

        mcp_server.register_tools(server, [])
        content = asyncio.run(server.callback("recall", {}))
        envelope = json.loads(content[0].text)

        assert server.validate_input is False
        assert "required" in envelope["data"]["error"].lower()
        assert envelope["partial"] is True

    def test_registered_legacy_callback_uses_parameterless_decorator(self, monkeypatch):
        import mcp_server

        class FakeTextContent:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class LegacyServer:
            def __init__(self):
                self.callback = None

            def list_tools(self):
                return lambda callback: callback

            def call_tool(self):
                def register(callback):
                    self.callback = callback
                    return callback

                return register

        server = LegacyServer()
        monkeypatch.setattr("llm_wiki.mcp_server_limits.TextContent", FakeTextContent)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_CALL_TOOL_RESULT_AVAILABLE", False)
        monkeypatch.setattr("llm_wiki.mcp_server_limits.MCP_STRUCTURED_OUTPUT_AVAILABLE", False)

        mcp_server.register_tools(server, [])
        content = asyncio.run(server.callback("read_page", {}))
        envelope = json.loads(content[0].text)

        assert "required" in envelope["data"]["error"].lower()


class TestRunServer:
    """Test server startup."""

    def test_run_server_without_mcp_package(self):
        """run_server should return 1 if mcp not installed."""
        from mcp_server import MCP_AVAILABLE, run_server

        if not MCP_AVAILABLE:
            result = run_server()
            assert result == 1

    def test_core_sdk_remains_available_without_optional_resource_types(self, tmp_path):
        package = tmp_path / "mcp"
        server_package = package / "server"
        server_package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (server_package / "__init__.py").write_text(
            "class Server:\n    pass\n", encoding="utf-8"
        )
        (server_package / "stdio.py").write_text(
            "def stdio_server():\n    return None\n", encoding="utf-8"
        )
        (package / "types.py").write_text(
            "class TextContent:\n    pass\n"
            "class Tool:\n    model_fields = {}\n",
            encoding="utf-8",
        )
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = (
            "import sys; "
            f"sys.path[:0] = [{str(tmp_path)!r}, {str(scripts)!r}]; "
            "import mcp_server; "
            "print(mcp_server.MCP_AVAILABLE, "
            "mcp_server.MCP_STRUCTURED_OUTPUT_AVAILABLE, "
            "mcp_server.MCP_RESOURCES_AVAILABLE)"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "True False False"

    def test_fully_absent_sdk_degrades_without_import_failure(self):
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        code = (
            "import sys; "
            "sys.modules['mcp'] = None; "
            f"sys.path.insert(0, {str(scripts)!r}); "
            "import mcp_server; "
            "print(mcp_server.MCP_AVAILABLE, "
            "mcp_server.MCP_RESOURCES_AVAILABLE, "
            "mcp_server.build_tool_definitions())"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.strip() == "False False []"
