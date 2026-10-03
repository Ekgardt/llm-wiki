"""Permanent source writes use real fenced transactions and survive runtime loss."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import breadcrumb_evidence as evidence
import memory_queue
import model_dlp
import pytest

from tests.test_breadcrumb_storage import _bundle, _publish
from tests.test_queue_v3_capture_links import _coordinator, _queue


@contextmanager
def _worker(tmp_path: Path, content: str = "complete event"):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    publication = _publish(queue, coordinator, {"prompt": content})
    bundle = _bundle(tmp_path, publication.intent_id)
    lease = queue.claim_capture("source-writer", handler_versions=(2,))
    with queue.queue_owner(role="queue-worker", scope="worker:source-writer") as owner:
        with memory_queue.capture_task_fences(
            queue, coordinator, lease.id, intent_id=publication.intent_id,
            mode="worker", owner=owner,
        ) as (_task_fence, intent_fence):
            active = queue.active_capture_binding(None, lease.id)
            queue.seal_capture_binding(
                lease.id, consumer_kind="transaction", consumer_id="source-qualification",
                active_link_digest=active.active_digest,
            )
            yield queue, coordinator, lease, intent_fence, owner, bundle


def test_full_source_is_committed_and_readable_without_runtime_parts(tmp_path):
    with _worker(tmp_path, "x" * 1_048_576) as arguments:
        head = evidence.publish_source(*arguments)
        before = {path: path.read_bytes() for path in (tmp_path / "knowledge").rglob("*.md")}

        assert evidence.publish_source(*arguments) == head
        assert {path: path.read_bytes() for path in before} == before
        for path in (tmp_path / "run/capture-intents").rglob("*.part"):
            path.unlink()  # Qualification only: the permanent reader must be independent.

        assert evidence.read_permanent_source(tmp_path, head) == arguments[-1].content


def test_conflicting_permanent_source_is_not_overwritten(tmp_path):
    with _worker(tmp_path) as arguments:
        head = evidence.publish_source(*arguments)
        target = tmp_path / head
        target.write_bytes(b"external conflicting edit")

        with pytest.raises(ValueError, match="conflicts"):
            evidence.publish_source(*arguments)

        assert target.read_bytes() == b"external conflicting edit"


def test_partial_permanent_publication_retains_input_and_retries(tmp_path, monkeypatch):
    with _worker(tmp_path) as arguments:
        create = evidence._create_source_document
        head = evidence.source_path(arguments[-1].anchor)

        def fail_head(coordinator, owner, relative, document, preconditions):
            if relative == head:
                raise OSError("head publication interrupted")
            create(coordinator, owner, relative, document, preconditions)

        monkeypatch.setattr(evidence, "_create_source_document", fail_head)
        with pytest.raises(OSError, match="interrupted"):
            evidence.publish_source(*arguments)
        assert not (tmp_path / head).exists()
        active = arguments[0].active_capture_binding(None, arguments[2].id)
        assert _bundle(tmp_path, active.intent_id).content == arguments[-1].content
        monkeypatch.setattr(evidence, "_create_source_document", create)

        assert evidence.publish_source(*arguments) == head


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "knowledge/notes/not-a-source.md"])
def test_permanent_reader_refuses_paths_outside_its_contract(tmp_path, path):
    with pytest.raises(ValueError):
        evidence.read_permanent_source(tmp_path, path)


def test_changed_publication_policy_refuses_before_any_permanent_write(tmp_path, monkeypatch):
    with _worker(tmp_path) as arguments:
        def refuse(_content):
            raise model_dlp.DLPContentBlocked("publication policy refusal")

        monkeypatch.setattr(model_dlp, "require_safe_publication", refuse)
        with pytest.raises(model_dlp.DLPContentBlocked):
            evidence.publish_source(*arguments)

        assert list((tmp_path / "knowledge").rglob("*.md")) == []
        active = arguments[0].active_capture_binding(None, arguments[2].id)
        assert _bundle(tmp_path, active.intent_id).content == arguments[-1].content


def test_substituted_bundle_content_is_refused_before_any_permanent_write(tmp_path):
    with _worker(tmp_path) as arguments:
        forged = replace(arguments[-1], content=b"different input")

        with pytest.raises(ValueError, match="conflicts"):
            evidence.publish_source(*arguments[:-1], forged)

        assert list((tmp_path / "knowledge").rglob("*.md")) == []
