"""A semantic fact can have multiple immutable observations."""
import copy

import pytest

from tests.test_compile_claims_producer import (
    SECOND_QUOTE,
    _candidate,
    _daily,
    _evidence,
    _operation,
)
from tests.test_compile_claims_producer import vault as vault


def test_distinct_observations_share_semantics_but_not_identity(vault):
    import compile_memory as compiler

    root, _ = vault
    inputs = compiler.snapshot_compile_inputs([_daily(root)])
    first = _operation()
    second = _operation()
    second["evidence"] = [_evidence(SECOND_QUOTE)]
    a = compiler._derived_claim(first, _candidate(), inputs)
    b = compiler._derived_claim(second, _candidate(), inputs)
    assert a["fingerprint"] == b["fingerprint"]
    assert a["id"] != b["id"]
    assert compiler._merged_claims([a], [b]) == [a, b]


def test_an_identical_record_is_retained_once(vault):
    import compile_memory as compiler

    inputs = compiler.snapshot_compile_inputs([_daily(vault[0])])
    record = compiler._derived_claim(_operation(), _candidate(), inputs)
    assert compiler._merged_claims([record], [copy.deepcopy(record)]) == [record]


def test_same_id_with_changed_record_still_refuses(vault):
    import compile_memory as compiler

    inputs = compiler.snapshot_compile_inputs([_daily(vault[0])])
    record = compiler._derived_claim(_operation(), _candidate(), inputs)
    changed = dict(record, authority="user")
    with pytest.raises(ValueError, match="claim id already exists"):
        compiler._merged_claims([record], [changed])


def test_occurrence_identity_is_stable_and_includes_the_whole_reference(vault):
    import compile_memory as compiler

    inputs = compiler.snapshot_compile_inputs([_daily(vault[0])])
    record = compiler._derived_claim(_operation(), _candidate(), inputs)
    repeat = compiler._derived_claim(_operation(), _candidate(), inputs)
    assert repeat == record
    binding = {"reference": record["evidence"]["reference"], "quote_sha256": record["evidence"]["sha256"]}
    changed = dict(binding, reference=binding["reference"].replace("sha256:", "sha256:0"))
    assert compiler._claim_occurrence_id("2026-07-14", record["fingerprint"], changed) != record["id"]
    assert compiler._claim_occurrence_id("2026-07-15", record["fingerprint"], binding) != record["id"]


def test_legacy_saved_record_is_not_renamed_or_duplicated(vault):
    import compile_memory as compiler

    inputs = compiler.snapshot_compile_inputs([_daily(vault[0])])
    record = compiler._derived_claim(_operation(), _candidate(), inputs)
    record.update(id="claim-2026-07-14-" + record["fingerprint"][:32], extractor_version="compile-claim/v1")
    before = copy.deepcopy(record)
    assert compiler._merged_claims([record], [copy.deepcopy(record)]) == [before]


def test_different_observation_days_keep_their_own_identity(vault):
    import compile_memory as compiler
    from claims import validate_claim_record

    root, _ = vault
    first = compiler._derived_claim(_operation(), _candidate(), compiler.snapshot_compile_inputs([_daily(root)]))
    next_day = _daily(root, "2026-07-15.md")
    operation = _operation()
    operation["evidence"][0]["daily_date"] = "2026-07-15"
    second = compiler._derived_claim(operation, _candidate(), compiler.snapshot_compile_inputs([next_day]))
    validate_claim_record(second)
    assert first["id"] != second["id"]
    assert second["observed_at"] == "2026-07-15T10:00:00Z"


def test_new_cache_identity_preserves_the_old_saved_plan(tmp_path):
    import compile_memory as compiler
    from compile_cache import CompileCache

    from tests.test_compile_cache import _action, _plan

    old = _action()
    new = compiler._action_descriptor(old.sources, old.draft_calls[0], old.critique_calls, critique=True)
    cache = CompileCache(tmp_path)
    plan = _plan()
    path = cache.put(old, plan)
    before = path.read_bytes()
    assert cache.key(old) != cache.key(new)
    assert cache.get(new, lambda item: item == plan) is None
    assert cache.get(old, lambda item: item == plan) == plan
    assert path.read_bytes() == before
