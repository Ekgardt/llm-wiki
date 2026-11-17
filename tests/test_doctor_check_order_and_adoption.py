"""The order checks run in, deferred writes, adoption and deletion certification under the caller's deadline."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import generation_maintenance
import pytest
from llm_wiki import adoption_admission, sqlite_io

from tests.doctor_case_helpers import (
    _moment_days_ago,
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _adopt,
    _build_root,
    _check,
    _qualified_pyright_check,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def _deferrable_check_names() -> list:
    import doctor

    return [
        name
        for name, _operation in doctor.deferrable_checks(
            Path("."), Path("."), Path("."), datetime.now(UTC)
        )
    ]


def _fix_cheap_checks(monkeypatch) -> None:
    """Every check scheduled before the deferrable ones answers ok at once."""
    from llm_wiki import doctor_base, doctor_deletion, doctor_queue, doctor_transactions

    monkeypatch.setattr(doctor_base, "environment_check", _fixed_ok("environment"))
    monkeypatch.setattr(doctor_base, "runtime_check", _fixed_ok("runtime"))
    monkeypatch.setattr(generation_maintenance, "filesystem_check", _fixed_ok("filesystem"))
    monkeypatch.setattr(doctor_transactions, "transaction_check", _fixed_ok("transactions"))
    monkeypatch.setattr(doctor_queue, "queue_check", _fixed_ok("queue"))
    monkeypatch.setattr(doctor_queue, "archive_check", _fixed_ok("archives"))
    monkeypatch.setattr(doctor_deletion, "claim_check", _fixed_ok("claims"))


def test_the_corpus_wide_check_runs_after_the_cheap_ones(monkeypatch):
    """Order is the budget policy: the expensive check must not starve the rest."""
    import doctor
    import generation_maintenance

    order: list[str] = []

    def record(check_id):
        def check(budget):
            order.append(check_id)
            return generation_maintenance.result(check_id, "ok", "ok", {})

        return check

    names = _deferrable_check_names()
    monkeypatch.setattr(
        doctor,
        "deferrable_checks",
        lambda *a, **k: tuple((name, record(name)) for name in names),
    )
    _fix_cheap_checks(monkeypatch)

    doctor.collect_checks(
        Path("."),
        Path("."),
        Path("."),
        datetime.now(UTC),
        time.monotonic() + 8.0,
    )

    assert names[-1] == "generation", "the corpus-wide check must be scheduled last"
    assert order[-1] == "generation"


def _fixed_ok(check_id):
    def check(*_args, **_keywords):
        return generation_maintenance.result(check_id, "ok", "ok", {})

    return check


def test_deferred_writes_are_reported_beside_lost_captures_not_as_them(tmp_path):
    """Issue #26.3: a writer race is retried by the next session, not lost."""
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    (state_root / "run" / "state.json").write_text(
        json.dumps(
            {
                "capture_failures": {
                    "adapter_capture_worker": {
                        "count": 17,
                        "deferred": 17,
                        "last_reason": "OperationalOwnershipError: owner_busy",
                        "last_at": _moment_days_ago(0),
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "capture")

    assert check["status"] == "ok"
    assert check["details"]["lost"] == 0
    assert check["details"]["deferred"] == 17
    assert "17 write(s) were deferred by a writer race" in check["message"]
    del home


def test_the_claim_check_names_the_cause_the_pages_and_the_repair() -> None:
    """Issue #29.5: `requires operator attention` said neither why nor what to do."""
    from llm_wiki import doctor_deletion

    details = {
        "index": "valid",
        "claims": 40,
        "diagnostics": 6,
        "codes": ["evidence_unresolved"],
        "by_code": {"evidence_unresolved": 6},
        "pages": ["knowledge/notes/alarm-thresholds.md", "knowledge/notes/verify-first.md"],
        "read_error": False,
        "deletion_codes": [],
    }

    result = doctor_deletion.claim_result(details)

    assert result["status"] == "degraded"
    assert "6 claim(s) cite daily bytes that no longer resolve" in result["message"]
    assert "knowledge/notes/alarm-thresholds.md, knowledge/notes/verify-first.md" in result["message"]
    assert "doctor.py --repair" in result["message"]


def test_an_adopted_vault_owes_no_v2_queue_migration(tmp_path, monkeypatch):
    """Adoption retires the v2 queue; the doctor must not wait for its marker.

    Found by adopting a test vault for real instead of patching the answer:
    `memory_queue` declines to migrate after adoption, so the marker never
    appears, and the queue check called that a pending migration for ever.
    """
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "queue")

    assert not (state_root / "run" / "queue-migrated-v2").exists()
    assert check["details"]["migration"] == "retired"
    assert check["status"] == "ok"


def test_repair_on_an_adopted_vault_does_not_run_the_retired_v2_migration(tmp_path, monkeypatch):
    """`doctor --repair` aborted on the v2 queue tombstone when the v2 marker was absent."""
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "adopted-only"
    _adopt(root, state_root)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    runtime = _check(report, "runtime")
    assert runtime["details"].get("repair_errors", []) == []
    assert not (state_root / "run" / "queue-migrated-v2").exists()
    assert all(item["action"] != "migrate_queue" for item in report["repaired"])


def test_a_stale_nightly_that_skipped_names_its_reason():
    """Audit OPS-23: the skip reason reaches the message, not only `details`."""
    from llm_wiki.doctor_schedule import (
        stale_nightly_message,
    )

    skipped = {
        "last_nightly_at": "2026-07-10T03:00:00",
        "last_nightly_skip": {"skipped_at": "2026-07-13T03:00:00", "reason": "owner_busy"},
    }
    ran_later = {
        "last_nightly_at": "2026-07-14T03:00:00",
        "last_nightly_skip": {"skipped_at": "2026-07-13T03:00:00", "reason": "owner_busy"},
    }

    assert stale_nightly_message(skipped) == (
        "Nightly maintenance is stale; the last pass skipped: owner_busy (2026-07-13)."
    )
    assert stale_nightly_message(ran_later) == "Nightly maintenance is stale."
    assert stale_nightly_message({}) == "Nightly maintenance is stale."


@pytest.mark.parametrize("deadline", [99.0, 100.0])
def test_expired_deletion_observation_does_not_start_adoption_validation(tmp_path, monkeypatch, deadline):
    from datetime import datetime
    from unittest.mock import Mock

    import doctor
    from llm_wiki import (
        doctor_deletion,
    )

    validation = Mock(side_effect=AssertionError("expired observation started database validation"))
    monkeypatch.setattr(adoption_admission, "require_reliability_v3_adopted", validation)
    monkeypatch.setattr(doctor.time, "monotonic", lambda: 100.0)
    result = doctor_deletion.run_deletion_check(tmp_path, datetime.now(UTC), deadline=deadline)
    assert result["quiescent"] is False
    assert result["blockers"] == [{"code": "run_deletion_state_unknown"}]
    validation.assert_not_called()


def test_adoption_diagnostic_receives_the_collector_deadline(monkeypatch):
    from unittest.mock import Mock

    import doctor
    import generation_maintenance
    from llm_wiki import (
        doctor_repair,
    )

    _fix_cheap_checks(monkeypatch)
    monkeypatch.setattr(doctor, "deferrable_checks", lambda *args: ())
    diagnostic = Mock(return_value=generation_maintenance.result("adoption", "ok", "ok", {}))
    monkeypatch.setattr(doctor_repair, "adoption_check", diagnostic)
    deadline = time.monotonic() + LONG_TIMEOUT
    doctor.collect_checks(Path("."), Path("."), Path("."), datetime.now(UTC), deadline)
    diagnostic.assert_called_once_with(Path("."), Path("."), deadline=deadline)


def test_adoption_certification_keeps_the_callers_validation_deadline(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import markdown_transaction

    validation = Mock()
    monkeypatch.setattr(adoption_admission, "require_reliability_v3_adopted", validation)
    deadline = time.monotonic() + LONG_TIMEOUT
    markdown_transaction.require_adopted_through_contention(tmp_path, tmp_path, deadline=deadline)
    validation.assert_called_once_with(root=tmp_path, state_root=tmp_path, deadline=deadline)


def test_contention_window_does_not_shorten_the_callers_validation_deadline(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import markdown_transaction

    validation = Mock()
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    markdown_transaction.retry_adoption_validation(tmp_path, tmp_path, validation, deadline=200.0)
    validation.assert_called_once_with(root=tmp_path, state_root=tmp_path, deadline=200.0)


@pytest.mark.parametrize("kwargs,clock,incomplete", [({}, 101.0, False), ({"deadline": 100.0}, 99.0, False), ({"deadline": 100.0}, 101.0, True)])
def test_adoption_timeout_is_incomplete_only_after_the_callers_deadline(tmp_path, monkeypatch, kwargs, clock, incomplete):
    from unittest.mock import Mock

    import doctor
    import markdown_transaction
    from llm_wiki import (
        doctor_repair,
    )

    monkeypatch.setattr(markdown_transaction, "reliability_v3_records_present", lambda _: True)
    monkeypatch.setattr(markdown_transaction, "require_adopted_through_contention", Mock(side_effect=TimeoutError("validation timed out")))
    monkeypatch.setattr(doctor_repair, "stray_candidates", lambda _: [])
    monkeypatch.setattr(doctor_repair, "quarantined_candidates", lambda _: 0)
    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock)
    if incomplete:
        result = doctor_repair.adoption_check(tmp_path, tmp_path, **kwargs)
        assert result["status"] == "degraded"
        assert result["details"]["budget_exhausted"] is True
        return
    with pytest.raises(TimeoutError, match="validation timed out"):
        doctor_repair.adoption_check(tmp_path, tmp_path, **kwargs)


def test_deletion_certification_uses_the_deadline_and_retains_unknown_state(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from llm_wiki import (
        doctor_deletion,
    )

    validation = Mock(side_effect=TimeoutError("validation timed out"))
    monkeypatch.setattr(adoption_admission, "require_reliability_v3_adopted", validation)
    deadline = time.monotonic() + LONG_TIMEOUT
    result = doctor_deletion.run_deletion_check(tmp_path, datetime.now(UTC), deadline=deadline)
    validation.assert_called_once_with(root=tmp_path, state_root=tmp_path, deadline=deadline)
    assert result["quiescent"] is False
    assert result["permit"] is False
    assert result["blockers"] == [{"code": "run_deletion_state_unknown"}]


@pytest.mark.parametrize("budget", [None, SHORT_TIMEOUT])
def test_filesystem_probe_uses_doctor_budget_after_slow_sqlite_open(tmp_path, monkeypatch, budget):
    import doctor
    import generation_maintenance
    import reliable_memory

    (tmp_path / "run").mkdir()
    clock = [100.0]
    real_open = sqlite_io.opened_probe

    def slow_open(path, deadline, connections):
        result = real_open(path, deadline, connections)
        clock[0] += 1.1
        return result

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(reliable_memory, "known_network_path", lambda path: False)
    monkeypatch.setattr(sqlite_io, "opened_probe", slow_open)
    kwargs = {} if budget is None else {"deadline": clock[0] + budget}
    result = generation_maintenance.filesystem_check(tmp_path, **kwargs)

    assert result["status"] == "ok", result
    assert list((tmp_path / "run").iterdir()) == []


def test_filesystem_probe_keeps_caller_deadline_during_slow_sqlite_open(tmp_path, monkeypatch):
    import doctor
    import generation_maintenance
    import reliable_memory

    (tmp_path / "run").mkdir()
    clock = [100.0]
    real_open = sqlite_io.opened_probe

    def slow_open(path, deadline, connections):
        result = real_open(path, deadline, connections)
        clock[0] += SHORT_TIMEOUT
        return result

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(reliable_memory, "known_network_path", lambda path: False)
    monkeypatch.setattr(sqlite_io, "opened_probe", slow_open)
    result = generation_maintenance.filesystem_check(tmp_path, deadline=clock[0] + SHORT_TIMEOUT)

    assert result["status"] == "degraded", result
    assert result["details"]["locking"] == "unknown"
    assert list((tmp_path / "run").iterdir()) == []


def test_the_default_vault_root_is_the_checkout_not_the_scripts_directory(monkeypatch):
    from llm_wiki.doctor_repair import resolved_doctor_paths

    monkeypatch.delenv("LLM_WIKI_ROOT", raising=False)
    monkeypatch.delenv("LLM_WIKI_STATE_ROOT", raising=False)
    checkout = Path(__file__).resolve().parents[1]

    root, state, _home = resolved_doctor_paths(None, None, None)

    assert (root, state) == (checkout, checkout)
