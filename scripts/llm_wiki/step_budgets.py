"""Worst-case seconds of the scheduled steps, independent of the steps themselves."""
from __future__ import annotations

# A nightly step keys new entries in this much time and leaves the rest for
# the next night; the store remembers what is done. The nightly kills the step
# at this budget plus the margin one last provider call may take
# (`scheduled_nightly.provider_margin_seconds`), which in auto mode is the whole
# provider order and not one of them.
# Research: docs/research/2026-09-17-a-step-no-provider-answered-is-not-green.md,
# docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
DEFAULT_BUDGET_SECONDS = 540.0


# The nightly's refresh of every indexed repository (also `--budget-seconds`). One
# repository of 436 sources indexed from scratch in 118.9 s (MCP_REPOSITORY_INDEX_SECONDS
# in mcp_server), and a refresh builds only what changed, so 15 minutes covers several
# full builds; past it each repository not yet built is named `deferred` for the next
# pass, never half-built. Scales
# with the number of repositories: a settings candidate.
REFRESH_ALL_BUDGET_SECONDS = 15 * 60


# The nightly repository-retention step's budget (scheduled_nightly adds its start margin). basis unknown — value predates measurement; review when the step reports its budget spent.
RETIRE_BUDGET_SECONDS = 5 * 60.0


# One `git fetch` of the nightly update; the update's time limit counts one fetch. basis unknown — value predates measurement; review when fetches time out on a working network.
FETCH_TIMEOUT_SECONDS = 120.0
# One git call of the nightly update, which fetches over the network; the local-only git calls elsewhere allow 10-20 s.
GIT_TIMEOUT_SECONDS = 60.0
SYNC_TIMEOUT_SECONDS = 600.0


# What one update may cost the pass that calls it, by its own timeouts: one
# fetch of the default branch, the baseline sync, and the seventeen ordinary git
# calls of a full update — `rev-parse --abbrev-ref`, `config --get`,
# `symbolic-ref`, `rev-parse FETCH_HEAD`, `rev-parse HEAD` twice,
# `merge-base --is-ancestor` twice, five `diff`s, `hash-object`, `cat-file`,
# the `merge`, and one `show` of the next commit's Python requirement, whose
# `uv python find` reads only this machine and is counted once. The nightly counts this in its own bound instead of leaving the
# step out of the sum; `tests/test_an_update_costs_what_it_says.py` runs a real
# update and holds the counts to the code. Research:
# docs/research/2026-09-18-a-pass-that-knows-how-long-it-can-be.md
FETCHES_PER_UPDATE = 1
GIT_CALLS_PER_UPDATE = 17
PYTHON_PROBE_TIMEOUT_SECONDS = GIT_TIMEOUT_SECONDS
WORST_CASE_SECONDS = (
    FETCHES_PER_UPDATE * FETCH_TIMEOUT_SECONDS
    + SYNC_TIMEOUT_SECONDS
    + PYTHON_PROBE_TIMEOUT_SECONDS
    + GIT_CALLS_PER_UPDATE * GIT_TIMEOUT_SECONDS
)


# The nightly kills this step after `RECLAIM_STEP_SECONDS`; the image prune stops
# on its own a margin before that, so the kill never lands inside a prune and the
# steps after it still run. What it did not reach is pruned the next night.
RECLAIM_STEP_SECONDS = 180
