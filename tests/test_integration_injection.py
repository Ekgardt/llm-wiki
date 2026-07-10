"""Guard tests: verify context injection mechanisms exist for all agents.

These tests ensure that:
1. The OpenCode plugin defines custom tools (memory.context, memory.recall)
2. The OpenCode plugin generates a context file at session.created
3. The Codex wrapper generates a context file before codex starts
4. The Cursor rules file contains mandatory session-start context reading
5. The Antigravity AGENTS.md contains mandatory session-start context reading
6. session_start_context.py supports --output-file mode
7. The install scripts generate the initial context file

If any of these are removed, CI catches it.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_opencode_plugin_has_memory_context_tool():
    """The OpenCode plugin must define a memory.context custom tool so the
    agent can get session-start knowledge context via a native tool call.
    """
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert '"memory.context"' in plugin, (
        "OpenCode plugin missing memory.context tool — agent has no way to "
        "get session-start knowledge context via native tool call"
    )
    assert "session_start_context" in plugin, (
        "memory.context tool must call session_start_context.py"
    )


def test_opencode_plugin_has_memory_recall_tool():
    """The OpenCode plugin must define a memory.recall custom tool so the
    agent can search the knowledge base in real-time.
    """
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert '"memory.recall"' in plugin, (
        "OpenCode plugin missing memory.recall tool — agent has no way to "
        "search the knowledge base via native tool call"
    )
    assert "search_memory" in plugin, (
        "memory.recall tool must call search_memory.py"
    )


def test_opencode_plugin_generates_context_file():
    """The session.created handler must generate cache/session-context.md
    as a fallback for agents that don't support custom tools.
    """
    plugin = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(encoding="utf-8")
    assert "generateContextFile" in plugin or "session-context.md" in plugin, (
        "OpenCode plugin session.created handler must generate "
        "cache/session-context.md for non-tool agents"
    )
    assert "--output-file" in plugin, (
        "Plugin must use --output-file flag to generate context file"
    )


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


def test_install_scripts_generate_context():
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
