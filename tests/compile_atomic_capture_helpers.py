"""Helpers shared by test_compile_atomic_capture.py and the test files importing them."""

from tests.compile_call_controller_helpers import work as work


def _interrupt_snapshot(work, monkeypatch):
    original = work[0]._append_compile_event

    def interrupted(database, row, now, *, event, lease):
        if event.sequence == 1:
            raise OSError('snapshot event interrupted')
        return original(database, row, now, event=event, lease=lease)

    monkeypatch.setattr(work[0], '_append_compile_event', interrupted)
