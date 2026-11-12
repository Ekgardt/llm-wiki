from __future__ import annotations

import hashlib
import time

import compile_pipeline
import pytest
from llm_wiki import doctor_transactions

from tests.archive_daily_bagit_helpers import archive_vault as archive_vault


def test_a_receipt_filename_alone_cannot_prove_a_day_compiled(archive_vault):
    root, state, daily = archive_vault
    logical = f'knowledge/daily/{daily.name}'
    digest = hashlib.sha256(daily.read_bytes()).hexdigest()
    identity = compile_pipeline.compile_source_identity(logical, digest)
    name = f"knowledge/daily/receipts/v3-{identity}.md"
    assert not (root / name).exists()
    for receipt in (root / "knowledge/daily/receipts").glob("*.md"):
        receipt.unlink()
    supersession = doctor_transactions.CompiledDaySupersession(root, state)
    assert not supersession._day_compiled(logical, {name})


def test_doctor_accepts_an_actual_committed_v4_context(archive_vault):
    root, state, daily = archive_vault
    supersession = doctor_transactions.CompiledDaySupersession(root, state)
    assert supersession._day_compiled(f'knowledge/daily/{daily.name}', set())


def test_an_expired_doctor_cannot_accept_context_authority(archive_vault):
    root, state, daily = archive_vault
    supersession = doctor_transactions.CompiledDaySupersession(root, state, deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError, match="deadline"):
        supersession._day_compiled(f"knowledge/daily/{daily.name}", set())
