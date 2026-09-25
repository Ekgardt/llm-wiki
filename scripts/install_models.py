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
import time
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
STATE_UNREACHABLE = "unreachable"
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
    # The modules that load this model; without them its weights are not wanted.
    runtime: tuple[str, ...] = ()


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
            ("onnxruntime", "tokenizers"),
        ),
        PinnedModel(
            DEFAULT_RERANKER_MODEL,
            DEFAULT_RERANKER_REVISION,
            RERANKER_WEIGHTS_FILE,
            DEFAULT_RERANKER_WEIGHTS_SHA256,
            DEFAULT_RERANKER_WEIGHTS_BYTES,
            RERANKER_ALLOW_PATTERNS,
            (),
            ("torch", "transformers"),
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
    """None when the file is the pinned one; otherwise what differs.

    A file already verified and unchanged since (same size, modification time and
    inode) is not read again: the nightly re-hashed 2.3 GB each night to learn
    nothing (audit C-28,
    docs/research/2026-09-25-a-bad-model-file-is-fetched-again-and-a-good-one-is-not-reread.md).
    """
    if _remembered_as_verified(path, model):
        return None
    reason = _hashed_mismatch(path, model)
    if reason is None:
        _remember_verified(path, model)
    return reason


def _hashed_mismatch(path: Path, model: PinnedModel) -> str | None:
    size, digest = _digest(path, model.weights_bytes)
    if size != model.weights_bytes:
        return f"size {size} != {model.weights_bytes}"
    if digest != model.weights_sha256:
        return "sha256 differs from the pinned digest"
    return None


def _verification_record() -> Path:
    from memory_state import STATE_ROOT

    return STATE_ROOT / "cache" / "model-verification.json"


def _file_identity(path: Path) -> list[object]:
    resolved = path.resolve()
    status = resolved.stat()
    return [str(resolved), status.st_size, status.st_mtime_ns, status.st_ino]


def _read_verifications() -> dict:
    try:
        recorded = json.loads(_verification_record().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return recorded if isinstance(recorded, dict) else {}


# A file modified within this long of its verification is re-read: a write in the
# same clock tick keeps size, modification time and inode (git's "racy" case).
RACY_WINDOW_NS = 2_000_000_000


def _remembered_as_verified(path: Path, model: PinnedModel) -> bool:
    try:
        identity = _file_identity(path)
    except OSError:
        return False
    entry = _read_verifications().get(model.repo_id)
    if not isinstance(entry, list) or entry[:-1] != [*identity, model.weights_sha256]:
        return False
    return _settled_before(identity[2], entry[-1])


def _settled_before(modified_ns: object, verified_ns: object) -> bool:
    if not isinstance(modified_ns, int) or not isinstance(verified_ns, int):
        return False
    return modified_ns + RACY_WINDOW_NS < verified_ns


def _remember_verified(path: Path, model: PinnedModel) -> None:
    """One entry per pinned model; a write that fails only costs a re-hash."""
    try:
        record = _verification_record()
        entry = [*_file_identity(path), model.weights_sha256, time.time_ns()]
        verified = {**_read_verifications(), model.repo_id: entry}
        record.parent.mkdir(parents=True, exist_ok=True)
        staged = record.with_suffix(".tmp")
        staged.write_text(json.dumps(verified, sort_keys=True), encoding="utf-8")
        staged.replace(record)
    except OSError:
        return


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
    # The blob goes with the link: the Hub re-links a cached blob with the same
    # name, so removing the link alone kept the bad bytes for the next fetch.
    _retire_cached(model, hub, model.weights_file)
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
    """Present and verified, fetched and verified, missing, or a named mismatch.

    A cached file that does not match is removed, blob and all, before the fetch,
    so the fetch brings the pinned bytes rather than re-linking the bad ones.
    """
    path = _cached_and_verified(model, hub)
    if path is not None:
        return _outcome(model, STATE_PRESENT, path, None)
    if not download:
        return _outcome(model, STATE_MISSING, None, "not in the local cache")
    _drop_mismatched(model, hub)
    return _verified_download(model, hub)


def _drop_mismatched(model: PinnedModel, hub) -> None:
    path = cached_weights(model, hub)
    if path is not None and mismatch_reason(path, model) is not None:
        _retire_cached(model, hub, model.weights_file)


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
    """Ensure the weights; only verified weights retire what they replace.

    A fetch that fails (offline, a server error) is this model's outcome, not the
    run's end: the next model is still tried (audit C-29 class,
    docs/research/2026-09-25-one-bad-generation-does-not-stop-the-prune.md).
    """
    try:
        outcome = ensure(model, hub, download=download)
    except _fetch_failures() as error:
        return _outcome(model, STATE_UNREACHABLE, None, f"{type(error).__name__}: fetch failed")
    if download and outcome["state"] in {STATE_PRESENT, STATE_FETCHED}:
        outcome["retired"] = retire_superseded(model, hub)
    return outcome


def _fetch_failures() -> tuple[type[BaseException], ...]:
    """OSError (the Hub's own errors derive from it) and the transport's, when installed."""
    try:
        import httpx
    except ImportError:
        return (OSError,)
    return (OSError, httpx.HTTPError)


def _runtime_installed(model: PinnedModel) -> bool:
    import importlib.util

    return all(importlib.util.find_spec(module) is not None for module in model.runtime)


def wanted_models() -> list[PinnedModel]:
    """The pinned models whose runtime is installed: a vault fetches what it can load.

    See `docs/research/2026-09-25-an-update-brings-the-extras-the-operator-chose.md`.
    """
    return [model for model in pinned_models() if _runtime_installed(model)]


def missing_models(hub) -> list[PinnedModel]:
    """The wanted models whose weights are not in the cache; a cheap probe."""
    return [
        model
        for model in wanted_models()
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
    wanted = wanted_models()
    if not wanted:
        print("install_models: no model runtime is installed; nothing to fetch")
        return 0
    hub = hub_library()
    if hub is None:
        print(
            "install_models: huggingface_hub is not installed; "
            "run `uv sync --extra semantic` first",
            file=sys.stderr,
        )
        return EXIT_NO_LIBRARY
    outcomes = [
        _settled_and_retired(model, hub, download=not args.check) for model in wanted
    ]
    _print(outcomes, args.json)
    return _exit_code(outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
