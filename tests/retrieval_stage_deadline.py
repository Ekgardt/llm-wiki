"""Control only the original retrieval deadline guard in stub-backend tests."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import retrieval


def expire_after_backend(monkeypatch, backend, *, seconds):
    clock = SimpleNamespace(now=time.monotonic())
    deadline = clock.now + seconds
    original_guard = retrieval._check_deadline

    def checked(declared_deadline):
        # The production guard itself runs unchanged. The stdlib clock is
        # replaced only during that synchronous check and then restored.
        with patch.object(time, "monotonic", return_value=clock.now):
            original_guard(declared_deadline)

    def finished(**kwargs):
        result = backend(**kwargs)
        clock.now = deadline
        return result

    monkeypatch.setattr(retrieval, "_check_deadline", checked)
    return deadline, finished
