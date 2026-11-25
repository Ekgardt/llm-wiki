"""A thread whose timed join returns by its timeout on every supported CPython.

`Thread.join(timeout)` on CPython 3.13 and 3.14 can block until the thread ends
when the deadline passes between the two clock reads of `ThreadHandle_join`: the
negative remainder means "wait forever" (python/cpython#161712, fixed in 3.14.x
by python/cpython#161771, not in 3.13). A hosted Windows runner hit it: a
heartbeat that ignored its stop was refused after 30 s instead of 2 s (run
45638724158). `Event.wait` takes the lock path, which clamps the remainder, so a
join waits on the thread's own end-of-run event and joins untimed only once the
thread has finished.

Every thread the product starts is a `BoundedThread`; `tests/test_bounded_thread.py`
holds the scripts to that.
"""

from __future__ import annotations

import threading
from typing import Any


class BoundedThread(threading.Thread):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._run_finished = threading.Event()

    def run(self) -> None:
        try:
            super().run()
        finally:
            self._run_finished.set()

    def join(self, timeout: float | None = None) -> None:
        if timeout is not None and not self._run_finished.wait(max(timeout, 0.0)):
            return
        super().join()
