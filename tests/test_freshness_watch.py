"""CODE-04: bounded daytime freshness — the observer, not the builder.

Every test here builds a real generation with the product's own maintenance
entry point and then asks the watcher what it sees. Nothing is stubbed except
time and, in the two tests that prove a tier was skipped, the tier itself.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

NANOS = 1_000_000_000


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    return root, state


def _note(root: Path, name: str, body: str) -> Path:
    path = root / "knowledge" / "notes" / name
    path.write_text(
        f"---\ntype: concept\nstatus: active\n---\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _build(root: Path, state: Path) -> dict:
    import doctor

    result = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=120, max_sources=200
    )
    assert result["status"] == "built", result
    return result


def _age_sources(root: Path, seconds: float) -> None:
    """Push every source's timestamp back, as an untouched vault would look."""
    when = time.time() - seconds
    for path in root.rglob("*.md"):
        os.utime(path, (when, when))


def test_the_probe_sees_what_the_collector_sees_and_notices_a_touch(tmp_path):
    import freshness_watch
    from corpus_snapshot import probe_corpus_identity

    root, _state = _vault(tmp_path)
    _note(root, "one.md", "first")
    _note(root, "two.md", "second")

    first = probe_corpus_identity(root, code_roots=freshness_watch.approved_code_roots(root))
    assert {entry[0] for entry in first.entries} >= {
        "knowledge/notes/one.md",
        "knowledge/notes/two.md",
    }
    assert first.file_count == len(first.entries)

    later = time.time() + 5
    os.utime(root / "knowledge" / "notes" / "one.md", (later, later))
    second = probe_corpus_identity(root, code_roots=freshness_watch.approved_code_roots(root))

    assert second.entries != first.entries
    assert second.newest_mtime_ns > first.newest_mtime_ns


def test_a_settled_generation_is_fresh_without_paying_for_a_snapshot(tmp_path, monkeypatch):
    import freshness_watch

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")
    built = _build(root, state)
    _age_sources(root, freshness_watch.BUILD_SKEW_SECONDS + 3600)

    def _refuse(*_args, **_kwargs):
        raise AssertionError("tier 1 must not run when nothing is newer")

    monkeypatch.setattr(freshness_watch, "collect_corpus", _refuse)
    verdict = freshness_watch.check_freshness(root, state)

    assert verdict.status == "fresh"
    assert verdict.reason == "nothing_written_since_the_generation"
    assert verdict.tier == 0
    assert verdict.generation_id == built["generation_id"]


def test_a_vault_written_to_moments_ago_is_busy_rather_than_stale(tmp_path, monkeypatch):
    import freshness_watch

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")
    _build(root, state)
    _note(root, "two.md", "written just now")

    def _refuse(*_args, **_kwargs):
        raise AssertionError("tier 1 must not run inside the quiet window")

    monkeypatch.setattr(freshness_watch, "collect_corpus", _refuse)
    verdict = freshness_watch.check_freshness(root, state, quiet_window_seconds=600.0)

    assert verdict.status == "busy"
    assert verdict.reason == "written_within_the_quiet_window"
    assert verdict.tier == 0


def test_a_file_touched_without_a_byte_changing_is_fresh_not_stale(tmp_path):
    """Tier 0's known false positive, absorbed by the digests tier 1 uses."""
    import freshness_watch

    root, state = _vault(tmp_path)
    note = _note(root, "one.md", "first")
    _build(root, state)
    when = time.time() + 1
    os.utime(note, (when, when))

    verdict = freshness_watch.check_freshness(
        root, state, quiet_window_seconds=0.0, now_ns=int((when + 600) * NANOS)
    )

    assert verdict.status == "fresh"
    assert verdict.reason == "no_source_delta"
    assert verdict.tier == 1
    assert verdict.delta == 0


def test_a_new_note_left_alone_becomes_stale_and_the_bounded_step_clears_it(tmp_path):
    """The gate in miniature: stale appears, one step runs, stale is gone."""
    import freshness_watch

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")
    first = _build(root, state)
    _note(root, "two.md", "written during the day")

    settled = int((time.time() + 600) * NANOS)
    before = freshness_watch.check_freshness(root, state, now_ns=settled)
    assert (before.status, before.reason, before.delta) == ("stale", "source_delta", 1)

    outcome = freshness_watch.refresh_if_quiet(
        root, state, refresh_budget_seconds=180.0, now_ns=settled
    )
    refreshed = outcome["refresh"]
    assert (refreshed["status"], refreshed["generation_id"] != first["generation_id"]) == (
        "built",
        True,
    )

    after = freshness_watch.check_freshness(
        root, state, now_ns=int((time.time() + 600) * NANOS)
    )
    assert (after.status, after.delta) == ("fresh", 0)


def test_a_busy_vault_is_never_handed_to_the_builder(tmp_path, monkeypatch):
    import freshness_watch

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")
    _build(root, state)
    _note(root, "two.md", "written just now")

    def _refuse(*_args, **_kwargs):
        raise AssertionError("a busy vault must not start a build")

    monkeypatch.setattr(freshness_watch, "_refresh", _refuse)
    outcome = freshness_watch.refresh_if_quiet(root, state, quiet_window_seconds=600.0)

    assert outcome["refresh"] is None
    assert outcome["verdict"]["status"] == "busy"


