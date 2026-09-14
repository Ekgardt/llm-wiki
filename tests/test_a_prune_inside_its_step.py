"""A prune pass has one deadline, and ends before the step that runs it is killed.

Each removal used to get a fresh 1 200 s under a 300 s nightly kill, and the episode
step left one minute for a model call of up to 90 s. Research:
`docs/research/2026-09-14-a-prune-inside-its-step.md`,
`docs/research/2026-09-14-every-budget-inside-its-step.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
TESTS = Path(__file__).resolve().parent
for directory in (SCRIPTS, TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_prune_generations import _catalog, _chain, _present  # noqa: E402


def test_a_spent_budget_defers_the_removal_and_is_not_a_failure(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])

    lines = prune_generations.prune_generations(state_root=catalog.state_root, apply=True, budget_seconds=0)

    deferred = [line for line in lines if line.startswith("DEFERRED:")]
    assert (_present(catalog, ["gen-1"]), deferred) == ([True], ["DEFERRED: gen-1: the pass's budget is spent"])
    assert prune_generations._count_prefixed(lines, "ERROR:") == 0


def _budget_and_timeout(command: list[str], timeout: int) -> tuple[float, int]:
    return float(command[command.index("--budget-seconds") + 1]), timeout


def _prune_steps() -> list[tuple[list[str], int]]:
    import scheduled_nightly
    import scheduled_weekly

    nightly = [(step.command, step.timeout) for step in scheduled_nightly._post_compile_steps()]
    weekly = [(command, timeout) for _m, _l, command, timeout in scheduled_weekly._script_steps()]
    return [(command, timeout) for command, timeout in nightly + weekly if "prune_generations.py" in command[1]]


def test_every_step_that_prunes_gives_it_a_budget_below_its_kill_timeout():
    pairs = [_budget_and_timeout(command, timeout) for command, timeout in _prune_steps()]

    assert len(pairs) == 2
    assert all(budget < timeout for budget, timeout in pairs)


def _budgeted_steps() -> list[tuple[list[str], int]]:
    import scheduled_nightly
    import scheduled_weekly

    nightly = [scheduled_nightly._episode_step(), *scheduled_nightly._post_compile_steps()]
    weekly = [(command, timeout) for _m, _l, command, timeout in scheduled_weekly._script_steps()]
    pairs = [(step.command, step.timeout) for step in nightly] + weekly
    return [(command, timeout) for command, timeout in pairs if "--budget-seconds" in command]


def test_every_budgeted_step_leaves_the_start_margin_before_its_kill():
    import scheduled_nightly

    margin = scheduled_nightly.STEP_START_MARGIN_SECONDS
    short = [
        command[1]
        for command, timeout in _budgeted_steps()
        if _budget_and_timeout(command, timeout)[0] + margin > timeout
    ]

    assert (len(_budgeted_steps()), short) == (4, [])
