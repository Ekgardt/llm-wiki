"""Host integrations and Codex hooks as the doctor sees them: configs, runtime hooks, the bounded hooks probe and markers."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.doctor_case_helpers import (
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _build_root,
    _check,
    _codex_hooks_fixture,
)

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def _runtime_hooks(root: Path, *, trust: str = "trusted", enabled: bool = True,
                   provider_bundle: dict | None = None) -> dict:
    template = _codex_hooks_fixture()["hooks"]
    if provider_bundle is not None:
        from integration_hook_config import codex_rendered_template

        template = codex_rendered_template(root, _codex_hooks_fixture(), provider_bundle)["hooks"]
    hooks = []
    for event_name, groups in template.items():
        group = groups[0]
        handler = group["hooks"][0]
        hooks.append(
            {
                "eventName": event_name,
                "matcher": group.get("matcher"),
                "command": handler["commandWindows"] if os.name == "nt" else handler["command"],
                "enabled": enabled,
                "trustStatus": trust,
                "source": "user",
                "currentHash": "never-report-this-hash",
            }
        )
    return {"data": [{"cwd": str(root), "hooks": hooks, "warnings": [], "errors": []}]}


def _installed_codex_fixture(root: Path, state_root: Path, home: Path) -> dict:
    from integration_hook_config import codex_hooks_resource
    from llm_wiki.install_v2_transactions import install_resources

    from tests.install_control_helpers import _release

    bundle = {"MEMORY_LLM_PROVIDER": "codex", "MEMORY_CODEX_MODEL": "gpt-6-luna",
              "MEMORY_CODEX_REASONING": "max"}
    resource = codex_hooks_resource(home / ".codex/hooks.json", _codex_hooks_fixture(),
                                     root=root, provider_bundle=bundle)
    install_resources(state_root=state_root, vault_root=root, release=_release(),
                      scheduler_backend="cron", resources=[resource], control_version=2)
    return bundle


def test_integrations_use_injected_home_and_skip_absent_optional_hosts(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        "LLM_WIKI_ROOT integration_adapter.py", encoding="utf-8"
    )

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["status"] == "ok"
    assert check["details"]["hosts"]["claude"]["status"] == "ok"
    assert check["details"]["hosts"]["opencode"]["status"] == "skipped"
    assert str(home) not in json.dumps(check)


def test_codex_mcp_config_alone_does_not_claim_capture(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["python", "scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    codex = check["details"]["hosts"]["codex"]
    assert codex["status"] == "degraded"
    assert codex["capture_mode"] == "none"


def test_codex_quoted_mcp_table_is_accepted(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers."llm-wiki"]\ncommand = "uv"\n'
        'args = ["run", "python", "scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"]["codex"]["status"] == "degraded"


def test_codex_doctor_prefers_trusted_runtime_hooks(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )
    (codex_dir / "hooks.json").write_bytes(
        (root / "integrations" / "codex" / "hooks.json").read_bytes()
    )
    bundle = _installed_codex_fixture(root, state_root, home)

    monkeypatch.setattr(
        doctor_integrations, "probe_codex_hooks_list", lambda *_args, **_kwargs: _runtime_hooks(root, provider_bundle=bundle)
    )
    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")
    codex = check["details"]["hosts"]["codex"]

    assert codex["status"] == "ok"
    assert codex["capture_mode"] == "official-hooks"
    assert codex["trust"] == "review-with-/hooks"
    assert "currentHash" not in json.dumps(codex)


def test_codex_runtime_hook_health_is_decoupled_from_mcp_config(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    bundle = _installed_codex_fixture(root, state_root, home)
    monkeypatch.setattr(
        doctor_integrations, "probe_codex_hooks_list", lambda *_args, **_kwargs: _runtime_hooks(root, provider_bundle=bundle)
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"]["codex"]["status"] == "ok"
    assert check["details"]["hosts"]["codex"]["provider_transport"]["status"] == "degraded"
    assert check["status"] == "degraded"


@pytest.mark.parametrize("trust", ["untrusted", "modified"])
def test_codex_runtime_untrusted_or_modified_is_degraded_without_capture(
    tmp_path, monkeypatch, trust
):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    bundle = _installed_codex_fixture(root, state_root, home)
    monkeypatch.setattr(
        doctor_integrations,
        "probe_codex_hooks_list",
        lambda *_args, **_kwargs: _runtime_hooks(root, trust=trust, provider_bundle=bundle),
    )

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["capture_mode"] == "none"
    assert codex["reason"] == f"runtime_hooks_{trust}"


def test_codex_runtime_probe_warnings_are_degraded(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    bundle = _installed_codex_fixture(root, state_root, home)
    response = _runtime_hooks(root, provider_bundle=bundle)
    response["data"][0]["warnings"] = ["configuration warning"]
    monkeypatch.setattr(doctor_integrations, "probe_codex_hooks_list", lambda *_args, **_kwargs: response)

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["reason"] == "runtime_hooks_warning_or_error"
    assert "configuration warning" not in json.dumps(codex)


def test_codex_unavailable_probe_reports_unverified_and_no_capture(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    _installed_codex_fixture(root, state_root, home)
    monkeypatch.setattr(doctor_integrations, "probe_codex_hooks_list", lambda *_args, **_kwargs: None)

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["reason"] == "runtime_hooks_unverified"
    assert codex["capture_mode"] == "none"


def test_codex_configured_wrapper_is_reported_as_heartbeat_fallback(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    (root / "scripts" / "codex-memory-wrapper.ps1").write_text(
        "function codex {}\n", encoding="utf-8"
    )
    (home / ".codex").mkdir()
    profile = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '. "$env:LLM_WIKI_ROOT\\scripts\\codex-memory-wrapper.ps1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor_integrations, "probe_codex_hooks_list", lambda *_args, **_kwargs: None)

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["capture_mode"] == "wrapper-fallback-heartbeat-only"


def test_codex_hooks_probe_skips_spawn_when_deadline_budget_is_too_small(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
        doctor_schedule,
    )

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    _installed_codex_fixture(root, state_root, home)
    monkeypatch.setattr(
        doctor_schedule,
        "codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )
    monkeypatch.setattr(
        doctor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("insufficient budget spawned Codex"),
    )

    deadline = time.monotonic() + doctor_integrations.CODEX_HOOK_PROBE_STARTUP_SECONDS / 2
    response = doctor_integrations.probe_codex_hooks_list(root, home, deadline=deadline)
    check = doctor_integrations.integration_check(root, home, deadline=deadline, state_root=state_root)

    assert response is None
    codex = check["details"]["hosts"]["codex"]
    assert codex["reason"] == "runtime_hooks_not_completed"
    assert codex["not_completed"] is True


def _fake_codex_tree(monkeypatch, process_factory, observed=None):
    """The probe spawns its peer as an owned process tree; a test stands in
    for the tree, not for `Popen`, so the pipes and the kill go where the
    product sends them (a Job Object on Windows, a process group on POSIX)."""
    import lsp_process_tree

    class Tree:
        def __init__(self, process):
            self.process = process

        def terminate(self, *, deadline):
            # Like the real tree: kill, then wait for the exit until the deadline.
            self.process.kill()
            try:
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass

        def close(self):
            return None

    def spawn(command, *, cwd, env, deadline):
        del deadline
        kwargs = {"cwd": str(cwd), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        if observed is not None:
            observed["args"] = list(command)
            observed["kwargs"] = kwargs
        return Tree(process_factory(list(command), **kwargs))

    monkeypatch.setattr(lsp_process_tree.ProcessTree, "spawn_with_deadline", staticmethod(spawn))


def test_codex_hooks_probe_cleanup_honors_absolute_deadline(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
        doctor_schedule,
    )

    root, _, home = _build_root(tmp_path)
    clock = [100.0]
    waits = []

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def wait(self, timeout=None):
            waits.append(timeout)
            clock[0] += timeout
            raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            return None

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    _fake_codex_tree(monkeypatch, Process)
    monkeypatch.setattr(
        doctor_schedule,
        "codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )

    active, reason = doctor_integrations.codex_runtime_hooks_state(root, home, deadline=100.5)

    assert active is False
    assert reason == "runtime_hooks_not_completed"
    assert waits[0] == pytest.approx(0.5)
    assert sum(waits) <= 0.500001


def test_codex_hooks_probe_own_timeout_remains_unverified(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
        doctor_schedule,
    )

    root, _, home = _build_root(tmp_path)
    clock = [100.0]

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def wait(self, timeout=None):
            clock[0] += timeout
            raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            return None

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    _fake_codex_tree(monkeypatch, Process)
    monkeypatch.setattr(
        doctor_schedule,
        "codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )

    active, reason = doctor_integrations.codex_runtime_hooks_state(root, home, deadline=105.0)

    assert active is False
    assert reason == "runtime_hooks_unverified"


def test_codex_hooks_probe_is_bounded_and_uses_exact_cwd(tmp_path, monkeypatch):
    from llm_wiki import (
        doctor_integrations,
        doctor_schedule,
    )

    root, _, home = _build_root(tmp_path)
    observed = {}

    class Input(io.BytesIO):
        def close(self):
            pass

    class Process:
        def __init__(self, args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            self.returncode = 0
            response = {
                "id": 2,
                "result": _runtime_hooks(root),
            }
            self.stdin = Input()
            self.stdout = io.BytesIO((' {"id":1,"result":{}}\n' + json.dumps(response) + "\n").encode())
            self.stderr = io.BytesIO()

        def wait(self, timeout=None):
            observed["timeout"] = timeout
            observed["input"] = self.stdin.getvalue().decode()
            return self.returncode

        def kill(self):
            pytest.fail("bounded probe unexpectedly timed out")

    _fake_codex_tree(monkeypatch, Process, observed)
    monkeypatch.setattr(
        doctor_schedule,
        "codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )

    response = doctor_integrations.probe_codex_hooks_list(
        root,
        home,
        deadline=time.monotonic() + doctor_integrations.CODEX_HOOK_PROBE_SECONDS,
    )

    _assert_codex_probe_identity(observed, root, home)
    _assert_codex_probe_limits(observed)
    _assert_codex_probe_requests(observed, root)
    assert response == _runtime_hooks(root)


def _assert_codex_probe_identity(observed, root, home):
    assert observed["args"] == ["codex", "app-server", "--listen", "stdio://"]
    assert observed["kwargs"]["cwd"] == str(root)
    assert observed["kwargs"]["env"]["CODEX_HOME"] == str(home / ".codex")


def _assert_codex_probe_limits(observed):
    from llm_wiki import (
        doctor_integrations,
    )

    assert observed["kwargs"]["stdout"] is subprocess.PIPE
    assert observed["kwargs"]["stderr"] is subprocess.PIPE
    assert observed["timeout"] <= doctor_integrations.CODEX_HOOK_PROBE_SECONDS


def _assert_codex_probe_requests(observed, root):
    requests = list(map(json.loads, observed["input"].splitlines()))
    assert requests[0]["method"] == "initialize"
    assert requests[1]["method"] == "initialized"
    assert requests[2] == {"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}}
    assert "bypass" not in observed["input"].casefold()


def test_codex_app_server_command_supports_windows_cmd_shim(monkeypatch):
    from llm_wiki import doctor_schedule

    shim = r"C:\Users\Test User\AppData\Roaming\npm\codex.cmd"
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: shim if name == "codex.cmd" else None,
    )
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")

    command = doctor_schedule.codex_app_server_command(platform="nt")

    assert command[:4] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"]
    assert "codex.cmd" in command[4]
    assert "app-server" in command[4]


def test_codex_app_server_command_uses_direct_executable_on_unix(monkeypatch):
    from llm_wiki import doctor_schedule

    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )

    assert doctor_schedule.codex_app_server_command(platform="posix") == [
        "/usr/local/bin/codex",
        "app-server",
        "--listen",
        "stdio://",
    ]


def test_codex_doctor_rejects_runtime_disabled_hooks(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[features]\nhooks = false\n\n[mcp_servers.llm-wiki]\n"
        'command = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )
    hooks = json.loads((root / "integrations" / "codex" / "hooks.json").read_text(encoding="utf-8"))
    hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 999
    (codex_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    bundle = _installed_codex_fixture(root, state_root, home)

    monkeypatch.setattr(
        doctor_integrations,
        "probe_codex_hooks_list",
        lambda *_args, **_kwargs: _runtime_hooks(root, enabled=False, provider_bundle=bundle),
    )
    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")
    codex = check["details"]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["reason"] == "runtime_hooks_disabled"
    assert codex["capture_mode"] == "none"


@pytest.mark.parametrize(
    "content",
    [
        '# [mcp_servers.llm-wiki]\n# command = "uv"\n# args = ["scripts/mcp_server.py"]\n',
        'description = "mcp_servers.llm-wiki command uv scripts/mcp_server.py"\n',
        '[other]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\nenabled = false\n',
        '[mcp_servers."llm-wiki"\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
    ],
    ids=["commented", "unrelated-string", "unrelated-table", "disabled", "malformed"],
)
def test_codex_config_requires_active_valid_toml_table(tmp_path, content):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(content, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"]["codex"]["status"] == "degraded"


def test_codex_hook_health_does_not_use_local_toml_parser(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_integrations,
    )

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )
    bundle = _installed_codex_fixture(root, state_root, home)
    monkeypatch.setattr(
        doctor_integrations, "probe_codex_hooks_list", lambda *_args, **_kwargs: _runtime_hooks(root, provider_bundle=bundle)
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")
    codex = check["details"]["hosts"]["codex"]

    assert codex["status"] == "ok"


@pytest.mark.parametrize(
    ("host", "relative", "marker"),
    [
        ("claude", ".claude/settings.json", "LLM_WIKI_ROOT integration_adapter.py"),
        (
            "opencode",
            ".config/opencode/plugins/llm-wiki-memory.js",
            "session.created LLM_WIKI_ROOT",
        ),
        (
            "codex",
            ".codex/config.toml",
            '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]',
        ),
    ],
)
def test_installed_integration_requires_expected_marker(tmp_path, host, relative, marker):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    expected = "degraded" if host == "codex" else "ok"
    assert check["details"]["hosts"][host]["status"] == expected


def test_the_pyright_advice_names_what_would_change_the_outcome() -> None:
    """Reinstalling cannot fix a repository that declares no Pyright settings."""
    from llm_wiki import doctor_lsp

    assert "pyproject.toml" in doctor_lsp.pyright_recommended_action(
        ["pyright_repository_config_ancestor_search"]
    )
    assert doctor_lsp.pyright_recommended_action(["pyright_missing"]) == (
        "uv run python scripts/install_pyright.py --state-root <state-root>"
    )
    assert doctor_lsp.pyright_recommended_action([]) == (
        "uv run python scripts/install_pyright.py --state-root <state-root>"
    )
