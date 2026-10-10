"""Context source selection must not expand a corpus before compilation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import corpus_snapshot  # noqa: E402
import mcp_server  # noqa: E402


def test_selected_sources_keep_the_captured_chunk_view(monkeypatch, tmp_path):
    page = tmp_path / "knowledge/notes/example.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\n---\n# Example\nA useful sentence.\n")
    snapshot = corpus_snapshot.collect_corpus(tmp_path)

    def unwanted_expansion(*args, **kwargs):
        raise AssertionError("source selection must not expand captured chunks")

    monkeypatch.setattr(corpus_snapshot, "_chunks", unwanted_expansion)
    selected = mcp_server._selected_chunks(snapshot, {"knowledge/notes/example.md"})
    assert len(selected) == len(snapshot.chunks)
