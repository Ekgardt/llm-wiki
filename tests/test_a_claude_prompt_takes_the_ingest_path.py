"""A Claude prompt takes the same ingest path as every host, inside the host's time.

Claude's hooks name `--delegate user_prompt_capture.py`, and a named delegate ran
on its own instead of the ingest path other hosts' prompts take; a delegate could
outlive the hook the host was about to kill. (The feedback capture that path ran
was retired on 2026-09-25,
docs/research/2026-09-25-corrections-are-learned-by-compile-not-by-candidates.md.) See
docs/research/2026-09-25-a-claude-prompt-reaches-feedback-capture.md and
docs/research/2026-09-25-a-hook-stops-its-delegate-before-the-host-stops-it.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import integration_adapter
import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def paths(monkeypatch) -> list[str]:
    taken: list[str] = []
    monkeypatch.setattr(integration_adapter, "ingest_event", lambda envelope: taken.append("ingest") or {})
    monkeypatch.setattr(integration_adapter, "_run_own_delegate", lambda args, envelope: taken.append("own"))
    return taken


@pytest.mark.parametrize(
    ("event", "delegate", "expected"),
    [
        ("user_prompt", "user_prompt_capture.py", "ingest"),
        ("post_tool_use", "post_tool_capture.py", "ingest"),
        ("pre_compact", "precompact_capture.py", "ingest"),
        ("session_start", "session_start_context.py", "own"),
    ],
)
def test_a_named_delegate_takes_the_path_its_event_needs(paths, event, delegate, expected) -> None:
    args = argparse.Namespace(source="claude", delegate=delegate)

    integration_adapter._dispatch_cli_event(args, SimpleNamespace(event_type=event))

    assert paths == [expected]


def test_every_claude_hook_delegate_is_known_to_the_adapter() -> None:
    known = set(integration_adapter.INGESTED_DELEGATES.values()) | set(integration_adapter.DELEGATES)

    assert {"user_prompt_capture.py", "post_tool_capture.py"} <= known


# The delegates the adapter itself runs for each event of the installed Claude hooks.
EVENT_DELEGATES = {
    "UserPromptSubmit": ("user_prompt_capture.py",),
    "PostToolUse": ("post_tool_capture.py",),
}
ADAPTER_START_ALLOWANCE_SECONDS = 1.0


def _host_timeouts() -> dict[str, float]:
    settings = json.loads((ROOT / "integrations/claude-code/settings.json").read_text(encoding="utf-8"))
    return {event: groups[0]["hooks"][0]["timeout"] for event, groups in settings["hooks"].items()}


def test_every_delegate_stops_before_the_host_stops_its_hook() -> None:
    """Audit B-11: the host killed a 10-second delegate at 5 seconds, unrecorded."""
    host = _host_timeouts()
    needed = {
        event: sum(integration_adapter.DELEGATE_TIMEOUTS.get(name, integration_adapter.DELEGATE_TIMEOUT_SECONDS) for name in names)
        + ADAPTER_START_ALLOWANCE_SECONDS
        for event, names in EVENT_DELEGATES.items()
    }

    assert {event: needed[event] <= host[event] for event in EVENT_DELEGATES} == dict.fromkeys(EVENT_DELEGATES, True)
