#!/usr/bin/env python3
"""Fetch the two pinned models the memory reads, verify them, and stop.

The read path loads weights from the local Hugging Face cache only, so a
vault whose cache is empty answers by words alone and says so in the trace
(`model_unavailable`, `reranker_unavailable`). This is the one place the
product goes to the network for a model: each pinned commit is fetched with
the file list limited to what is read, and `model.safetensors` is checked
against the size and SHA-256 recorded beside the revision. A file that does
not match is removed and the run fails. Present and verified files are not
fetched again, so the nightly can run this every night.

Owner's decision 2026-09-10; see
`docs/research/2026-09-10-the-weights-arrive-with-the-install.md`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from embedding_model import (  # noqa: E402
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_WEIGHTS_BYTES,
    EMBEDDING_WEIGHTS_SHA256,
)
from reranker import (  # noqa: E402
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RERANKER_REVISION,
    DEFAULT_RERANKER_WEIGHTS_BYTES,
    DEFAULT_RERANKER_WEIGHTS_SHA256,
)

WEIGHTS_FILE = "model.safetensors"
# Everything the two loaders read, and nothing else: no README, no ONNX or
# `.bin` duplicates, no scripts.
ALLOW_PATTERNS = (
    "config.json",
    WEIGHTS_FILE,
    "tokenizer*",
    "sentencepiece*",
    "special_tokens*",
    "1_Pooling/*",
)
HASH_CHUNK_BYTES = 8 * 1024 * 1024
STATE_PRESENT = "present"
STATE_FETCHED = "fetched"
STATE_MISSING = "missing"
STATE_MISMATCH = "mismatch"
EXIT_INCOMPLETE = 1
EXIT_NO_LIBRARY = 2


@dataclass(frozen=True)
class PinnedModel:
    repo_id: str
    revision: str
    weights_sha256: str
    weights_bytes: int


def pinned_models() -> tuple[PinnedModel, ...]:
    """The encoder and the default reranker, as the read path pins them."""
    return (
        PinnedModel(
            EMBEDDING_MODEL,
            EMBEDDING_MODEL_REVISION,
            EMBEDDING_WEIGHTS_SHA256,
            EMBEDDING_WEIGHTS_BYTES,
        ),
        PinnedModel(
            DEFAULT_RERANKER_MODEL,
            DEFAULT_RERANKER_REVISION,
            DEFAULT_RERANKER_WEIGHTS_SHA256,
            DEFAULT_RERANKER_WEIGHTS_BYTES,
        ),
    )


def hub_library():
    """`huggingface_hub`, or None when the semantic extra is not installed."""
    try:
        import huggingface_hub
    except ImportError:
        return None
    return huggingface_hub


def cached_weights(model: PinnedModel, hub) -> Path | None:
    """The cached weights file at the pinned revision, without the network."""
    found = hub.try_to_load_from_cache(model.repo_id, WEIGHTS_FILE, revision=model.revision)
    if not isinstance(found, str):
        return None
    return Path(found)


def _digest(path: Path, ceiling: int) -> tuple[int, str]:
    """Size and SHA-256, reading no further than one chunk past the ceiling."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            total += len(chunk)
            if total > ceiling:
                break
            digest.update(chunk)
    return total, digest.hexdigest()


def mismatch_reason(path: Path, model: PinnedModel) -> str | None:
    """None when the file is the pinned one; otherwise what differs."""
    size, digest = _digest(path, model.weights_bytes)
    if size != model.weights_bytes:
        return f"size {size} != {model.weights_bytes}"
    if digest != model.weights_sha256:
        return "sha256 differs from the pinned digest"
    return None


def fetch(model: PinnedModel, hub) -> Path:
    """One pinned commit, the read files only; returns the weights path."""
    snapshot = hub.snapshot_download(
        model.repo_id, revision=model.revision, allow_patterns=list(ALLOW_PATTERNS)
    )
    return Path(snapshot) / WEIGHTS_FILE


def _outcome(model: PinnedModel, state: str, path: Path | None, reason: str | None) -> dict:
    return {
        "model": model.repo_id,
        "revision": model.revision,
        "state": state,
        "bytes": model.weights_bytes,
        "path": None if path is None else str(path),
        "reason": reason,
    }


def _verified_download(model: PinnedModel, hub) -> dict:
    path = fetch(model, hub)
    reason = mismatch_reason(path, model)
    if reason is None:
        return _outcome(model, STATE_FETCHED, path, None)
    path.unlink(missing_ok=True)
    return _outcome(model, STATE_MISMATCH, None, reason)


def ensure(model: PinnedModel, hub, *, download: bool) -> dict:
    """Present and verified, fetched and verified, missing, or a named mismatch."""
    path = cached_weights(model, hub)
    if path is not None and mismatch_reason(path, model) is None:
        return _outcome(model, STATE_PRESENT, path, None)
    if not download:
        return _outcome(model, STATE_MISSING, None, "not in the local cache")
    return _verified_download(model, hub)


def missing_models(hub) -> list[PinnedModel]:
    """The pinned models whose weights are not in the cache; a cheap probe."""
    return [model for model in pinned_models() if cached_weights(model, hub) is None]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report without downloading")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    return parser


def _print(outcomes: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"models": outcomes}, ensure_ascii=False, sort_keys=True))
        return
    for item in outcomes:
        reason = f" ({item['reason']})" if item["reason"] else ""
        print(f"install_models: {item['state']} {item['model']}@{item['revision'][:12]}{reason}")


def _exit_code(outcomes: list[dict]) -> int:
    settled = {STATE_PRESENT, STATE_FETCHED}
    if all(item["state"] in settled for item in outcomes):
        return 0
    return EXIT_INCOMPLETE


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    hub = hub_library()
    if hub is None:
        print(
            "install_models: huggingface_hub is not installed; "
            "run `uv sync --extra semantic` first",
            file=sys.stderr,
        )
        return EXIT_NO_LIBRARY
    outcomes = [ensure(model, hub, download=not args.check) for model in pinned_models()]
    _print(outcomes, args.json)
    return _exit_code(outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
