"""Generic captured JSON stays physical data; native identities stay strict."""
import json
from functools import partial

import compile_memory
import fact_keys
import pytest
from event_envelope import build_event_envelope, native_encoding_candidate, native_user_text
from integration_adapter import _breadcrumb_input

from tests import test_breadcrumb_archived_journal as archived_journal
from tests import test_breadcrumb_terminal_proof as terminal_proof
from tests.test_event_envelope import FIXED_TIME
from tests.test_native_user_frames_keep_their_physical_evidence import _captured_vault


def _record():
    envelope = build_event_envelope(event_type="user_prompt", payload={"prompt": "Complete user prompt."},
                                    occurred_at=FIXED_TIME, captured_at=FIXED_TIME, agent="codex")
    return _breadcrumb_input(envelope)


def _encoded(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize("record", [
    {"prompt": "complete prompt"}, {"agent": "ordinary"},
    {"data": {"event_type": "ordinary", "schema_version": "example/v1"}},
    {"schema_version": "1.0", "data": "ordinary"},
    {"event_type": "user_prompt", "data": "ordinary"},
])
def test_generic_complete_json_is_not_a_native_frame(record):
    encoded = _encoded(record)
    assert not native_encoding_candidate(encoded)
    assert native_user_text("    " + encoded) is None


def test_actual_host_envelope_preserves_exact_user_prompt():
    record = _record()
    assert native_encoding_candidate(_encoded(record))
    assert native_user_text("    " + _encoded(record)) == record["payload"]["prompt"]


@pytest.mark.parametrize("missing", [(field,) for field in _record()] + [("event_type", "schema_version")])
def test_missing_native_fields_remain_visible_refusals(missing):
    record = _record()
    for field in missing:
        del record[field]
    with pytest.raises(ValueError):
        native_user_text("    " + _encoded(record))


@pytest.mark.parametrize("change", [{"schema_version": "future"}, {"payload": {"prompt": 4}},
    {"event_type": "future"}, {"extra": "not permitted"}])
def test_unknown_or_invalid_native_records_remain_refused(change):
    record = _record()
    record.update(change)
    with pytest.raises(ValueError):
        native_user_text("    " + _encoded(record))


def test_outer_discriminator_pair_is_native_shaped_even_when_incomplete():
    encoded = _encoded({"schema_version": "example/v1", "event_type": "ordinary"})
    assert native_encoding_candidate(encoded)
    with pytest.raises(ValueError, match="fields"):
        native_user_text("    " + encoded)


@pytest.mark.parametrize("encoded", ['{"agent":"codex","event_type":"user_prompt",',
    _encoded(_record()).replace('"event_type":"user_prompt"', '"event_type":"stop","event_type":"user_prompt"'),
    json.dumps(_record(), sort_keys=True)])
def test_malformed_duplicate_or_noncanonical_native_is_not_silent_zero(encoded):
    with pytest.raises(ValueError):
        native_user_text("    " + encoded)


@pytest.mark.parametrize("daily", [True, False])
@pytest.mark.parametrize("record", [{"agent": "ordinary"},
    {"data": {"event_type": "ordinary", "schema_version": "example/v1"}},
    {"agent": "ordinary", "text": "x" * 1100000}])
def test_verified_generic_breadcrumb_never_manufactures_user_facts(tmp_path, record, daily):
    snapshot = _captured_vault(tmp_path, _encoded(record), daily=daily)
    assert snapshot.sources and snapshot.chunks
    assert fact_keys.user_turns(snapshot.chunks, sources=snapshot.sources) == []


def _compile_host_for_archive(coordinator, daily):
    inputs = compile_memory.snapshot_compile_inputs([daily])
    batch = compile_memory.pack_compile_batches(inputs, model=None)[0]
    compile_memory.apply_compile_plan(
        batch.inputs, {"schema_version": "compile-plan/v2", "operations": []},
        action_key="d" * 64, trigger="manual", coordinator=coordinator,
        completed_at="2026-09-29T00:00:00Z", batch=batch,
        provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
    )


@pytest.mark.parametrize("scenario", [
    archived_journal.test_sealed_bag_is_accepted_after_the_flat_journal_is_archived,
    archived_journal.test_present_conflicting_journal_does_not_fall_back_to_archive,
    archived_journal.test_delayed_worker_does_not_recreate_a_journal_already_in_an_archive,
])
def test_actual_host_envelope_preserves_every_archived_journal_guard(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(terminal_proof, "_publish", partial(terminal_proof._publish, event=_record()))
    monkeypatch.setattr(archived_journal, "_compile_for_archive", _compile_host_for_archive)
    scenario(tmp_path, monkeypatch)
