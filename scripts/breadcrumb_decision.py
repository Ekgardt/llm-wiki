"""Deterministic breadcrumb receipt: complete source plus one stable journal plan.

No provider result, classification, current clock or task identity participates
in this receipt. Queue seals bind it to whichever task currently owns delivery.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath

import breadcrumb_evidence as evidence
import breadcrumb_protocol as protocol
from reliable_memory import MAX_CAPTURE_DECISION_BYTES, canonical_json_bytes, sha256_bytes

VERSION = "breadcrumb-decision/v1"


def _require_canonical_event(content: bytes) -> None:
    event = json.loads(content.decode("utf-8", errors="strict"))
    if not isinstance(event, dict) or canonical_json_bytes(event) != content:
        raise ValueError("breadcrumb event is not a canonical JSON object")


def _require_input(bundle) -> tuple[dict, dict]:
    _require_canonical_event(bundle.content)
    manifest = protocol.read_manifest(bundle.manifest)
    anchor = protocol.read_anchor(bundle.anchor)
    actual = (manifest["input_sha256"], manifest["input_bytes"], anchor["input_sha256"], anchor["input_bytes"])
    expected = (sha256_bytes(bundle.content), len(bundle.content)) * 2
    if actual != expected:
        raise ValueError("breadcrumb receipt input does not match its manifest")
    return manifest, anchor


def _journal_block(anchor: dict, source_path: str, content: bytes | None) -> str:
    occurred = datetime.fromisoformat(anchor["occurred_at"])
    operation_id = f"breadcrumb:{anchor['intent_id']}"
    marker = sha256_bytes(operation_id.encode())
    link = "../raw/sessions/" + str(PurePosixPath(source_path).relative_to("knowledge/raw/sessions"))
    header = f"\n<!-- llm-wiki-operation:{marker} -->\n## [{occurred.strftime('%H:%M:%S')}] Captured event\n\n"
    block = header + f"[Complete captured event]({link})\n"
    archived = evidence.archived_source_path(source_path)
    archive_link = "../raw/sessions/" + str(PurePosixPath(archived).relative_to("knowledge/raw/sessions"))
    block += f"[Complete captured event if archived]({archive_link})\n"
    if content is not None:
        # Canonical JSON has no literal LF; indentation contains Markdown-like
        # user text without a delimiter chosen from that untrusted text.
        block += "\n    " + content.decode("utf-8", errors="strict") + "\n"
    return block


def _plan(anchor: dict, source_path: str, content: bytes | None) -> dict:
    block = _journal_block(anchor, source_path, content)
    return {
        "kind": "append", "path": f"knowledge/daily/{anchor['day']}.md",
        "block": block, "block_sha256": sha256_bytes(block.encode()),
        "operation_id": f"breadcrumb:{anchor['intent_id']}",
        "chosen_at": anchor["occurred_at"],
    }


def _record(bundle, manifest: dict, anchor: dict, content: bytes | None) -> dict:
    source_path = evidence.source_path(bundle.anchor)
    return {
        "schema_version": VERSION, "processing": "deterministic", "stage": "flush",
        "intent_id": manifest["intent_id"], "intent_sha256": sha256_bytes(bundle.manifest),
        "complete_input_sha256": manifest["input_sha256"], "input_bytes": manifest["input_bytes"],
        "source": {
            "path": source_path,
            "sha256": sha256_bytes(evidence.render_head(bundle.manifest, bundle.anchor)),
        },
        "operation_plan": [_plan(anchor, source_path, content)],
    }


def decision_bytes(bundle) -> bytes:
    manifest, anchor = _require_input(bundle)
    complete = canonical_json_bytes(_record(bundle, manifest, anchor, bundle.content))
    if len(complete) <= MAX_CAPTURE_DECISION_BYTES:
        return complete
    # This is a presentation choice dictated by the existing receipt transport,
    # not truncation: the linked permanent source always retains the full input.
    linked = canonical_json_bytes(_record(bundle, manifest, anchor, None))
    if len(linked) > MAX_CAPTURE_DECISION_BYTES:
        raise ValueError("breadcrumb receipt metadata exceeds its transport")
    return linked


def read_decision(data: bytes, bundle) -> dict:
    if data != decision_bytes(bundle):
        raise ValueError("breadcrumb deterministic receipt changed or conflicts")
    return json.loads(data.decode("utf-8", errors="strict"))
