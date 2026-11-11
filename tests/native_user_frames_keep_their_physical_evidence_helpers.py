"""Helpers shared by test_native_user_frames_keep_their_physical_evidence.py and the test files importing them."""

import json

from breadcrumb_decision import journal_block
from corpus_snapshot import collect_corpus

from tests.modules.breadcrumb_documents.contract_helpers import _source


def _captured_vault(root, frame, *, daily=True):
    import breadcrumb_evidence

    head, documents = _source(frame.encode())
    _, anchor = breadcrumb_evidence.read_head(documents[head])
    path = "knowledge/daily/2026-09-29.md"
    if daily:
        documents[path] = ("# Daily\n" + journal_block(json.loads(anchor), head, frame.encode())).encode()
    for relative, content in documents.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return collect_corpus(root, daily_paths=[path] if daily else [])
