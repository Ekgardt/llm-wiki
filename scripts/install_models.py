#!/usr/bin/env python3
"""Fetch the two pinned models the memory reads, verify them, and stop.

The read path loads weights from the local Hugging Face cache only, so a
vault whose cache is empty answers by words alone and says so in the trace
(`model_unavailable`, `reranker_unavailable`). This is the one place the
product goes to the network for a model: each pinned commit is fetched with
the file list limited to what is read, and the weights file is checked against
the size and SHA-256 recorded beside the revision. A file that does not match is
removed and the run fails. Present and verified files are not fetched again, so
the nightly can run this every night.

The encoder is read through ONNX Runtime (`onnx/model.onnx`); once those
weights are verified, the `model.safetensors` it was read from before is removed
from the cache, since nothing reads it any more. The reranker still reads
`model.safetensors`.

Owner's decisions 2026-09-10 and 2026-09-25; see
`docs/research/2026-09-10-the-weights-arrive-with-the-install.md` and
`docs/research/2026-09-25-the-encoder-runs-without-torch.md`.
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
    EMBEDDING_READ_FILES,
    EMBEDDING_RETIRED_FILES,
    EMBEDDING_WEIGHTS_BYTES,
    EMBEDDING_WEIGHTS_FILE,
    EMBEDDING_WEIGHTS_SHA256,
)
from reranker import (  # noqa: E402
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RERANKER_REVISION,
    DEFAULT_RERANKER_WEIGHTS_BYTES,
    DEFAULT_RERANKER_WEIGHTS_SHA256,
)

RERANKER_WEIGHTS_FILE = "model.safetensors"
# Everything the reranker's `transformers` loader reads, and nothing else: no
# README, no ONNX or `.bin` duplicates, no scripts.
RERANKER_ALLOW_PATTERNS = (
    "config.json",
    RERANKER_WEIGHTS_FILE,
    "tokenizer*",
    "sentencepiece*",
    "special_tokens*",
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
    weights_file: str
    weights_sha256: str
    weights_bytes: int
    allow_patterns: tuple[str, ...]
    # Files an earlier loader read at this revision and nothing reads now.
    retired_files: tuple[str, ...] = ()


def pinned_models() -> tuple[PinnedModel, ...]:
    """The encoder and the default reranker, as the read path pins them."""
    return (
        PinnedModel(
            EMBEDDING_MODEL,
            EMBEDDING_MODEL_REVISION,
            EMBEDDING_WEIGHTS_FILE,
            EMBEDDING_WEIGHTS_SHA256,
            EMBEDDING_WEIGHTS_BYTES,
            EMBEDDING_READ_FILES,
            EMBEDDING_RETIRED_FILES,
        ),
        PinnedModel(
            DEFAULT_RERANKER_MODEL,
            DEFAULT_RERANKER_REVISION,
            RERANKER_WEIGHTS_FILE,
            DEFAULT_RERANKER_WEIGHTS_SHA256,
            DEFAULT_RERANKER_WEIGHTS_BYTES,
            RERANKER_ALLOW_PATTERNS,
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
    found = hub.try_to_load_from_cache(model.repo_id, model.weights_file, revision=model.revision)
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
        model.repo_id, revision=model.revision, allow_patterns=list(model.allow_patterns)
    )
    return Path(snapshot) / model.weights_file


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


def _companions_cached(model: PinnedModel, hub) -> bool:
    """Every file the loader reads by name is cached, not the weights alone."""
    names = [pattern for pattern in model.allow_patterns if "*" not in pattern]
    return all(_cached_path(model, hub, name) is not None for name in names)


def _cached_and_verified(model: PinnedModel, hub) -> Path | None:
    path = cached_weights(model, hub)
    if path is None or not _companions_cached(model, hub):
        return None
    if mismatch_reason(path, model) is not None:
        return None
    return path


def ensure(model: PinnedModel, hub, *, download: bool) -> dict:
    """Present and verified, fetched and verified, missing, or a named mismatch."""
    path = _cached_and_verified(model, hub)
    if path is not None:
        return _outcome(model, STATE_PRESENT, path, None)
    if not download:
        return _outcome(model, STATE_MISSING, None, "not in the local cache")
    return _verified_download(model, hub)


def _cached_path(model: PinnedModel, hub, filename: str) -> Path | None:
    found = hub.try_to_load_from_cache(model.repo_id, filename, revision=model.revision)
    if not isinstance(found, str):
        return None
    return Path(found)


def _snapshots_directory(link: Path) -> Path | None:
    """The repository's `snapshots/` directory the cached file lives under."""
    return next((parent for parent in link.parents if parent.name == "snapshots"), None)


def _blob_still_used(target: Path, link: Path) -> bool:
    """Whether any other cached file of the repository resolves to this blob."""
    snapshots = _snapshots_directory(link)
    if snapshots is None:
        return True
    return any(path.resolve() == target for path in snapshots.rglob("*") if path.is_file())


def _retire_cached(model: PinnedModel, hub, filename: str) -> bool:
    """Remove one retired file; its blob goes when no other cached file uses it."""
    link = _cached_path(model, hub, filename)
    if link is None:
        return False
    target = link.resolve()
    link.unlink()
    if target != link and not _blob_still_used(target, link):
        target.unlink(missing_ok=True)
    return True


def retire_superseded(model: PinnedModel, hub) -> list[str]:
    """The retired files removed from the cache at the pinned revision."""
    return [name for name in model.retired_files if _retire_cached(model, hub, name)]


def _settled_and_retired(model: PinnedModel, hub, *, download: bool) -> dict:
    """Ensure the weights; only verified weights retire what they replace."""
    outcome = ensure(model, hub, download=download)
    if download and outcome["state"] in {STATE_PRESENT, STATE_FETCHED}:
        outcome["retired"] = retire_superseded(model, hub)
    return outcome


def missing_models(hub) -> list[PinnedModel]:
    """The pinned models whose weights are not in the cache; a cheap probe."""
    return [
        model
        for model in pinned_models()
        if cached_weights(model, hub) is None or not _companions_cached(model, hub)
    ]


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
        print(_line(item))


def _line(item: dict) -> str:
    """One model's outcome, naming any retired file it removed from the cache."""
    reason = f" ({item['reason']})" if item["reason"] else ""
    retired = item.get("retired") or []
    removed = f"; removed {', '.join(retired)}" if retired else ""
    return f"install_models: {item['state']} {item['model']}@{item['revision'][:12]}{reason}{removed}"


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
    outcomes = [
        _settled_and_retired(model, hub, download=not args.check) for model in pinned_models()
    ]
    _print(outcomes, args.json)
    return _exit_code(outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
