"""A deep expression is a parse error on every Python, never a dead process.

docs/research/2026-09-26-python-3-10-does-not-crash-on-a-deep-expression.md
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.slow_machine import LONG_TIMEOUT

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
# The Windows main thread's stack; Linux CI passed only on its 8 MiB one.
PROBE = f"""
import sys, threading
sys.path.insert(0, {str(SCRIPTS)!r})
from python_parse import PARSE_FAILURES, parse_python
threading.stack_size(1024 * 1024)
answer = []
def run():
    try:
        parse_python("x = " + "+".join(["1"] * 100_000))
        answer.append("parsed")
    except PARSE_FAILURES as error:
        answer.append(type(error).__name__)
thread = threading.Thread(target=run)
thread.start()
thread.join()
print(answer[0])
"""


def test_a_deep_expression_in_a_small_stack_is_a_parse_error() -> None:
    probe = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=LONG_TIMEOUT
    )

    assert (probe.returncode, probe.stdout.strip()) == (0, "RecursionError")


def test_a_long_data_literal_is_still_parsed() -> None:
    sys.path.insert(0, str(SCRIPTS))
    from python_parse import parse_python

    tree = parse_python("x = [" + ", ".join(["1"] * 100_000) + "]\n")

    assert len(tree.body) == 1


def test_a_flat_table_of_signed_numbers_is_parsed() -> None:
    """Audit 2026-09-26 C-3: many one-level items are not one deep chain."""
    sys.path.insert(0, str(SCRIPTS))
    from python_parse import _longest_operator_chain

    table = ("TABLE = [" + ", ".join(f"-{index}" for index in range(4000)) + "]\n").encode()

    assert _longest_operator_chain(table) == 1
