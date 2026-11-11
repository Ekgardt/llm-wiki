"""Publishing a search generation: one writer gate through validate, register and activate, and the caller's deadline at every stage."""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import generation_search
import pytest

from tests.search_ranking_helpers import (
    _generation_snapshot,
)
from tests.slow_machine import SHORT_TIMEOUT

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def test_publication_survives_a_vault_that_moved_while_the_build_ran(tmp_path):
    """The build has to finish on a vault that is in use, or it never finishes.

    This asserted the opposite until 2026-09-05: any drift, even an edit with a
    preserved mtime, refused the publication before the catalog was touched. On
    the live vault that meant every nightly build from 2026-08-30 died here and
    nothing was ever activated. A snapshot describes a moment the vault passed
    through; it does not have to be the newest moment, and a source that has
    since moved on is dropped before its text can reach the model.
    """
    import search_memory

    vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nBefore.\n"}
    )
    target = vault / "knowledge/notes/page.md"
    before = target.stat()
    target.write_text("# Page\nChanged\n", encoding="utf-8", newline="")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    class Catalog:
        registered = False
        activated = False

        def register(self, generation_id):
            self.registered = True

        def activate(self, generation_id, *, expected_active):
            self.activated = True
            return True

    catalog = Catalog()

    published = search_memory.publish_generation(
        snapshot, vault, catalog, "gen-search", expected_active=None
    )

    assert published is True
    assert catalog.registered is True
    assert catalog.activated is True


def test_publication_holds_one_writer_gate_through_validate_register_and_activate(
    monkeypatch,
):
    import search_memory

    events = []
    lock = threading.Lock()
    writer_attempted = threading.Event()
    writer_entered = threading.Event()

    class Coordinator:
        gate_calls = 0

        @contextmanager
        def writer_gate(self, *, wait_seconds=None):
            self.gate_calls += 1
            name = threading.current_thread().name
            if name == "cooperating-writer":
                writer_attempted.set()
            with lock:
                events.append(f"{name}:enter")
                if name == "cooperating-writer":
                    writer_entered.set()
                try:
                    yield
                finally:
                    events.append(f"{name}:exit")

    coordinator = Coordinator()
    writer = None

    def validate(snapshot, vault, *, coordinator=None, **deadline_options):
        assert coordinator is None
        assert lock.locked()
        assert deadline_options == {}
        events.append("validate")

    class Catalog:
        def register(self, generation_id):
            nonlocal writer
            assert lock.locked()
            events.append("register")

            def cooperating_write():
                with coordinator.writer_gate():
                    events.append("writer")

            writer = threading.Thread(
                target=cooperating_write,
                name="cooperating-writer",
            )
            writer.start()
            assert writer_attempted.wait(timeout=SHORT_TIMEOUT)
            assert writer_entered.is_set() is False

        def activate(self, generation_id, *, expected_active):
            assert lock.locked()
            assert writer_entered.is_set() is False
            events.append("activate")
            return True

    monkeypatch.setattr(generation_search, "validate_live_snapshot", validate)

    assert search_memory.publish_generation(
        object(),
        Path("vault"),
        Catalog(),
        "gen-search",
        expected_active=None,
        coordinator=coordinator,
    )
    assert writer is not None
    writer.join(timeout=SHORT_TIMEOUT)
    assert writer.is_alive() is False
    assert coordinator.gate_calls == 2
    assert events == [
        "MainThread:enter",
        "validate",
        "register",
        "activate",
        "MainThread:exit",
        "cooperating-writer:enter",
        "writer",
        "cooperating-writer:exit",
    ]


def test_publication_deadline_bounds_gate_validation_and_catalog_stages(monkeypatch):
    import search_memory

    now = 10.0
    gate_waits = []
    events = []

    def monotonic():
        return now

    class Coordinator:
        @contextmanager
        def writer_gate(self, *, wait_seconds=None):
            gate_waits.append(wait_seconds)
            events.append("gate-enter")
            try:
                yield
            finally:
                events.append("gate-exit")

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None):
        nonlocal now
        assert coordinator is None
        assert deadline_seconds == 5.0
        events.append("validate")
        now = 12.0

    class Catalog:
        activated = False

        def register(self, generation_id, *, deadline=None):
            nonlocal now
            assert deadline == 15.0
            events.append("register")
            now = 15.0

        def activate(self, generation_id, *, expected_active, deadline=None):
            self.activated = True
            pytest.fail("activation started after publication deadline")

    catalog = Catalog()
    monkeypatch.setattr(search_memory.time, "monotonic", monotonic)
    monkeypatch.setattr(generation_search, "validate_live_snapshot", validate)

    with pytest.raises(TimeoutError, match="deadline"):
        search_memory.publish_generation(
            object(),
            Path("vault"),
            catalog,
            "gen-search",
            expected_active=None,
            coordinator=Coordinator(),
            deadline=15.0,
        )

    assert gate_waits == [5.0]
    assert events == ["gate-enter", "validate", "register", "gate-exit"]
    assert catalog.activated is False


