"""OpenCode forwarding and the node harness, the generated context file, and the Windows scheduler contract."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.integration_injection_case_helpers import (
    _shell_functions,
    _unmet_substrings,
    require_tool,
)
from tests.integration_injection_case_helpers import opencode_plugin_url as opencode_plugin_url
from tests.integration_injection_helpers import (
    ROOT,
    _ensure_scripts_on_path,
)
from tests.powershell_literal import ps_literal


def _task_limit(kind: str) -> str:
    """The execution-time limit the product registers for a task of `kind`.

    Read from the contract rather than written out again: the stub below spelled
    `PT3H`/`PT5H` and went on saying so after the passes' bounds moved to 4 h and
    6 h, so it offered the script a task the script rightly called stale.
    """
    _ensure_scripts_on_path()
    from llm_wiki import install_windows

    return f"PT{install_windows.WINDOWS_TASK_LIMIT_HOURS[kind]}H"


def test_codex_wrapper_is_labeled_compatibility_heartbeat_fallback():
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    assert "compatibility fallback" in wrapper.casefold()
    assert "heartbeat-only" in wrapper.casefold()
    assert "official hooks" in wrapper.casefold()


def test_opencode_host_directory_maps_directly_to_worktree_or_null():

    _ensure_scripts_on_path()
    from integration_adapter import normalize_event

    supplied = normalize_event(
        "opencode",
        "session_start",
        {"directory": "C:/host/project"},
    )
    unavailable = normalize_event(
        "opencode",
        "session_start",
        {"directory": None},
    )

    assert supplied.worktree == "C:/host/project"
    assert unavailable.worktree is None


def test_opencode_node_harness_forwards_bounded_tail_and_escaped_paths(
    tmp_path, opencode_plugin_url: str
):
    plugin_url = opencode_plugin_url
    root = str(tmp_path / "Vault With Spaces")
    directory = str(tmp_path / "Project With Spaces")
    adapter_context = json.dumps(
        {
            "context": (
                "# Project memory context\n\n## Health\n\nScheduler degraded.\n\n"
                "# Project handoff\n\n## MCP identifiers\n"
                "- `project:demo`\n- `sequence:7`\n"
            )
        }
    )
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        process.env.LLM_WIKI_CAPTURE_TIMEOUT_MS = "20";
        const commands = [];
        const requests = [];
        globalThis.Bun = {{ spawn(args, options) {{
          const record = {{ args, options, stdin: "", killed: false }};
          commands.push(record);
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          const context = args.at(-1) === "session_start"
            ? {json.dumps(adapter_context)}
            : "";
          const stdout = new ReadableStream({{ start(controller) {{
            if (context) controller.enqueue(new TextEncoder().encode(context));
            controller.close();
          }} }});
          return {{
            stdin: {{
              write(value) {{ record.stdin += value; }},
              end() {{ finish(0); }},
            }},
            stdout,
            exited,
            kill() {{ record.killed = true; finish(143); }},
          }};
        }} }};
        const client = {{ session: {{ messages: async (request) => {{
          requests.push(request);
          return {{ data: Array.from({{ length: 20 }}, (_, i) => ({{
            parts: [{{ text: `m${{i}}-` + "x".repeat(100) }}],
          }})) }};
        }} }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=1");
        const hooks = await LlmWikiMemoryPlugin({{ client, directory: {json.dumps(directory)} }});
        await hooks.event({{ event: {{
          type: "session.idle", properties: {{ sessionID: "session-1" }},
        }} }});
        await hooks["experimental.session.compacting"]({{ sessionId: "session-1" }});
        await hooks.event({{ event: {{
          type: "session.created",
          properties: {{ sessionID: "session-1", info: {{ id: "session-1" }} }},
        }} }});
        const system = [];
        await hooks["experimental.chat.system.transform"](
          {{ sessionID: "session-1" }}, {{ system }}
        );
        console.log(JSON.stringify({{ commands, requests, system }}));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    payload = json.loads(observed["commands"][0]["stdin"])
    compact_payload = json.loads(observed["commands"][1]["stdin"])
    assert (
        payload["directory"],
        payload["transcript_text"].startswith("m8-"),
        "m7-" in payload["transcript_text"],
        len(payload["transcript_text"]) <= 8000,
        observed["requests"][0]["query"]["limit"],
        compact_payload["transcript_text"].startswith("m8-"),
        _unmet_substrings(
            (
                ("## Health", observed["system"][0], True),
                ("project:demo", observed["system"][0], True),
                ("sequence:7", observed["system"][0], True),
            )
        ),
        observed["commands"][0]["args"],
    ) == (
        directory,
        True,
        False,
        True,
        12,
        True,
        [],
        [
            "uv",
            "run",
            "--locked",
            "--no-sync",
            "--directory",
            root,
            "python",
            f"{root}/scripts/integration_adapter.py",
            "--source",
            "opencode",
            "--event",
            "session_end",
        ],
    )


def test_opencode_node_harness_times_out_stalled_capture(tmp_path, opencode_plugin_url: str):
    plugin_url = opencode_plugin_url
    root = str(tmp_path / "Vault With Spaces")
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        process.env.LLM_WIKI_CAPTURE_TIMEOUT_MS = "20";
        let killed = false;
        globalThis.Bun = {{ spawn() {{
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            exited,
            kill() {{ killed = true; finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=timeout");
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "project" }});
        const started = Date.now();
        await hooks.event({{ event: {{
          type: "session.created", properties: {{ sessionID: "session-1" }},
        }} }});
        console.log(JSON.stringify({{ elapsed: Date.now() - started, killed }}));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["elapsed"] < 500
    assert observed["killed"] is True


def test_opencode_forwards_known_mutation_and_dirty_idle_without_fake_progress_signals(
    tmp_path, opencode_plugin_url: str
):
    plugin_url = opencode_plugin_url
    root = str(tmp_path / "vault")
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = {json.dumps(root)};
        const payloads = [];
        globalThis.Bun = {{ spawn() {{
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{
              write(value) {{ payloads.push(JSON.parse(value)); }},
              end() {{ finish(0); }},
            }},
            stdout: new ReadableStream({{ start(controller) {{ controller.close(); }} }}),
            exited,
            kill() {{ finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=signals");
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/project" }});
        await hooks["tool.execute.after"]({{
          sessionId: "session-1", tool: "edit", input: {{ filePath: "src/app.py" }}
        }});
        await hooks.event({{ event: {{
          type: "session.idle", properties: {{ sessionID: "session-1" }},
        }} }});
        console.log(JSON.stringify(payloads));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    tool, idle = json.loads(result.stdout)
    assert (
        tool["changed"] is True,
        tool["dirty"] is True,
        tool["significant"] is True,
        idle["dirty"] is True,
        _progress_signal_keys(tool, idle),
    ) == (True, True, True, True, [])


def _progress_signal_keys(*payloads: dict) -> list[str]:
    """Signals a host did not supply must not appear as if it had."""
    names = ("token_percent", "compaction_confirmed")
    return [name for payload in payloads for name in names if name in payload]


def test_codex_project_state_observes_session_start_before_recovery(monkeypatch, tmp_path):

    _ensure_scripts_on_path()
    import codex_memory

    calls = []
    monkeypatch.setattr(
        codex_memory,
        "observe_checkpoint_fail_open",
        lambda envelope: calls.append(("observe", envelope.event_type)),
    )
    monkeypatch.setattr(
        codex_memory,
        "run_script",
        lambda *args: (
            calls.append(("recover", args[0]))
            or subprocess.CompletedProcess(
                [], 0, '{"hookSpecificOutput":{"additionalContext":""}}', ""
            )
        ),
    )
    monkeypatch.setattr(codex_memory, "project_state_path", lambda path: ("demo", tmp_path / "state.md"))
    args = type("Args", (), {"cwd": str(tmp_path), "json": True})()

    assert codex_memory.command_project_state(args) == 0
    assert calls == [("observe", "session_start"), ("recover", "session_start_project_state.py")]


def test_opencode_forwards_sessions_inside_and_beside_the_vault(opencode_plugin_url: str):
    """The in-vault drop was removed on 2026-09-25 (audit C-6); both sessions are forwarded."""
    plugin_url = opencode_plugin_url
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = "/work/wiki";
        const commands = [];
        globalThis.Bun = {{ spawn(args) {{
          commands.push(args);
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            exited: Promise.resolve(0),
            kill() {{}},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin_url)} + "?harness=vault");
        const sibling = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/work/wiki-client" }});
        const vault = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/work/wiki" }});
        const created = {{ event: {{
          type: "session.created", properties: {{ sessionID: "session-1" }},
        }} }};
        await sibling.event(created);
        await vault.event(created);
        // A number goes through `util.inspect`, which colours it when
        // FORCE_COLOR is set in the developer's shell; a string never does.
        console.log(String(commands.length));
        """
    )

    result = subprocess.run(
        [require_tool("node"), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert (result.returncode, result.stdout.strip()) == (0, "2"), result.stderr


def test_codex_wrapper_generates_context_file():
    """The Codex wrapper must generate cache/session-context.md before
    starting codex, so the agent has knowledge context available.
    """
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    assert "session_start_context" in wrapper, (
        "Codex wrapper must call session_start_context.py before codex starts"
    )
    assert "session-context.md" in wrapper, "Codex wrapper must write to cache/session-context.md"


def test_session_start_context_supports_output_file():
    """session_start_context.py must support --output-file flag for
    writing context to a file (used by non-Claude agents).
    """
    script = (ROOT / "scripts" / "session_start_context.py").read_text(encoding="utf-8")
    assert "--output-file" in script, "session_start_context.py must support --output-file flag"
    assert "write_text" in script, "session_start_context.py must write context to the output file"


def test_install_scripts_generate_context(tmp_path):
    """Install scripts must generate the initial context file so the
    first session after install has knowledge context available.
    """
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    # Both installers call session_start_context.py during OpenCode setup.
    assert (
        _unmet_substrings(
            (
                ("session_start_context", install_ps1, True),
                ("session_start_context", install_sh, True),
                ("sync-args", install_sh, True),
                ("sync-args", install_ps1, True),
                # The sync plan itself (--locked --no-default-groups --extra full) is
                # pinned in tests/test_dependency_environments.py; the installers take it
                # from `installer_config.py sync-args`.
                ("--locked", install_sh, True),
                ("--locked", install_ps1, True),
            )
        )
        == []
    )

    sh_codex = install_sh.split("# Codex CLI", 1)[1].split("# Claude Code", 1)[0]
    sh_claude = install_sh.split("# Claude Code", 1)[1].split("# OpenCode configuration", 1)[0]
    sh_opencode = install_sh.split("# OpenCode configuration", 1)[1].split("# Codex CLI", 1)[0]
    ps_codex = install_ps1.split("# Codex", 1)[1].split("# Claude Code", 1)[0]
    ps_claude = install_ps1.split("# Claude Code", 1)[1].split("# --- 8.", 1)[0]
    ps_opencode = install_ps1.split("# OpenCode", 1)[1].split("# Codex", 1)[0]

    assert (
        _unmet_substrings(
            (
                ('CLAUDE_MCP="$HOME/.claude.json"', sh_claude, True),
                (
                    '"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--locked","--no-sync","--directory"',
                    sh_claude,
                    True,
                ),
                (".claude/.mcp.json", install_sh, False),
                ("Existing ~/.claude.json found without llm-wiki", sh_claude, True),
                ('claude_mcp_state "$CLAUDE_MCP" "$VAULT_ROOT"', sh_claude, True),
                ("scripts/installer_config.py", sh_opencode, True),
                ("opencode", sh_opencode, True),
                ('--root "$VAULT_ROOT"', sh_opencode, True),
                ('--state-root "$STATE_ROOT"', sh_opencode, True),
                ('--cwd "$CALLER_CWD"', sh_opencode, True),
                ("active)", sh_opencode, True),
                ("conflict)", sh_opencode, True),
                ("configured_unverified)", sh_opencode, True),
                ("not_detected)", sh_opencode, True),
                ("OPENCODE_CONFIG=", sh_opencode, False),
                ("grep", sh_opencode, False),
            )
        )
        == []
    )

    claude_json = _parse_shell_json(sh_claude, "CLAUDE_MCP")
    assert claude_json == {
        "mcpServers": {
            "llm-wiki": {
                "command": "uv",
                "args": [
                    "run",
                    "--locked",
                    "--no-sync",
                    "--directory",
                    "ROOT",
                    "python",
                    "scripts/mcp_server.py",
                ],
            }
        }
    }
    sh_codex_mcp = _shell_functions(install_sh, "write_codex_mcp_block", "add_codex_mcp_block", "codex_mcp_state_status", "configure_codex_mcp")
    ps_codex_mcp = install_ps1.split("function Add-CodexMcpEntry {", 1)[1].split("\nfunction ", 1)[0]
    assert (
        _unmet_substrings(
            (
                ('CODEX_CONFIG="$HOME/.codex/config.toml"', sh_codex, True),
                ("config-state", sh_codex_mcp, True),
                ('uv run --locked --no-sync --directory "$vault_root" python scripts/codex_memory.py', sh_codex_mcp, True),
                ('config-block --vault-root "$vault_root"', sh_codex_mcp, True),
                ('write_codex_mcp_block "$block" "$config"', sh_codex_mcp, True),
                ("config.bak", sh_codex_mcp, True),
                ("grep", sh_codex_mcp, False),
                ("$CODEX_HOOKS_STATE", sh_codex, True),
                ("codex_inline_hooks_state", install_sh, True),
                ("hooks-state", install_sh, True),
                (
                    '$claudeUserConfig = Join-Path $env:USERPROFILE ".claude.json"',
                    install_ps1,
                    True,
                ),
                ("$claudeMcp = $claudeUserConfig", ps_claude, True),
                ("mcpServers = [ordered]@{", ps_claude, True),
                (".claude\\.mcp.json", install_ps1, False),
                ("Existing ~/.claude.json found without llm-wiki", install_ps1, True),
                ("Write-ClaudeRegistration -McpState $claudeMcpState", ps_claude, True),
                ("Get-ClaudeMcpState -Config $claudeMcp -VaultRoot $VAULT_ROOT", ps_claude, True),
                ("-notmatch '\"llm-wiki\"\\s*:'", ps_claude, False),
                ("scripts\\installer_config.py", ps_opencode, True),
                ('"opencode", "--root", $VAULT_ROOT', ps_opencode, True),
                ('"--state-root", $STATE_ROOT', ps_opencode, True),
                ('"--cwd", $callerDirectory', ps_opencode, True),
                ('"active" {', ps_opencode, True),
                ('"conflict" {', ps_opencode, True),
                ('"configured_unverified" {', ps_opencode, True),
                ('"not_detected" {', ps_opencode, True),
                ("$openCodeMcp", ps_opencode, False),
                ("-notmatch", ps_opencode, False),
                ('$codexConfig = Join-Path $env:USERPROFILE ".codex\\config.toml"', ps_codex, True),
                ("function Install-CodexMcp", install_ps1, True),
                ("config-state", install_ps1, True),
                ('uv run --locked --no-sync --directory $VaultRoot python scripts/codex_memory.py', ps_codex_mcp, True),
                ('config-block --vault-root $VaultRoot', ps_codex_mcp, True),
                ('$block = $blockLines -join "`n"', ps_codex_mcp, True),
                ("Copy-Item -LiteralPath $Config", install_ps1, True),
                ("Config.bak", install_ps1, True),
                ("codex-memory-wrapper", ps_codex, True),
                ("$codexHooksState", ps_codex, True),
                ("hooks-state", install_ps1, True),
                ("v4.0 optional features", install_sh, False),
                ("mcp-server", install_sh.split("Useful commands:", 1)[-1], False),
                ("function Write-Utf8NoBom", install_ps1, True),
                ("[System.IO.File]::WriteAllText", install_ps1, True),
                ("[System.Text.UTF8Encoding]::new($false)", install_ps1, True),
                ("Write-Utf8NoBom $claudeMcp", ps_claude, True),
                ("Set-Content -LiteralPath $claudeMcp", ps_claude, False),
                ("Add-Content -LiteralPath $claudeMcp", ps_claude, False),
                ("Set-Content -LiteralPath $Config", install_ps1, False),
                ("Add-Content -LiteralPath $Config", install_ps1, False),
            )
        )
        == []
    )


def _parse_shell_json(block: str, destination: str) -> dict:
    line = next(line for line in block.splitlines() if f'> "${destination}"' in line)
    match = re.search(r"printf '%s\\n' '(.*?)'\"\$VAULT_JSON\"'(.*?)' >", line)
    assert match, line
    return json.loads(match.group(1) + '"ROOT"' + match.group(2))


def test_windows_scheduler_payload_carries_exact_roots_and_uv(tmp_path):
    script = ROOT / "scripts" / "install-scheduled-tasks.ps1"
    runner = ROOT / "scripts" / "run-scheduled-task.ps1"
    root = str(tmp_path / "vault's source")
    state = str(tmp_path / "state's data")
    uv_path = str(tmp_path / "bin's tools" / "uv.exe")


    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(script))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'New-LLMWikiScheduledAction'
        }}, $true)
        if ($null -eq $fn) {{ throw 'New-LLMWikiScheduledAction missing' }}
        Invoke-Expression $fn.Extent.Text
        function New-ScheduledTaskAction {{
            param($Execute, $Argument)
            [pscustomobject]@{{ Execute = $Execute; Argument = $Argument }}
        }}
        $action = New-LLMWikiScheduledAction `
            -Kind nightly `
            -VaultRoot {ps_literal(root)} `
            -StateRoot {ps_literal(state)} `
            -UvPath {ps_literal(uv_path)} `
            -RunnerPath {ps_literal(str(runner))} `
            -PowerShellPath {ps_literal(shutil.which("pwsh") or "pwsh")}
        $encoded = ($action.Argument -split '\\s+')[-1]
        $decoded = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
        [pscustomobject]@{{ Execute = $action.Execute; Decoded = $decoded }} |
            ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        # pwsh startup alone can take ten seconds on a loaded hosted runner.
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    decoded = value["Decoded"]
    quoted_root = root.replace("'", "''")
    quoted_state = state.replace("'", "''")
    quoted_uv = uv_path.replace("'", "''")
    assert (
        _unmet_substrings(
            (
                (
                    Path(value["Execute"]).name.casefold(),
                    {"pwsh", "pwsh.exe", "powershell.exe"},
                    True,
                ),
                ("-Kind 'nightly'", decoded, True),
                (f"-VaultRoot '{quoted_root}'", decoded, True),
                (f"-StateRoot '{quoted_state}'", decoded, True),
                (f"-UvPath '{quoted_uv}'", decoded, True),
                (str(runner).replace("'", "''"), decoded, True),
            )
        )
        == []
    )


