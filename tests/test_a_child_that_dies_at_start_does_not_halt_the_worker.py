"""A processor child that dies before it says it is up is a failed start, not a failed cleanup.

Before the ready handshake the parent has not tracked the child's tree. The
final cleanup read that as "the tree is unknown", reported
`process_cleanup_failed` in place of the real error, blocked the task and halted
the worker — for an import error or an out-of-memory kill in a spawned child.
"""

from __future__ import annotations

import os

import memory_queue
import pytest
from memory_queue import QueueOperationError


class _DiesWhenTheChildLoadsIt:
    """Pickles in the parent; unpickling in the spawned child exits the child."""

    def __reduce__(self):
        return (os._exit, (3,))

    def __call__(self, _task: dict) -> bool:
        return True


def test_the_real_error_is_reported_and_cleanup_is_confirmed() -> None:
    with pytest.raises(QueueOperationError) as error:
        memory_queue._run_processor_child(_DiesWhenTheChildLoadsIt(), {}, 30.0)

    assert error.value.code == "processor_result_malformed"
