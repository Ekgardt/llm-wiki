"""Tests for the zero-silent-loss durability stand (benchmark/run_durability.py).

Three layers:
1. The stage table names real product boundaries — a renamed product function
   must fail here, not silently arm nothing (that failure mode was observed
   while building the stand: wrapping the legacy `MemoryQueue` class armed no
   kill at all).
2. The outcome classifier can actually emit `silent-loss` — otherwise a
   zero-losses report would be vacuous.
3. Full killed trials through the real subprocess chain land where the
   measured map says they land, with the kill observed (rc -9).
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from durability_child import PRODUCER_STAGES, STAGE_TARGETS  # noqa: E402
from durability_stand import TrialSpec, classify, kill_points, run_trial  # noqa: E402

REQUIRED_STAGES = {
    "publish-intent",
    "enqueue",
    "claim",
    "record-write",
    "classifier",
    "markdown-commit",
    "terminal-publish",
}


def _resolve_target(module_name: str, dotted: str) -> object:
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    target: object = __import__(module_name)
    for head in dotted.split("."):
        target = getattr(target, head)
    return target


def test_the_stage_table_names_required_stages_and_real_boundaries() -> None:
    unresolved = []
    for stage, (module_name, dotted) in STAGE_TARGETS.items():
        target = _resolve_target(module_name, dotted)
        if not callable(target):
            unresolved.append(stage)
    assert REQUIRED_STAGES <= set(STAGE_TARGETS)
    assert unresolved == []


def test_kill_points_cover_both_processes_before_and_after() -> None:
    points = kill_points()
    worker_stages = set(STAGE_TARGETS) - PRODUCER_STAGES
    assert PRODUCER_STAGES and worker_stages
    assert len(points) == 2 * len(STAGE_TARGETS)


def _evidence(**overrides: object) -> dict:
    evidence: dict = {
        "tasks": [],
        "intents": [],
        "intent_files": 0,
        "terminals": [],
        "terminals_verified": [],
        "daily_blocks": 0,
        "session_record": False,
        "failure_reasons": [],
        "quarantined": 0,
        "transcript_present": False,
    }
    evidence.update(overrides)
    return evidence


def test_the_classifier_can_actually_say_silent_loss() -> None:
    """Nothing durable, source consumed — the property's only failure mode."""
    gone = classify(_evidence())
    false_success = classify(
        _evidence(terminals=[{"disposition": {"kind": "markdown_committed"}}],
                  terminals_verified=[False])
    )
    assert (gone, false_success) == ("silent-loss", "silent-loss")


def test_every_durable_trace_defeats_the_silent_loss_verdict() -> None:
    verdicts = (
        classify(_evidence(transcript_present=True)),
        classify(_evidence(intents=[{"intent_id": "a" * 64, "publication_state": "ready"}])),
        classify(_evidence(failure_reasons=["IntegrityError: boom"])),
        classify(_evidence(session_record=True)),
    )
    assert verdicts == ("source-only", "pending-visible", "named-failure", "content-partial")


def test_a_clean_trial_lands_completely(tmp_path: Path) -> None:
    result = run_trial(TrialSpec(None, "before"), tmp_path / "trial", "clean-marker")
    landed_shape = (result.outcome, result.recovery_runs, result.kill_observed)
    assert landed_shape == ("landed", 0, False)
    assert result.evidence["daily_blocks"] == 1
    assert result.evidence["session_record"] is True


def test_a_kill_after_durable_publication_recovers_to_landed(tmp_path: Path) -> None:
    """Producer dies once its fenced publication committed; the queue replays."""
    result = run_trial(TrialSpec("publish-return", "after"), tmp_path / "trial", "replay-marker")
    assert (result.outcome, result.kill_observed) == ("landed", True)
    assert result.recovery_runs == 1


def test_a_dead_producer_mid_publication_now_recovers_to_landed(
    tmp_path: Path,
) -> None:
    """Killed inside the publication fence, before the task existed: adopted.

    This pinned a named failure until 2026-08-28. The named trace was real and
    the reason it gave was right — the dead producer's ownership row is
    reclaimed only under its own (role, scope) — but the outcome it pinned was
    the recovery-path gap of `NEW-136`, not a contract: the intent was durable,
    carried the whole record, and no code path anywhere looked for an intent
    with no task. `scripts/capture_adoption.py` is that path, so the trial now
    lands. The intent is still visible; it is no longer only visible.
    """
    result = run_trial(TrialSpec("enqueue", "before"), tmp_path / "trial", "wedge-marker")
    assert (result.outcome, result.kill_observed) == ("landed", True)
    assert result.evidence["intents"]
    assert result.evidence["daily_blocks"] == 1


# The classifier-kill case moved to tests/test_durability_stand_recovery.py on
# 2026-08-28: it pinned the D1 wedge (content-partial behind an orphaned-fence
# FOREIGN KEY refusal), and the ownership reclaim fix closed that wedge. The
# trial now lands in one recovery run, which the new file asserts.
