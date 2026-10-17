"""An incomplete live-corpus scan does not imply damaged immutable artifacts."""

import hashlib
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tests.slow_machine import LONG_TIMEOUT
from tests.test_generation_maintenance import _generation_directory, _vault


@pytest.mark.parametrize(
    "max_sources,total_bytes,setting,limit",
    [(1, None, "corpus.max_files", 1), (10, 1024, "corpus.max_total_bytes", 1024)],
)
def test_capacity_refusal_keeps_a_valid_generation_explicitly_unverified(
    tmp_path, max_sources, total_bytes, setting, limit
):
    import doctor

    root, state = _vault(tmp_path)
    notes = root / "knowledge" / "notes"
    (notes / "first.md").write_text("---\ntype: concept\n---\n# First\n")
    built = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=LONG_TIMEOUT, max_sources=10
    )
    evidence = _generation_directory(state, built) / "evidence.sqlite3"
    before = hashlib.sha256(evidence.read_bytes()).hexdigest()
    (notes / "second.md").write_text("---\ntype: concept\n---\n# Second\n" + "x" * 2048)
    if total_bytes is not None:
        (root / "llm-wiki.toml").write_text(f"[corpus]\nmax_total_bytes = {total_bytes}\n")

    result = doctor._generation_check(
        root, state, datetime.now(timezone.utc),
        deadline=time.monotonic() + LONG_TIMEOUT, max_sources=max_sources,
    )

    assert result["status"] == "degraded"
    details = result["details"]
    assert details["catalog"] == "valid"
    assert details["active_generation"] == built["generation_id"]
    assert details["capacity_exceeded"] is True
    assert details["limit_name"] == setting
    assert details["configured_limit"] == limit
    assert details["observed_minimum"] > limit
    assert details["freshness"] == "unknown"
    assert details["partial"] is True
    assert details["repairable"] is False
    assert details["recommended_action"] == "review_corpus_budget"
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before


def test_code_bytes_report_the_same_typed_capacity_refusal(tmp_path):
    import corpus_snapshot

    root, _state = _vault(tmp_path)
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "large.py").write_bytes(("#" + "x" * 2048 + "\n").encode("ascii"))

    with pytest.raises(ValueError) as caught:
        corpus_snapshot.collect_corpus(
            root, code_roots=("scripts",), max_total_bytes=1024,
            deadline=time.monotonic() + LONG_TIMEOUT,
        )

    assert type(caught.value).__name__ == "CorpusCapacityExceeded"
    assert caught.value.setting == "corpus.max_total_bytes"
    assert caught.value.limit == 1024
    assert caught.value.observed == 2050


def test_ignored_entries_report_capacity_not_corrupt_sources(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    notes = root / "knowledge/notes"
    (notes / "first.md").write_text("---\ntype: concept\n---\n# First\n")
    built = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=LONG_TIMEOUT, max_sources=10,
    )
    (notes / "ignored.txt").write_text("not a source")
    (root / "llm-wiki.toml").write_text("[corpus]\nmax_files = 1\n")
    result = doctor._generation_check(
        root, state, datetime.now(timezone.utc),
        deadline=time.monotonic() + LONG_TIMEOUT,
    )
    assert result["status"] == "degraded"
    assert result["details"]["active_generation"] == built["generation_id"]
    assert result["details"]["capacity_exceeded"] is True
    assert result["details"]["limit_name"] == "corpus.max_files"
    assert result["details"]["observed_minimum"] == 2
    assert result["details"]["freshness"] == "unknown"
    assert result["details"]["repairable"] is False


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_generation_reports_its_actual_scope_without_an_extra_collection(tmp_path, monkeypatch, newline):
    import doctor

    root, state = _vault(tmp_path)
    note = "---\ntype: concept\n---\n# First\n"
    payload = note.replace("\n", newline).encode()
    (root / "knowledge/notes/first.md").write_bytes(payload)
    doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=LONG_TIMEOUT, max_sources=10,
    )
    daily = root / "knowledge/daily/2026-10-17.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_text("not selected\n" * 100)
    (root / "knowledge/notes/ignored.txt").write_text("not a source")
    (root / "llm-wiki.toml").write_text("[corpus]\nmax_files = 2\n")
    collect = doctor._collect_generation_live_corpus
    calls = []

    def observe(*args):
        calls.append(args)
        return collect(*args)

    monkeypatch.setattr(doctor, "_collect_generation_live_corpus", observe)
    result = doctor._generation_check(
        root, state, datetime.now(timezone.utc),
        deadline=time.monotonic() + LONG_TIMEOUT,
    )
    usage = result["details"]["capacity_usage"]
    assert len(calls) == 1
    assert usage["entries"]["used"] == 2
    assert usage["sources"]["used"] == 1
    assert usage["bytes"]["used"] == len(payload)
    assert usage["knowledge_extraction_sources"]["used"] == 1
    assert result["details"]["capacity_near_ceiling"] == ["entries"]
    assert result["details"]["freshness"] == "fresh"
    assert result["details"]["repairable"] is False
    assert result["status"] == "degraded"


def test_snapshot_without_discovery_observations_does_not_report_zero_capacity(tmp_path):
    from types import SimpleNamespace

    import doctor

    assert doctor._generation_capacity_usage(SimpleNamespace(), tmp_path) is None


def test_deferred_source_read_reports_the_same_capacity_refusal(tmp_path):
    import corpus_snapshot

    root, _state = _vault(tmp_path)
    page = root / "knowledge" / "notes" / "large.md"
    page.write_bytes(("#" + "x" * 2048 + "\n").encode("ascii"))
    snapshot = corpus_snapshot.collect_corpus(root)
    policy = replace(snapshot.policy, max_total_bytes=1024)
    reader = corpus_snapshot._Capture(policy, time.monotonic() + LONG_TIMEOUT, None)
    # Pathname traversal defers reading until this stage; use a real sealed path.
    seal = corpus_snapshot._seal_source_file(root, page, policy.max_depth + 3)
    candidate = corpus_snapshot._Candidate(
        page, "knowledge/notes/large.md", "note", None, seal, None
    )

    with pytest.raises(ValueError) as caught:
        reader.add(candidate)

    assert type(caught.value).__name__ == "CorpusCapacityExceeded"
    assert caught.value.setting == "corpus.max_total_bytes"
    assert caught.value.limit == 1024
    assert caught.value.observed == 2050
