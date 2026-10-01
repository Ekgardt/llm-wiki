"""The health child leaves time to return an honest report to its caller."""

import json
import subprocess

import mcp_server


def test_doctor_reserves_return_time(tmp_path, monkeypatch):
    seen = []
    report = {"overall_status": "degraded"}

    def run(command, **kwargs):
        seen.append((float(command[-1]), kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 1, json.dumps(report), "")

    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    assert mcp_server._run_doctor_process(root=tmp_path, state_root=tmp_path, deadline=116.0) == report
    assert seen == [(115.0, 16.0)]


def test_doctor_status_uses_its_measured_host_budget(monkeypatch):
    import settings

    monkeypatch.setattr(settings, "setting_value", lambda name: {"mcp.doctor_seconds": 16}[name])
    assert mcp_server._tool_operation_seconds("doctor", {"action": "status"}) == 16
    assert mcp_server._tool_operation_seconds("doctor", {"action": "queue-redrive"}) == mcp_server.MCP_OPERATION_SECONDS
