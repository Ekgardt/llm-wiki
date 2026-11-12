"""Helpers shared by test_breadcrumb_storage.py and the test files importing them."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import breadcrumb_bundle
import breadcrumb_storage as storage

MOMENT = datetime(2026, 9, 29, tzinfo=UTC)


SCOPE = {"host": "claude", "session": "test", "kind": "user_prompt", "occurrence": "one"}


def _publish(queue, coordinator, event=None, scope=None, accepted_at=MOMENT):
    return storage.publish_breadcrumb(
        queue, coordinator, SCOPE if scope is None else scope,
        {"prompt": "complete prompt"} if event is None else event,
        occurred_at=MOMENT, accepted_at=accepted_at, time_origin="host",
    )


def _bundle(root: Path, intent_id: str):
    return breadcrumb_bundle.load_bundle(root, breadcrumb_bundle.stored_manifest(root, intent_id))


def _registration_failure(*args, **kwargs):
    raise RuntimeError("queue registration unavailable")