def test_a_vault_with_no_generation_says_so_and_builds_nothing(tmp_path, monkeypatch):
    import freshness_watch

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")

    def _refuse(*_args, **_kwargs):
        raise AssertionError("there is nothing to refresh yet")

    monkeypatch.setattr(freshness_watch, "_refresh", _refuse)
    outcome = freshness_watch.refresh_if_quiet(root, state)

    assert outcome["verdict"]["status"] == "no_generation"
    assert outcome["verdict"]["reason"] == "no_active_generation"
    assert outcome["refresh"] is None


def test_a_corpus_written_to_mid_probe_is_reported_busy_not_stale(tmp_path, monkeypatch):
    import freshness_watch
    from corpus_snapshot import CorpusChanged

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")
    _build(root, state)

    def _changed(*_args, **_kwargs):
        raise CorpusChanged("corpus source changed during collection")

    monkeypatch.setattr(freshness_watch, "probe_corpus_identity", _changed)
    verdict = freshness_watch.check_freshness(root, state)

    assert verdict.status == "busy"
    assert verdict.reason == "written_during_the_probe"


def test_the_session_stops_at_its_wall_budget_and_leaves_nothing_running(tmp_path, monkeypatch):
    import threading

    import freshness_watch

    root, state = _vault(tmp_path)
    clock = {"now": 0.0}
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(
        freshness_watch,
        "refresh_if_quiet",
        lambda *_a, **_k: {"verdict": {"status": "busy", "cost_seconds": 0.01}, "refresh": None},
    )
    threads_before = threading.active_count()
    report = freshness_watch.run_watch_session(
        root,
        state,
        wall_budget_seconds=100.0,
        probe_interval_seconds=30.0,
        sleep=_sleep,
        monotonic=lambda: clock["now"],
    )

    assert (report.stopped_by, report.probes, slept, report.refreshes) == (
        "wall_budget",
        4,
        [30.0, 30.0, 30.0],
        [],
    )
    assert threading.active_count() == threads_before


def test_the_session_stops_when_its_probing_has_spent_its_cpu_budget(tmp_path, monkeypatch):
    import freshness_watch

    root, state = _vault(tmp_path)
    clock = {"now": 0.0}
    cost = freshness_watch.SESSION_PROBE_CPU_BUDGET_SECONDS / 2

    monkeypatch.setattr(
        freshness_watch,
        "refresh_if_quiet",
        lambda *_a, **_k: {"verdict": {"status": "busy", "cost_seconds": cost}, "refresh": None},
    )
    report = freshness_watch.run_watch_session(
        root,
        state,
        wall_budget_seconds=10_000.0,
        probe_interval_seconds=1.0,
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        monotonic=lambda: clock["now"],
    )

    assert report.stopped_by == "probe_cpu_budget"
    assert report.probes == 2
    assert report.probe_cpu_seconds == pytest.approx(
        freshness_watch.SESSION_PROBE_CPU_BUDGET_SECONDS
    )


def test_the_session_stops_after_its_refresh_cap(tmp_path, monkeypatch):
    import freshness_watch

    root, state = _vault(tmp_path)
    clock = {"now": 0.0}

    monkeypatch.setattr(
        freshness_watch,
        "refresh_if_quiet",
        lambda *_a, **_k: {
            "verdict": {"status": "stale", "cost_seconds": 0.01},
            "refresh": {"status": "built", "cpu_seconds": 5.0},
        },
    )
    report = freshness_watch.run_watch_session(
        root,
        state,
        wall_budget_seconds=10_000.0,
        probe_interval_seconds=1.0,
        max_refreshes=2,
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        monotonic=lambda: clock["now"],
    )

    assert report.stopped_by == "refresh_cap"
    assert len(report.refreshes) == 2
    assert report.refresh_cpu_seconds == pytest.approx(10.0)


def test_a_refresh_that_lost_its_race_still_counts_against_the_cap(tmp_path, monkeypatch):
    """A `corpus_changed` refusal arrives after most of the budget was spent."""
    import freshness_watch

    root, state = _vault(tmp_path)
    clock = {"now": 0.0}

    monkeypatch.setattr(
        freshness_watch,
        "refresh_if_quiet",
        lambda *_a, **_k: {
            "verdict": {"status": "stale", "cost_seconds": 0.01},
            "refresh": {
                "status": "deferred",
                "reason": "corpus_changed",
                "cpu_seconds": 700.0,
            },
        },
    )
    report = freshness_watch.run_watch_session(
        root,
        state,
        wall_budget_seconds=10_000.0,
        probe_interval_seconds=1.0,
        max_refreshes=1,
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        monotonic=lambda: clock["now"],
    )

    assert (report.stopped_by, len(report.refreshes), report.probes) == (
        "refresh_cap",
        1,
        1,
    )


def test_the_command_line_check_never_writes(tmp_path, capsys):
    import json

    import freshness_watch

    root, state = _vault(tmp_path)
    _note(root, "one.md", "first")
    _build(root, state)
    before = sorted(path.name for path in (root / "knowledge" / "notes").iterdir())

    exit_code = freshness_watch.main(
        ["--root", str(root), "--state-root", str(state), "--check"]
    )
    reported = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert reported["status"] in {"fresh", "busy", "stale"}
    assert sorted(path.name for path in (root / "knowledge" / "notes").iterdir()) == before
