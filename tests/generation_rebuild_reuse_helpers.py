"""Helpers shared by test_generation_rebuild_reuse.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path


class _CountingEmbedder:
    """A deterministic stand-in: the vector is a function of the text alone.

    That is the property the real model has only up to float32 re-batching
    noise, so the reuse rule is tested here against a model that has it
    exactly, and the real model's departure from it is measured separately in
    `docs/research/2026-08-28-what-a-rebuild-may-reuse.md`.
    """

    def __init__(self, dimensions: int = 4) -> None:
        self.dimensions = dimensions
        self.encoded: list[str] = []

    def __call__(self, texts):
        import hashlib

        rows = []
        for text in texts:
            self.encoded.append(text)
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            rows.append([digest[index] / 255.0 for index in range(self.dimensions)])
        return rows


def _snapshot_from(tmp_path: Path, pages: dict[str, str]):
    from corpus_snapshot import collect_corpus

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    for name, body in pages.items():
        (notes / name).write_text(body, encoding="utf-8")
    return collect_corpus(tmp_path, code_roots=(), max_files=100)


_PAGE = """---
type: concept
status: active
---

# {title}

One-sentence summary: {title} exists so a chunk exists.

{body}
"""


def _seal_like_a_published_generation(directory: Path) -> None:
    """A published generation's manifest seals its vectors; reuse checks those seals (2026-09-14)."""
    import hashlib
    import json

    artifacts = [
        {"path": name, "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(), "size": (directory / name).stat().st_size}
        for name in ("vectors.json", "vectors.npy")
    ]
    (directory / "manifest.json").write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