def test_publication_forwards_absolute_deadline_without_post_commit_false_timeout(
    monkeypatch,
):
    import search_memory

    now = 10.0
    calls = []

    def monotonic():
        return now

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None):
        assert coordinator is None
        assert deadline_seconds == 5.0
        calls.append(("validate", deadline_seconds))

    class Catalog:
        def register(self, generation_id, *, deadline=None):
            assert deadline == 15.0
            calls.append(("register", deadline))

        def activate(self, generation_id, *, expected_active, deadline=None):
            nonlocal now
            assert deadline == 15.0
            calls.append(("activate", deadline))
            now = 20.0
            return True

    monkeypatch.setattr(search_memory.time, "monotonic", monotonic)
    monkeypatch.setattr(generation_search, "validate_live_snapshot", validate)

    assert search_memory.publish_generation(
        object(),
        Path("vault"),
        Catalog(),
        "gen-search",
        expected_active=None,
        deadline=15.0,
    ) is True
    assert calls == [
        ("validate", 5.0),
        ("register", 15.0),
        ("activate", 15.0),
    ]


def test_publication_forwards_stop_context_to_failed_cas_cleanup(monkeypatch):
    import search_memory

    calls = []

    def cancelled():
        return False

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None, cancelled=None):
        assert coordinator is None
        assert deadline_seconds == 5.0
        assert cancelled is not None

    class Catalog:
        def register(self, generation_id, *, deadline=None, cancelled=None):
            return None

        def activate(
            self, generation_id, *, expected_active, deadline=None, cancelled=None
        ):
            return False

        def discard_unactivated(self, generation_id, *, deadline=None, cancelled=None):
            calls.append((generation_id, deadline, cancelled))
            return True

    monkeypatch.setattr(search_memory.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(generation_search, "validate_live_snapshot", validate)

    assert search_memory.publish_generation(
        object(),
        Path("vault"),
        Catalog(),
        "gen-search",
        expected_active=None,
        deadline=15.0,
        cancelled=cancelled,
    ) is False
    assert calls == [("gen-search", 15.0, cancelled)]


@pytest.mark.parametrize("timeout_stage", ["register", "activate"])
def test_publication_propagates_catalog_deadline_timeout(monkeypatch, timeout_stage):
    import search_memory

    activated = False

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None):
        assert deadline_seconds == 5.0

    class Catalog:
        def register(self, generation_id, *, deadline=None):
            assert deadline == 15.0
            if timeout_stage == "register":
                raise TimeoutError("catalog register deadline")

        def activate(self, generation_id, *, expected_active, deadline=None):
            nonlocal activated
            assert deadline == 15.0
            activated = True
            if timeout_stage == "activate":
                raise TimeoutError("catalog activate deadline")
            return True

    monkeypatch.setattr(search_memory.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(generation_search, "validate_live_snapshot", validate)

    with pytest.raises(TimeoutError, match=f"catalog {timeout_stage} deadline"):
        search_memory.publish_generation(
            object(),
            Path("vault"),
            Catalog(),
            "gen-search",
            expected_active=None,
            deadline=15.0,
        )
    assert activated is (timeout_stage == "activate")


def test_publication_rejects_expired_deadline_before_gate(monkeypatch):
    import search_memory

    class Coordinator:
        def writer_gate(self, *, wait_seconds=None):
            pytest.fail("expired publication attempted writer gate")

    monkeypatch.setattr(search_memory.time, "monotonic", lambda: 10.0)

    with pytest.raises(TimeoutError, match="deadline"):
        search_memory.publish_generation(
            object(),
            Path("vault"),
            object(),
            "gen-search",
            expected_active=None,
            coordinator=Coordinator(),
            deadline=10.0,
        )
