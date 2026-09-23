"""Findings A2, A3, B3, C2, C4 and C6 of the live audit of 2026-09-23.

See `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_guardrails  # noqa: E402
import compile_memory  # noqa: E402
import doctor  # noqa: E402
import integration_adapter  # noqa: E402
import retire_lsp_evidence  # noqa: E402
import scheduled_nightly  # noqa: E402
import session_start_context  # noqa: E402

from tests.test_doctor import _write_lsp_failure, _write_lsp_owner  # noqa: E402


def _own_state(tmp_path: Path, monkeypatch) -> Path:
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(scheduled_nightly, "update_state", memory_state.update_state)
    memory_state.save_state({"last_nightly_status": "success", "last_nightly_date": "2026-09-17"})
    return memory_state.STATE_FILE


# ---------------------------------------------------------------- A2 ----


def test_a_pass_that_cannot_take_its_fence_records_the_failure(tmp_path, monkeypatch) -> None:
    state_file = _own_state(tmp_path, monkeypatch)

    def refuse(role: str):
        raise RuntimeError("reliability_v3_record_invalid")

    monkeypatch.setattr(scheduled_nightly, "take_scheduled_fence", refuse)
    with pytest.raises(RuntimeError):
        scheduled_nightly.main()

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert (state["last_nightly_status"], "reliability_v3_record_invalid" in state["last_nightly_failure"]["error"]) == (
        "failed",
        True,
    )


# ---------------------------------------------------------------- A3 ----


def test_a_truncated_transaction_scan_says_its_counts_are_lower_bounds() -> None:
    """Growth past the read ceiling is not a health problem; a whole-truth claim is."""
    details = {"truncated_scans": ["transaction_scan_truncated"]}
    status, message = doctor._truncated_scan_verdict(details, "ok", "Transaction state is healthy.")
    assert (status, "lower bound" in message) == ("ok", True)


def test_a_complete_scan_keeps_its_verdict() -> None:
    assert doctor._truncated_scan_verdict({"truncated_scans": []}, "ok", "fine") == ("ok", "fine")


def test_the_state_bound_admits_what_the_writer_can_produce() -> None:
    """40 pending items per project over 91 projects made 354 KiB on 2026-09-23."""
    assert doctor.MAX_STATE_BYTES >= 4 * 1024 * 1024


# ---------------------------------------------------------------- B3 ----


# ---------------------------------------------------------------- C4 ----


def test_pending_checkpoint_events_older_than_thirty_days_are_dropped_and_noted(monkeypatch) -> None:
    noted: list[tuple[str, int]] = []
    monkeypatch.setattr(integration_adapter, "_note_expired_events", lambda slug, n: noted.append((slug, n)))
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=45)).isoformat()
    young = (now - timedelta(days=2)).isoformat()
    pending = {
        "stale": [{"event_id": "a", "occurred_at": old}],
        "mixed": [{"event_id": "b", "occurred_at": old}, {"event_id": "c", "occurred_at": young}],
        "fresh": [{"event_id": "d", "occurred_at": young}],
    }

    integration_adapter._expire_pending_events(pending)

    assert (sorted(pending), [i["event_id"] for i in pending["mixed"]], sorted(noted)) == (
        ["fresh", "mixed"],
        ["c"],
        [("mixed", 1), ("stale", 1)],
    )


# ---------------------------------------------------------------- C6 ----


def test_daily_logs_are_counted_at_the_top_level_only(tmp_path, monkeypatch) -> None:
    daily = tmp_path / "daily"
    (daily / "receipts").mkdir(parents=True)
    (daily / "2026-09-14.md").write_text("# log\n", encoding="utf-8")
    (daily / "README.md").write_text("# readme\n", encoding="utf-8")
    (daily / "receipts" / "v3-abc.md").write_text("# receipt\n", encoding="utf-8")
    monkeypatch.setattr(session_start_context, "DAILY_DIR", daily)

    assert session_start_context._count_daily_logs() == 1


def test_a_wrapped_one_sentence_summary_is_joined_and_stops_at_the_paragraph() -> None:
    content = (
        "# Title\n\nOne-sentence summary: a daily log larger than the compile input budget should be\n"
        "split at entry boundaries and compiled as parts,\nrather than skipped.\n\n## Next\n- bullet\n"
    )
    assert build_guardrails._summary_of(content) == (
        "a daily log larger than the compile input budget should be split at entry "
        "boundaries and compiled as parts, rather than skipped."
    )


def test_a_dry_run_moves_no_clock(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(compile_memory, "_mark_started", lambda trigger: calls.append("started"))
    monkeypatch.setattr(compile_memory, "_mark_finished", lambda *a, **k: calls.append("finished"))
    dry = argparse.Namespace(dry_run=True, trigger="manual")
    wet = argparse.Namespace(dry_run=False, trigger="manual")

    compile_memory._mark_started_unless_dry(dry)
    compile_memory._mark_ok_unless_dry(dry)
    compile_memory._mark_error_unless_dry(dry, RuntimeError("x"))
    compile_memory._mark_started_unless_dry(wet)
    compile_memory._mark_ok_unless_dry(wet)

    assert calls == ["started", "finished"]


# ---------------------------------------------------------------- C2 ----


def _failure_root(state_root: Path, index: int, age_days: float, now: datetime) -> Path:
    nonce = f"{index:032x}"
    started = now - timedelta(days=age_days, minutes=1)
    owner = _write_lsp_owner(
        state_root, owner_nonce=nonce, generation_nonce="f" * 32, started_at=started, owner_pid=999_999
    )
    _write_lsp_failure(owner, owner_nonce=nonce, generation_nonce="f" * 32, timestamp=now - timedelta(days=age_days), server_pid=999_999)
    return owner


def test_old_failure_roots_beyond_the_newest_twenty_are_retired(tmp_path) -> None:
    state_root = tmp_path / "state"
    now = datetime.now(timezone.utc)
    roots = [_failure_root(state_root, index, 30.0 - index * 0.1, now) for index in range(25)]
    young = _failure_root(state_root, 99, 1.0, now)

    removed = retire_lsp_evidence.retire(state_root, now)

    assert (removed, young.exists(), sum(root.exists() for root in roots)) == (6, True, 19)


def test_young_evidence_is_kept_even_beyond_twenty(tmp_path) -> None:
    state_root = tmp_path / "state"
    now = datetime.now(timezone.utc)
    roots = [_failure_root(state_root, index, 2.0, now) for index in range(25)]

    assert (retire_lsp_evidence.retire(state_root, now), all(r.exists() for r in roots)) == (0, True)


# ---------------------------------------------------------------- D1 ----


def test_consolidation_gives_its_provider_the_compile_ceiling(monkeypatch) -> None:
    import episode_consolidation
    import llm_client

    seen: list[int | None] = []

    def fake_call(prompt, system_prompt, max_tokens=0):
        seen.append(llm_client._CALL_CEILING_S)
        return "ok"

    monkeypatch.setattr(llm_client, "call_llm", fake_call)

    assert (episode_consolidation._call_provider("records"), seen) == ("ok", [300])


# ---------------------------------------------------------------- D2 ----


def test_a_pre_era_pyright_receipt_is_retired_and_the_release_installs_fresh(tmp_path, monkeypatch) -> None:
    import pyright_profile
    from install_pyright import install_pyright
    from reliable_memory import canonical_json_bytes

    from tests.test_install_pyright import _artifact, _root

    state_root = tmp_path / "state"
    artifact = _artifact(tmp_path, monkeypatch)
    install_pyright(state_root=state_root, artifact=artifact.path)
    root = _root(state_root)
    manifest = root / "install-manifest.json"
    receipt = json.loads(manifest.read_text(encoding="utf-8"))
    receipt.pop("executed_tree_sha256")
    assert set(receipt) == pyright_profile._MANIFEST_KEYS_BEFORE_TREE_DIGEST
    manifest.write_bytes(canonical_json_bytes(receipt))

    result = install_pyright(state_root=state_root, artifact=artifact.path)

    siblings = sorted(p.name for p in root.parent.iterdir() if not p.name.startswith("."))
    fresh = json.loads(manifest.read_text(encoding="utf-8"))
    assert (result.version, siblings, "executed_tree_sha256" in fresh) == ("1.1.411", ["1.1.411"], True)


def test_a_directory_under_dist_does_not_invalidate_a_valid_install(tmp_path, monkeypatch) -> None:
    """pyright ships `dist/typeshed-fallback/`; re-validation read it as a file."""
    import tarfile

    from install_pyright import install_pyright

    from tests.code_kernel_helpers import (
        PyrightTarEntry,
        _default_pyright_entries,
        create_pyright_install_artifact,
        use_pyright_install_artifact_identity,
    )

    server = b"synthetic pyright language server\n"
    entries = (
        *_default_pyright_entries(None, server, True, True),
        PyrightTarEntry("package/dist/typeshed-fallback/", b"", tarfile.DIRTYPE),
        PyrightTarEntry("package/dist/typeshed-fallback/builtins.pyi", b"class object: ...\n"),
    )
    artifact = create_pyright_install_artifact(tmp_path / "pyright.tgz", entries=entries)
    use_pyright_install_artifact_identity(monkeypatch, artifact)
    state_root = tmp_path / "state"

    first = install_pyright(state_root=state_root, artifact=artifact.path)
    second = install_pyright(state_root=state_root, artifact=artifact.path)

    assert (first.manifest_sha256 == second.manifest_sha256, second.version) == (True, "1.1.411")
