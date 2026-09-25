"""The OpenCode plugin and the adapter read the event shapes OpenCode sends.

`session.created` carries `{info: Session}`, `tool.execute.after` carries `args`
(OpenCode SDK and plugin types, 2026-09-25). See
docs/research/2026-09-25-the-opencode-plugin-reads-the-events-opencode-sends.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import integration_adapter
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_plugin(tmp_path: Path, body: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    plugin = tmp_path / "plugin.mjs"
    shutil.copyfile(ROOT / "scripts" / "llm-wiki-memory-opencode.js", plugin)
    harness = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = "/work/wiki";
        const commands = [];
        globalThis.Bun = {{ spawn(args) {{
          commands.push(args[args.length - 1]);
          const reply = new TextEncoder().encode(JSON.stringify({{ context: "vault context" }}));
          return {{
            stdin: {{ write() {{}}, end() {{}} }},
            stdout: new ReadableStream({{ start(c) {{ c.enqueue(reply); c.close(); }} }}),
            exited: Promise.resolve(0),
            kill() {{}},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin.resolve().as_uri())});
        const client = {{ session: {{ messages: async () => ({{ data: [{{ parts: [{{ text: "same words" }}] }}] }}) }} }};
        const hooks = await LlmWikiMemoryPlugin({{ client, directory: "/work/project" }});
        {body}
        """
    )
    result = subprocess.run([node, "--input-type=module", "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_session_start_context_reaches_the_system_prompt(tmp_path: Path) -> None:
    out = _run_plugin(
        tmp_path,
        """
        await hooks.event({ event: { type: "session.created", properties: { info: { id: "s1" } } } });
        const output = { system: [] };
        await hooks["experimental.chat.system.transform"]({ sessionID: "s1" }, output);
        console.log(JSON.stringify(output.system));
        """,
    )

    assert out == '["vault context"]'


def test_an_idle_with_nothing_new_is_not_forwarded_twice(tmp_path: Path) -> None:
    out = _run_plugin(
        tmp_path,
        """
        const idle = { event: { type: "session.idle", properties: { sessionID: "s1" } } };
        await hooks.event(idle);
        await hooks.event(idle);
        console.log(String(commands.filter((event) => event === "session_end").length));
        """,
    )

    assert out == "1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"tool": "edit", "args": {"filePath": "src/a.py"}}, "src/a.py"),
        ({"tool": "bash", "args": {"command": "pytest -q"}}, "pytest -q"),
        ({"tool": "edit", "input": {"filePath": "old/shape.py"}}, "old/shape.py"),
    ],
)
def test_the_adapter_reads_an_opencode_tool_target_from_args(raw, expected) -> None:
    assert integration_adapter._tool_payload("opencode", raw)["target"] == expected


def test_the_adapter_reads_the_session_from_info() -> None:
    assert integration_adapter._session("opencode", {"info": {"id": "s1"}}) == "s1"
