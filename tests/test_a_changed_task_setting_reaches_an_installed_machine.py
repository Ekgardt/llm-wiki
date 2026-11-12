"""A Windows machine registered before the time-limit fix is re-registered by an update.

The limit was no part of the task specification or of the script's "equivalent", so
one-hour tasks compared equal to the corrected contract and were never rewritten. Research:
`docs/research/2026-09-17-a-changed-task-setting-reaches-an-installed-machine.md`.
"""
from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path

from tests.a_changed_task_setting_reaches_an_installed_machine_helpers import ROOT

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import install_state
from llm_wiki import install_v2_transactions, install_windows  # noqa: E402
from reliable_memory import canonical_json_bytes  # noqa: E402

from tests.a_changed_task_setting_reaches_an_installed_machine_helpers import (
    SCRIPT,
    STUBS,
    _run,
)
from tests.powershell_literal import ps_literal  # noqa: E402

RELEASE = {
    "commit_oid": "a" * 40,
    "project_version": "0.0.0",
    "source_mode": "pinned_remote",
    "uv_lock_sha256": "b" * 64,
    "worktree_clean": True,
}
LEGACY_TASKS = [
    {"at": "03:00", "kind": "nightly", "name": "LLMWiki-Nightly"},
    {"at": "04:00", "day": "Sunday", "kind": "weekly", "name": "LLMWiki-Weekly"},
]


def _legacy_spec(root: Path, state_root: Path, uv_path: Path) -> bytes:
    """What the release before 2026-09-17 recorded: no version, no limits."""
    return canonical_json_bytes(
        {
            "root": str(Path(root).resolve()),
            "state_root": str(Path(state_root).resolve()),
            "tasks": LEGACY_TASKS,
            "uv_path": str(Path(uv_path).resolve()),
        }
    )


class _Machine:
    """Task Scheduler as the script reports it: tasks answer to one version only."""

    def __init__(self) -> None:
        self.registered: int | None = None
        self.calls: list[tuple[str, int]] = []

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        version = 1
        if "-SpecVersion" in command:
            version = int(command[command.index("-SpecVersion") + 1])
        handlers = {"-StateJson": self._state, "-Uninstall": self._uninstall}
        mode = next((flag for flag in handlers if flag in command), "register")
        self.calls.append((mode, version))
        return handlers.get(mode, self._register)(version)

    def _state(self, version: int) -> tuple[int, bytes]:
        names = {None: "absent", version: "equivalent"}
        return 0, json.dumps({"state": names.get(self.registered, "conflict")}).encode()

    def _uninstall(self, version: int) -> tuple[int, bytes]:
        if self.registered != version:
            return 1, b""
        self.registered = None
        return 0, b""

    def _register(self, version: int) -> tuple[int, bytes]:
        if self.registered is not None:
            return 1, b""
        self.registered = version
        return 0, b""


def _resource(tmp_path: Path, machine: _Machine) -> install_state.ManagedResource:
    return install_windows.windows_task_scheduler_resource(
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / "uv.exe",
        script_path=tmp_path / "install-scheduled-tasks.ps1",
        powershell="pwsh.exe",
        runner=machine,
    )


def _install(tmp_path: Path, resource: install_state.ManagedResource) -> None:
    install_v2_transactions.install_resources(
        state_root=tmp_path / "state",
        vault_root=tmp_path / "vault",
        release=RELEASE,
        scheduler_backend="task_scheduler",
        resources=[resource],
        control_version=2,
    )


def test_an_update_registers_the_tasks_again_under_the_new_contract(tmp_path, monkeypatch) -> None:
    (tmp_path / "state").mkdir()
    machine = _Machine()
    with monkeypatch.context() as earlier_release:
        earlier_release.setattr(install_windows, "render_windows_task_spec", _legacy_spec)
        _install(tmp_path, _resource(tmp_path, machine))
    before = machine.registered
    machine.calls.clear()

    _install(tmp_path, _resource(tmp_path, machine))

    writes = [call for call in machine.calls if call[0] != "-StateJson"]
    assert (before, machine.registered, writes) == (1, 2, [("-Uninstall", 1), ("register", 2)])


def test_the_specification_names_the_limits_the_script_registers() -> None:
    from tests.the_scheduler_outlasts_the_pass_helpers import script_limit_hours

    script = SCRIPT.read_text(encoding="utf-8")
    registered = re.findall(r"-ExecutionTimeLimit \(New-TimeSpan -Hours \$LimitHours\.(\w+)\)", script)
    checked = re.findall(r"LimitHours = \$limits\.(\w+)", script)

    assert (registered, checked, script_limit_hours()) == (
        ["nightly", "weekly"],
        ["nightly", "weekly"],
        install_windows.WINDOWS_TASK_LIMIT_HOURS,
    )


def test_the_two_contracts_never_both_claim_the_same_task() -> None:
    command = textwrap.dedent(
        f"""
        $tokens = $null; $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Test-LLMWikiTaskSpec' }}, $true)
        Invoke-Expression $fn.Extent.Text
        $tasks = @(
            @('old text', 'PT1H'), @('old text', 'PT3H'),
            @('new [llm-wiki-task-spec:2]', 'PT3H'), @('new [llm-wiki-task-spec:2]', 'PT180M'),
            @('new [llm-wiki-task-spec:2]', 'PT1H'), @('new [llm-wiki-task-spec:2]', 'soon')
        ) | ForEach-Object {{ [pscustomobject]@{{
            Description = $_[0]
            Settings = [pscustomobject]@{{ ExecutionTimeLimit = $_[1] }} }} }}
        $answers = foreach ($version in 1, 2) {{
            ,@($tasks | ForEach-Object {{
                [bool](Test-LLMWikiTaskSpec -Task $_ -SpecVersion $version -LimitHours 3) }})
        }}
        ConvertTo-Json -Compress $answers
        """
    )

    result = _run(command)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [
        [True, True, False, False, False, False],
        [False, False, True, True, False, False],
    ]


def _specified_limit_hours() -> list[float]:
    """The hour limits the product promises, nightly then weekly.

    Read from `install_control.WINDOWS_TASK_LIMIT_HOURS` rather than repeated
    here. The pair moved from 3/5 to 4/6 when the passes gained their bounds,
    and a test that spells the numbers out again only records the day it was
    written. `test_the_specification_names_the_limits_the_script_registers`
    holds the script to the same constant.
    """
    return [float(hours) for hours in install_windows.WINDOWS_TASK_LIMIT_HOURS.values()]


def test_a_registration_carries_the_marker_and_the_limits(tmp_path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run-scheduled-task.ps1").write_text("", encoding="utf-8")
    (tmp_path / "uv.exe").write_text("", encoding="utf-8")
    command = STUBS + textwrap.dedent(
        f"""
        . {ps_literal(str(SCRIPT))} -VaultRoot {ps_literal(str(tmp_path))} `
            -StateRoot {ps_literal(str(tmp_path))} -UvPath {ps_literal(str(tmp_path / "uv.exe"))} 6>$null
        ConvertTo-Json -Compress $script:registered
        """
    )

    result = _run(command)

    expected = [[hours, True] for hours in _specified_limit_hours()]
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == expected
