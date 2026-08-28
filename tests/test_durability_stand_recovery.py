"""The stand's assertions after NEW-113/114/115: the wedges recover.

`tests/test_durability_stand.py` pinned the wedged behaviour the stand found
on 2026-08-28 — a worker killed while classifying left the capture
`content-partial` behind an orphaned-fence FOREIGN KEY refusal. The ownership
reclaim fix closed that, so the pin is now a pin on a defect. Measured here
instead: the same kill lands in one recovery run.

The producer-side kill is kept as a still-open case with its real reason —
the capture task does not exist yet, so no worker can adopt it. That is a
recovery-path gap, not an ownership wedge, and it stays visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
for directory in (BENCHMARK_DIR, SCRIPTS_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from durability_stand import TrialSpec, run_trial  # noqa: E402


def test_a_worker_killed_while_classifying_now_lands_in_one_recovery(
    tmp_path: Path,
) -> None:
    result = run_trial(
        TrialSpec("classifier", "before"), tmp_path / "trial", "content-marker"
    )
    assert (result.outcome, result.kill_observed) == ("landed", True)
    assert result.recovery_runs == 1
    assert result.evidence["session_record"] is True


def test_a_producer_killed_before_the_task_exists_stays_visibly_unfinished(
    tmp_path: Path,
) -> None:
    """No task to adopt yet — the intent stays durable and the failure named."""
    result = run_trial(
        TrialSpec("enqueue", "before"), tmp_path / "trial", "wedge-marker"
    )
    assert (result.outcome, result.kill_observed) == ("named-failure", True)
    assert result.evidence["intents"]
