"""Helpers the integration injection scenario files share."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.integration_injection_helpers import (
    ROOT,
)


def _existing_transcript(tmp_path) -> str:
    """A transcript the adapter can actually find.

    The fixture used to name `C:/tmp/session.jsonl`, which exists on no machine
    this suite runs on, so these tests only ever exercised the branch by having
    the key present. Since 2026-09-02 a path that names nothing is treated as no
    transcript — which is what production does when a session leaves no file —
    so the fixture has to be a real one.
    """
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    return str(transcript)


def require_tool(name: str) -> str:
    """Resolve an optional external runtime or skip the calling test.

    Node and PowerShell 7 are optional for contributors (README: "Node 22 is
    optional"), so a missing binary must skip rather than fail the suite.
    """
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is unavailable")
    return path


def _unmet_substrings(checks) -> list[tuple[int, str, bool]]:
    """(index, needle, should_be_present) for every check the text does not meet."""
    return [
        (index, needle, present)
        for index, (needle, text, present) in enumerate(checks)
        if (needle in text) is not present
    ]


@pytest.fixture
def opencode_plugin_url(tmp_path: Path) -> str:
    plugin = tmp_path / "llm-wiki-memory-opencode.mjs"
    shutil.copyfile(ROOT / "scripts" / "llm-wiki-memory-opencode.js", plugin)
    return plugin.resolve().as_uri()


def _without_writer_wait(callback):
    def observe(envelope, *, writer_wait_seconds):
        assert writer_wait_seconds == 0.0
        return callback(envelope)

    return observe


def _codex_inline_hooks_toml(*, include_stop: bool = True) -> str:
    template = json.loads(
        (ROOT / "integrations" / "codex" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    blocks = []
    for event_name, groups in template.items():
        if event_name == "Stop" and not include_stop:
            continue
        for group in groups:
            blocks.extend(_codex_inline_group_toml(event_name, group))
    return "\n".join(blocks)


def _codex_inline_group_toml(event_name: str, group: dict) -> list[str]:
    """One template group as Codex's inline TOML: an event may hold several (#24, 2026-09-11)."""
    lines = [f"[[hooks.{event_name}]]"]
    if "matcher" in group:
        lines.append(f"matcher = {json.dumps(group['matcher'])}")
    for handler in group["hooks"]:
        lines.extend(
            [
                "",
                f"[[hooks.{event_name}.hooks]]",
                'type = "command"',
                f"command = {json.dumps(handler['command'])}",
                f"command_windows = {json.dumps(handler['commandWindows'])}",
                "",
            ]
        )
        if "timeout" in handler:
            lines.append(f"timeout = {handler['timeout']}")
    return lines


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", source)
    assert match, f"{name} missing"
    return match.group(0)


def _shell_functions(source: str, *names: str) -> str:
    return "\n".join(_shell_function(source, name) for name in names)
