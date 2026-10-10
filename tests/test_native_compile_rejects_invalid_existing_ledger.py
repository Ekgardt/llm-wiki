"""Publication must validate the existing ledger before adding versioned claims."""
import json

import claims
import compile_memory as compiler
import pytest

from tests.test_claim_v2_versioned_consumers import _page, _record


def _incoming():
    record, _ = _record("new")
    return {**record, "id": "new-id"}


def _damaged_page(case):
    old, _ = _record("old", "claim/v1")
    ledger = {"schema_version": "claim-ledger/v1", "claims": [old]}
    changes = {
        "extra_property": {"unexpected_property": "must not be repaired"},
        "unknown": {"schema_version": "claim-ledger/v99"},
        "invalid_mixed": {"claims": [dict(old, schema_version="claim/v2")]},
        "noncanonical": {},
    }
    ledger.update(changes[case])
    raw = claims.claim_json_bytes(ledger)
    if case == "noncanonical":
        raw = json.dumps(ledger, ensure_ascii=False, sort_keys=True).encode()
    return b"# Existing\n\n## Claims\n```json\n" + raw + b"\n```\n"


@pytest.mark.parametrize("case", ["extra_property", "noncanonical", "unknown", "invalid_mixed"])
def test_invalid_existing_ledger_is_refused_before_merge(case):
    original = _damaged_page(case)
    with pytest.raises(ValueError):
        claims.parse_claim_ledger(original)
    with pytest.raises(ValueError):
        compiler._with_claim_ledger(original, [_incoming()])


@pytest.mark.parametrize("version", ["claim-ledger/v1", "claim-ledger/v2"])
def test_valid_existing_ledger_preserves_records_and_adds_v2(version):
    old, _ = _record("old", "claim/v1")
    new = _incoming()
    result = compiler._with_claim_ledger(_page([old], version), [new])
    assert claims.parse_claim_ledger(result) == {"schema_version": "claim-ledger/v2", "claims": [old, new]}


def test_no_existing_ledger_can_receive_first_claim():
    new = _incoming()
    result = compiler._with_claim_ledger(b"# New page\n", [new])
    assert claims.parse_claim_ledger(result)["claims"] == [new]


def test_no_additions_preserve_existing_noop_contract():
    original = _damaged_page("extra_property")
    assert compiler._with_claim_ledger(original, []) == original
