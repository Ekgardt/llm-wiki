"""Guard tests for thin native lifecycle integrations.

These tests ensure that:
1. The OpenCode plugin forwards lifecycle events to shared Python ingestion.
2. Native integrations do not duplicate MCP reads or classification logic.
3. The Codex wrapper generates a context file before codex starts.
4. The Cursor rules file contains mandatory session-start context reading
5. The Antigravity AGENTS.md contains mandatory session-start context reading
6. session_start_context.py supports --output-file mode
7. The install scripts generate the initial context file

If any of these are removed, CI catches it.
"""
from __future__ import annotations

import json
import re
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_opencode_plugin_is_lifecycle_only():
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert "integration_adapter.py" in plugin
    assert '"session.created"' in plugin
    assert '"tool.execute.after"' in plugin
    assert '"session.idle"' in plugin
    assert '"experimental.session.compacting"' in plugin
    assert '"memory.context"' not in plugin
    assert '"memory.recall"' not in plugin
    assert "Classify this transcript" not in plugin
    assert "FLUSH_MAJOR" not in plugin
    assert "memory-ephemeral" not in plugin
    assert "client.session.create" not in plugin
    assert "client.session.prompt" not in plugin
    assert "memory_queue.py" not in plugin
    assert "maybe_compile.py" not in plugin
    assert "computeSlug" not in plugin
    assert "state-path" not in plugin
    assert 'directory: typeof directory === "string" ? directory : null' in plugin
    assert "project" not in plugin


def test_opencode_host_directory_maps_directly_to_worktree_or_null():
    import sys

    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
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


def test_opencode_node_harness_forwards_bounded_tail_and_escaped_paths(tmp_path):
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
    root = str(tmp_path / "Vault With Spaces")
    directory = str(tmp_path / "Project With Spaces")
    adapter_context = json.dumps({
        "context": "# Project memory context\n\n## Health\n\nScheduler degraded."
    })
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
        await hooks["session.idle"]({{ sessionId: "session-1" }});
        await hooks["experimental.session.compacting"]({{ sessionId: "session-1" }});
        await hooks["session.created"]({{ sessionInfo: {{ id: "session-1" }} }});
        const system = [];
        await hooks["experimental.chat.system.transform"](
          {{ sessionID: "session-1" }}, {{ system }}
        );
        console.log(JSON.stringify({{ commands, requests, system }}));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    payload = json.loads(observed["commands"][0]["stdin"])
    assert payload["directory"] == directory
    assert payload["transcript_text"].startswith("m8-")
    assert "m7-" not in payload["transcript_text"]
    assert len(payload["transcript_text"]) <= 8000
    assert observed["requests"][0]["query"]["limit"] == 12
    assert observed["commands"][1]["stdin"]
    compact_payload = json.loads(observed["commands"][1]["stdin"])
    assert compact_payload["transcript_text"].startswith("m8-")
    assert "## Health" in observed["system"][0]
    assert observed["commands"][0]["args"] == [
        "uv",
        "run",
        "--directory",
        root,
        "python",
        f"{root}/scripts/integration_adapter.py",
        "--source",
        "opencode",
        "--event",
        "session_end",
    ]


def test_opencode_node_harness_times_out_stalled_capture(tmp_path):
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
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
        await hooks["session.created"]({{}});
        console.log(JSON.stringify({{ elapsed: Date.now() - started, killed }}));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["elapsed"] < 500
    assert observed["killed"] is True


def test_opencode_vault_guard_uses_resolved_path_boundary():
    plugin_url = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").resolve().as_uri()
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
        await sibling["session.created"]({{}});
        await vault["session.created"]({{}});
        console.log(commands.length);
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_codex_wrapper_generates_context_file():
    """The Codex wrapper must generate cache/session-context.md before
    starting codex, so the agent has knowledge context available.
    """
    wrapper = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    assert "session_start_context" in wrapper, (
        "Codex wrapper must call session_start_context.py before codex starts"
    )
    assert "session-context.md" in wrapper, (
        "Codex wrapper must write to cache/session-context.md"
    )


def test_cursor_rules_has_mandatory_context_read():
    """Cursor rules file must instruct the agent to read the session
    context file at session start (MANDATORY).
    """
    rules = (ROOT / "integrations" / "cursor" / "rules" / "llm-wiki.mdc").read_text(encoding="utf-8")
    assert "session-context.md" in rules, (
        "Cursor rules must reference cache/session-context.md"
    )
    assert "MANDATORY" in rules.upper() or "first" in rules.lower(), (
        "Cursor rules must mark context reading as mandatory/first step"
    )


