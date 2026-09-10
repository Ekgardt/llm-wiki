"""The two waits a test may use, sized for the slowest supported machine.

The names and the rule are CPython's (`test.support.SHORT_TIMEOUT` and
`LONG_TIMEOUT`, https://docs.python.org/3/library/test.html): a wait that
detects a hang is long enough for the slowest buildbot and is never used to
call a slow test failed. Here the slowest machine is a hosted Windows runner
whose `synchronous=FULL` commits share one slow disk with the rest of the
matrix: a migration that costs 0.1 s locally measured 12.89 s there, and a
two-file generation build passed 60 s (runs 34486127382 and 34491307502,
2026-09-10). Every wait comes from here; no test carries a literal. See
`docs/research/2026-09-10-a-timeout-is-a-hang-bound-not-a-stopwatch.md`.
"""

from __future__ import annotations

import os

# Bazel's `--test_timeout`, CPython's `--timeout`: a machine known to be
# loaded scales every wait at the invocation instead of in the tests.
_SCALE_VARIABLE = "LLM_WIKI_TEST_TIMEOUT_SCALE"


def _scale() -> float:
    raw = os.environ.get(_SCALE_VARIABLE, "").strip()
    if not raw:
        return 1.0
    scale = float(raw)
    if scale <= 0:
        raise ValueError(f"{_SCALE_VARIABLE} must be positive, not {raw!r}")
    return scale


# A refusal the test expects: it must not wait five minutes to be told no.
SHORT_TIMEOUT = 30.0 * _scale()

# A hang bound: a thread to join, an event another thread sets, a build the
# test expects to finish. Hitting it means something is broken, not slow.
LONG_TIMEOUT = 300.0 * _scale()

# The pause a worker holds while the test acts around it. Longer than every
# wait of the test, so the worker can never time out first and turn "the
# test was slow" into "the worker failed".
PAUSE_TIMEOUT = 2 * LONG_TIMEOUT
