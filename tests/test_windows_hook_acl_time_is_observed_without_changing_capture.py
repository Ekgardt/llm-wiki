"""Observe real Windows ACL costs while keeping the original host assertions."""
import json
import os
import time

import integration_adapter
import markdown_transaction
import pytest

from tests.test_a_prompt_checkpoint_does_not_outwait_its_host import (
    test_capture_keeps_its_checkpoint_pending_without_waiting_past_host as original_capture,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="actual Windows ACL command timing")


def _observed_acl(command, *, original, observations, phase):
    started = time.monotonic()
    try:
        return original(command)
    finally:
        observations.append({"phase": phase["name"], "operation": command[2:],
                             "seconds": time.monotonic() - started})


def _observed_ingest(envelope, *, original, observations, phase):
    phase["name"] = "host_ingest"
    started = time.monotonic()
    try:
        return original(envelope)
    finally:
        observations.append({"phase": "host_ingest_total", "seconds": time.monotonic() - started})
        phase["name"] = "delivery"


@pytest.mark.parametrize("event", ["user_prompt", "post_tool_use"])
def test_original_host_checkpoint_records_real_acl_costs(tmp_path, monkeypatch, record_property, event):
    from functools import partial

    observations = []
    phase = {"name": "setup"}
    observed = partial(_observed_acl, original=markdown_transaction._run_acl_command,
                       observations=observations, phase=phase)
    monkeypatch.setattr(markdown_transaction, "_run_acl_command", observed)
    observed_ingest = partial(_observed_ingest, original=integration_adapter.ingest_event,
                             observations=observations, phase=phase)
    monkeypatch.setattr(integration_adapter, "ingest_event", observed_ingest)
    started = time.monotonic()
    try:
        original_capture(tmp_path, monkeypatch, event)
    finally:
        record_property("acl_command_observations", json.dumps(observations))
        record_property("original_scenario_seconds", time.monotonic() - started)