def test_antigravity_agents_has_mandatory_context_read():
    """Antigravity AGENTS.md must instruct the agent to read the session
    context file at session start (MANDATORY).
    """
    agents = (ROOT / "integrations" / "antigravity" / "AGENTS.md").read_text(encoding="utf-8")
    assert "session-context.md" in agents, (
        "Antigravity AGENTS.md must reference cache/session-context.md"
    )
    assert "MANDATORY" in agents.upper() or "first" in agents.lower(), (
        "Antigravity AGENTS.md must mark context reading as mandatory/first step"
    )


def test_session_start_context_supports_output_file():
    """session_start_context.py must support --output-file flag for
    writing context to a file (used by non-Claude agents).
    """
    script = (ROOT / "scripts" / "session_start_context.py").read_text(encoding="utf-8")
    assert "--output-file" in script, (
        "session_start_context.py must support --output-file flag"
    )
    assert "write_text" in script, (
        "session_start_context.py must write context to the output file"
    )


def test_install_scripts_generate_context(tmp_path):
    """Install scripts must generate the initial context file so the
    first session after install has knowledge context available.
    """
    install_ps1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
    install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "session_start_context" in install_ps1, (
        "install.ps1 must call session_start_context.py during OpenCode setup"
    )
    assert "session_start_context" in install_sh, (
        "install.sh must call session_start_context.py during OpenCode setup"
    )
    locked_mcp_sync = "uv sync --locked --extra mcp-server --quiet"
    assert locked_mcp_sync in install_sh
    assert locked_mcp_sync in install_ps1

    sh_codex = install_sh.split("# Codex CLI", 1)[1].split("# Cursor", 1)[0]
    sh_claude = install_sh.split("# Claude Code", 1)[1].split("# v4.0: OpenCode", 1)[0]
    sh_opencode = install_sh.split("# v4.0: OpenCode", 1)[1].split("# ─── 9.", 1)[0]
    ps_codex = install_ps1.split("# Codex", 1)[1].split("# Claude Code", 1)[0]
    ps_claude = install_ps1.split("# Claude Code", 1)[1].split("# Cursor", 1)[0]
    ps_opencode = install_ps1.split("# OpenCode", 1)[1].split("# Codex", 1)[0]

    assert 'CLAUDE_MCP="$HOME/.claude.json"' in sh_claude
    assert '"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--directory"' in sh_claude
    assert ".claude/.mcp.json" not in install_sh
    assert "Existing ~/.claude.json found without llm-wiki" in sh_claude
    assert "grep -q '\"llm-wiki\"'" in sh_claude

    assert 'OPENCODE_CONFIG="$HOME/.config/opencode/opencode.json"' in sh_opencode
    assert '"mcp":{"llm-wiki":{"type":"local","command":["uv","run","--directory"' in sh_opencode
    assert '"enabled":true' in sh_opencode
    assert '"mcpServers"' not in sh_opencode
    assert "Existing opencode.json found without llm-wiki" in sh_opencode
    assert "grep -q '\"llm-wiki\"'" in sh_opencode

    def parse_shell_json(block: str, destination: str) -> dict:
        line = next(line for line in block.splitlines() if f'> "${destination}"' in line)
        match = re.search(
            r"printf '%s\\n' '(.*?)'\"\$VAULT_JSON\"'(.*?)' >", line
        )
        assert match, line
        return json.loads(match.group(1) + '"ROOT"' + match.group(2))

    claude_json = parse_shell_json(sh_claude, "CLAUDE_MCP")
    assert claude_json == {
        "mcpServers": {
            "llm-wiki": {
                "command": "uv",
                "args": [
                    "run", "--directory", "ROOT", "python", "scripts/mcp_server.py"
                ],
            }
        }
    }
    opencode_json = parse_shell_json(sh_opencode, "OPENCODE_CONFIG")
    assert opencode_json == {
        "mcp": {
            "llm-wiki": {
                "type": "local",
                "command": [
                    "uv", "run", "--directory", "ROOT", "python",
                    "scripts/mcp_server.py",
                ],
                "enabled": True,
            }
        }
    }

    assert 'CODEX_CONFIG="$HOME/.codex/config.toml"' in sh_codex
    assert "[mcp_servers.llm-wiki]" in sh_codex
    assert 'command = "uv"' in sh_codex
    assert 'args = [\\"run\\", \\"--directory\\"' in sh_codex
    assert "CODEX_CONFIG.bak" in sh_codex
    assert "codex_memory.py daily-log" in sh_codex
    sh_args_line = next(line for line in sh_codex.splitlines() if "args = [" in line)
    sh_args = sh_args_line.split("args = ", 1)[1].rsplit('"', 1)[0]
    sh_args = sh_args.replace('\\"', '"').replace("$VAULT_JSON", '"ROOT"')
    assert json.loads(sh_args) == [
        "run", "--directory", "ROOT", "python", "scripts/mcp_server.py"
    ]

    assert '$claudeUserConfig = Join-Path $env:USERPROFILE ".claude.json"' in install_ps1
    assert "$claudeMcp = $claudeUserConfig" in ps_claude
    assert 'mcpServers = [ordered]@{' in ps_claude
    assert ".claude\\.mcp.json" not in install_ps1
    assert "Existing ~/.claude.json found without llm-wiki" in ps_claude
    assert "-notmatch '\"llm-wiki\"\\s*:'" in ps_claude

    assert '$openCodeMcp = Join-Path $openCodeConfig "opencode.json"' in ps_opencode
    assert 'mcp = [ordered]@{' in ps_opencode
    assert 'type = "local"' in ps_opencode
    assert 'command = @("uv", "run", "--directory", $VAULT_ROOT, "python", "scripts/mcp_server.py")' in ps_opencode
    assert "enabled = $true" in ps_opencode
    assert "mcpServers" not in ps_opencode
    assert "-notmatch '\"llm-wiki\"\\s*:'" in ps_opencode

    assert '$codexConfig = Join-Path $env:USERPROFILE ".codex\\config.toml"' in ps_codex
    assert "[mcp_servers.llm-wiki]" in ps_codex
    assert 'command = "uv"' in ps_codex
    assert 'args = ["run", "--directory"' in ps_codex
    assert "Copy-Item -LiteralPath $codexConfig" in ps_codex
    assert "codexConfig.bak" in ps_codex
    assert "codex-memory-wrapper" in ps_codex
    ps_args_line = next(line for line in ps_codex.splitlines() if line.startswith("args = ["))
    ps_args = ps_args_line.split("=", 1)[1].strip().replace('"$tomlVault"', '"ROOT"')
    assert json.loads(ps_args) == [
        "run", "--directory", "ROOT", "python", "scripts/mcp_server.py"
    ]

    assert "v4.0 optional features" not in install_sh
    assert "mcp-server" not in install_sh.split("Useful commands:", 1)[-1]

    assert "function Write-Utf8NoBom" in install_ps1
    assert "[System.IO.File]::WriteAllText" in install_ps1
    assert "[System.Text.UTF8Encoding]::new($false)" in install_ps1
    for block, config_var in (
        (ps_opencode, "$openCodeMcp"),
        (ps_claude, "$claudeMcp"),
        (ps_codex, "$codexConfig"),
    ):
        assert f"Write-Utf8NoBom {config_var}" in block
        assert f"Set-Content -LiteralPath {config_var}" not in block
        assert f"Add-Content -LiteralPath {config_var}" not in block
    assert "Copy-Item -LiteralPath $codexConfig" in ps_codex
    assert "$codexExisting +" in ps_codex
    install_path = ROOT / "install.ps1"
    external = str(tmp_path / "external runtime")
    vault = str(tmp_path / "vault")
    command = textwrap.dedent(
        f"""
        $tokens = $null
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {json.dumps(str(install_path))}, [ref]$tokens, [ref]$errors)
        if ($errors.Count) {{ throw ($errors | Out-String) }}
        $fn = $ast.Find({{ param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Resolve-StateRoot'
        }}, $true)
        if ($null -eq $fn) {{ throw 'Resolve-StateRoot missing' }}
        Invoke-Expression $fn.Extent.Text
        $first = Resolve-StateRoot -ProcessState '{external.replace("'", "''")}' -UserState '' -VaultRoot '{vault.replace("'", "''")}'
        $second = Resolve-StateRoot -ProcessState $first -UserState '' -VaultRoot '{vault.replace("'", "''")}'
        $fromUser = Resolve-StateRoot -ProcessState '' -UserState '{external.replace("'", "''")}' -VaultRoot '{vault.replace("'", "''")}'
        @($first, $second, $fromUser) | ConvertTo-Json -Compress
        """
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [external, external, external]
