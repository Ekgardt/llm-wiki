"""A checkpoint recorded that something happened and never what.

Measured on the live vault 2026-08-29: `knowledge/projects/llm-wiki/journal.md`
held 1 000 events and every one carried `checkpoint-none` closes on all three
scalars and empty lists on the other six — zero events with a single list
operation. `state.md` read `Goal: None, Phase: None`, and always had.
`_checkpoint_delta` reads `payload["project_delta"]`, which nothing in
`scripts/`, `integrations/`, `skills/` or `rules/` ever writes.

What to derive is settled by measurement rather than taste. The cold-start
ablation separates the contribution of the agentic tasks in history from the
agent's own response content and finds the tasks are the primary driver while
the response content has little effect, so the delta is derived from the
observation and never narrated. Every value is dated, because the dominant
failure of carried state is staleness rather than absence.

See `docs/research/2026-08-29-what-a-project-checkpoint-should-record.md`.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402

AT = datetime(2026, 8, 29, 21, 57, tzinfo=timezone.utc)


def _edit_event(**overrides):
    raw = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "scripts/retrieval.py"},
        "changed": True,
        "significant": True,
        "session_id": "s1",
        "cwd": "/tmp",
    }
    raw.update(overrides)
    return integration_adapter.normalize_event(
        "claude", "post_tool_use", raw, occurred_at=AT
    )


def test_a_changed_file_is_recorded_by_its_path() -> None:
    delta = integration_adapter._derived_delta(_edit_event())

    assert delta["changed_files"] == [
        {
            "id": "file:scripts/retrieval.py",
            "action": "upsert",
            "value": "scripts/retrieval.py — 2026-08-29T21:57:00+00:00",
        }
    ]


def test_editing_the_same_file_twice_stays_one_item() -> None:
    """The id is the path, so churn on one file does not fill the projection."""
    first = integration_adapter._derived_delta(_edit_event())
    second = integration_adapter._derived_delta(_edit_event())

    assert first["changed_files"][0]["id"] == second["changed_files"][0]["id"]


def test_a_tool_that_changed_nothing_records_no_file() -> None:
    delta = integration_adapter._derived_delta(_edit_event(changed=False))

    assert delta["changed_files"] == []


def test_a_command_is_recorded_and_keyed_by_its_own_text() -> None:
    event = _edit_event(tool_name="Bash", tool_input={"command": "uv run pytest -q"})

    delta = integration_adapter._derived_delta(event)

    assert delta["commands"] == [
        {
            "id": "cmd:uv run pytest -q",
            "action": "upsert",
            "value": "uv run pytest -q — 2026-08-29T21:57:00+00:00",
        }
    ]


def test_a_failure_opens_a_blocker_keyed_by_what_failed() -> None:
    """Keyed by the failing thing, not the moment, so a repeat is one blocker."""
    event = _edit_event(severity="error")

    delta = integration_adapter._derived_delta(event)

    assert delta["blockers"][0]["id"] == "failed:Edit:scripts/retrieval.py"
    assert "Edit failed" in delta["blockers"][0]["value"]


def test_a_successful_run_opens_no_blocker() -> None:
    assert integration_adapter._derived_delta(_edit_event())["blockers"] == []


def test_the_current_task_is_one_id_that_is_always_replaced() -> None:
    """Thousands of tool events must leave one line, not thousands."""
    delta = integration_adapter._derived_delta(_edit_event())

    assert delta["current_task"]["id"] == "observed"
    assert delta["current_task"]["value"].startswith("Edit scripts/retrieval.py")


def test_every_derived_value_carries_its_date() -> None:
    """Staleness is the dominant failure of carried state; an undated item hides it."""
    delta = integration_adapter._derived_delta(_edit_event())
    values = [delta["current_task"]["value"], delta["changed_files"][0]["value"]]

    assert all(value.endswith("2026-08-29T21:57:00+00:00") for value in values)


def test_a_stated_delta_still_wins() -> None:
    """An agent that says what it is doing is not overruled by the derivation."""
    stated = integration_adapter._empty_delta()
    stated["goal"] = {"id": "g1", "action": "upsert", "value": "ship rotation"}
    event = _edit_event(project_delta=stated)

    assert integration_adapter._checkpoint_delta(event)["goal"]["value"] == (
        "ship rotation"
    )


def test_an_observation_with_no_tool_records_nothing_but_a_close() -> None:
    """Nothing observed is nothing claimed."""
    event = integration_adapter.normalize_event(
        "claude", "stop", {"session_id": "s1", "cwd": "/tmp"}, occurred_at=AT
    )

    delta = integration_adapter._derived_delta(event)

    assert delta["current_task"]["action"] == "close"
    assert delta["changed_files"] == []
