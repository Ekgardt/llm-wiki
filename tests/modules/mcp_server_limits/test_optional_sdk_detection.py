"""Each optional MCP SDK capability is detected on its own import, not on a sibling's."""
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

LIMITS = Path(__file__).resolve().parents[3] / 'scripts' / 'llm_wiki' / 'mcp_server_limits.py'


def _fresh_limits(name: str):
    spec = importlib.util.spec_from_file_location(name, LIMITS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_installed_sdk_offers_every_capability():
    limits = _fresh_limits('limits_with_the_whole_sdk')
    assert limits.MCP_AVAILABLE is True
    assert limits.MCP_CALL_TOOL_RESULT_AVAILABLE is True
    assert limits.MCP_RESOURCES_AVAILABLE is True


def test_a_missing_resource_helper_disables_only_resources(monkeypatch):
    # An SDK whose server imports without the resource helper: load the server first,
    # so the blocked helper is the only missing name whatever ran before.
    importlib.import_module('mcp.server')
    importlib.import_module('mcp.types')
    monkeypatch.setitem(sys.modules, 'mcp.server.lowlevel.helper_types', None)
    limits = _fresh_limits('limits_without_resource_helpers')
    assert limits.MCP_AVAILABLE is True
    assert limits.MCP_CALL_TOOL_RESULT_AVAILABLE is True
    assert limits.MCP_RESOURCES_AVAILABLE is False


@pytest.mark.parametrize('blocked', ['mcp.server', 'mcp.types'])
def test_a_missing_core_sdk_reports_no_mcp(monkeypatch, blocked):
    monkeypatch.setitem(sys.modules, blocked, None)
    limits = _fresh_limits(f'limits_without_{blocked.replace(".", "_")}')
    assert limits.MCP_AVAILABLE is False
    assert limits.MCP_CALL_TOOL_RESULT_AVAILABLE is False