def test_windows_scheduler_status_accepts_only_the_registered_contract(tmp_path):
    if shutil.which("pwsh") is None:
        pytest.skip("PowerShell 7 is unavailable")
    script = ROOT / "scripts" / "install-scheduled-tasks.ps1"
    root = str(tmp_path / "vault")
    state = str(tmp_path / "state")
    uv_path = str(tmp_path / "uv.exe")
    runner = str(tmp_path / "vault/scripts/run-scheduled-task.ps1")


    command = textwrap.dedent(
        f"""
        $ErrorActionPreference = 'Stop'
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {ps_literal(str(script))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        foreach ($name in @(
            'Get-LLMWikiLimitHours', 'Get-LLMWikiConfiguredLimitHours',
            'New-LLMWikiScheduledAction', 'Test-LLMWikiTaskSpec',
            'Test-LLMWikiScheduledTasks', 'Test-LLMWikiTaskAction', 'Test-LLMWikiTaskSchedule',
            'Test-LLMWikiTaskIdentity', 'Write-LLMWikiTaskStatus', 'Test-LLMWikiTaskRegistration'
        )) {{
            $fn = $ast.Find({{ param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $name
            }}, $true)
            if ($null -eq $fn) {{ throw "$name missing" }}
            Invoke-Expression $fn.Extent.Text
        }}
        function New-ScheduledTaskAction {{
            param($Execute, $Argument)
            [pscustomobject]@{{ Execute = $Execute; Arguments = $Argument }}
        }}
        $script:invalid = $false
        function Get-ScheduledTask {{
            param($TaskName, $ErrorAction)
            $kind = if ($TaskName -eq 'LLMWiki-Nightly') {{ 'nightly' }} else {{ 'weekly' }}
            $action = New-LLMWikiScheduledAction `
                -Kind $kind `
                -VaultRoot {ps_literal(root)} `
                -StateRoot {ps_literal(state)} `
                -UvPath {ps_literal(uv_path)} `
                -RunnerPath {ps_literal(runner)} `
                -PowerShellPath 'pwsh.exe'
            $limit = if ($kind -eq 'nightly') {{ {ps_literal(_task_limit("nightly"))} }} else {{ {ps_literal(_task_limit("weekly"))} }}
            [pscustomobject]@{{
                State = 'Ready'
                Description = 'LLM-wiki task [llm-wiki-task-spec:2]'
                Settings = [pscustomobject]@{{ ExecutionTimeLimit = $limit }}
                Actions = @($action)
                Triggers = @([pscustomobject]@{{
                    StartBoundary = '2026-08-15T03:00:00'
                    Enabled = $true
                }})
                Principal = [pscustomobject]@{{
                    LogonType = $(if ($script:invalid) {{ 'ServiceAccount' }} else {{ 'Interactive' }})
                    UserId = 'operator'
                }}
            }}
        }}
        function Get-ScheduledTaskInfo {{
            param($InputObject, $ErrorAction)
            process {{ [pscustomobject]@{{
                LastRunTime = [datetime]::MinValue
                LastTaskResult = 0
                NextRunTime = [datetime]::MaxValue
            }} }}
        }}
        $valid = Test-LLMWikiScheduledTasks `
            -VaultRoot {ps_literal(root)} `
            -StateRoot {ps_literal(state)} `
            -UvPath {ps_literal(uv_path)}
        $script:invalid = $true
        $invalid = Test-LLMWikiScheduledTasks `
            -VaultRoot {ps_literal(root)} `
            -StateRoot {ps_literal(state)} `
            -UvPath {ps_literal(uv_path)}
        @([bool]$valid, [bool]$invalid) | ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        [require_tool("pwsh"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        # pwsh startup alone can take ten seconds on a loaded hosted runner.
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [True, False], result.stdout
