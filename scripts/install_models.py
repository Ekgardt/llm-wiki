#!/usr/bin/env python3
"""Fetch the two pinned models the memory reads, verify them, and stop.

The read path loads weights from `cache/models/<repo>/<revision>/` only, so a
vault without them answers by words alone and says so in the trace
(`model_unavailable`, `reranker_unavailable`). This is the one place the
product goes to the network for a model: each pinned commit is fetched with the
file list limited to what is read, written there as ordinary files (OpenVINO
refuses external weights reached through the Hugging Face cache's links), and
the weights file is checked against the size and SHA-256 recorded beside the
revision. A file that does not match is removed and the run fails. Present and
verified files are not fetched again, so the nightly can run this every night.

Once a model is verified, the pinned revision it replaced is deleted from the
Hugging Face cache (`intfloat/multilingual-e5-small` and
`BAAI/bge-reranker-v2-m3`, about 2.8 GB), since nothing reads it any more.
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
    EMBEDDING_WEIGHTS_BYTES,
    EMBEDDING_WEIGHTS_FILE,
    EMBEDDING_WEIGHTS_SHA256,
)
from model_files import model_directory  # noqa: E402
from openvino_runtime import decline_telemetry  # noqa: E402
from reranker import (  # noqa: E402
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RERANKER_REVISION,
    DEFAULT_RERANKER_WEIGHTS_BYTES,
    DEFAULT_RERANKER_WEIGHTS_SHA256,
    RERANKER_READ_FILES,
    RERANKER_WEIGHTS_FILE,
)

# One read while hashing model weights (hundreds of MB); 8 MiB trades a little memory for fewer
# reads. Performance trade-off, not measured.
HASH_CHUNK_BYTES = 8 * 1024 * 1024
STATE_PRESENT = "present"
STATE_FETCHED = "fetched"
STATE_MISSING = "missing"
STATE_MISMATCH = "mismatch"
STATE_UNREACHABLE = "unreachable"
EXIT_INCOMPLETE = 1
EXIT_NO_LIBRARY = 2
RUNTIME = ("openvino", "tokenizers")


@dataclass(frozen=True)
class PinnedModel:
    repo_id: str
    revision: str
    weights_file: str
    weights_sha256: str
    weights_bytes: int
    read_files: tuple[str, ...]
    # The pinned (repository, revision) this model replaced in the Hugging Face cache.
    replaces: tuple[str, str] | None = None
    # The modules that load this model; without them its weights are not wanted.
    runtime: tuple[str, ...] = RUNTIME

    @property
    def directory(self) -> Path:
        return model_directory(self.repo_id, self.revision)


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
            ("intfloat/multilingual-e5-small", "614241f622f53c4eeff9890bdc4f31cfecc418b3"),
        ),
        PinnedModel(
            DEFAULT_RERANKER_MODEL,
            DEFAULT_RERANKER_REVISION,
            RERANKER_WEIGHTS_FILE,
            DEFAULT_RERANKER_WEIGHTS_SHA256,
            DEFAULT_RERANKER_WEIGHTS_BYTES,
            RERANKER_READ_FILES,
            ("BAAI/bge-reranker-v2-m3", "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"),
        ),
    )


def hub_library():
    """`huggingface_hub`, or None when the semantic extra is not installed."""
    try:
        import huggingface_hub
    except ImportError:
        return None
    return huggingface_hub


def installed(model: PinnedModel) -> bool:
    """Every file the read path opens is on disk, the weights unverified."""
    return all((model.directory / name).is_file() for name in model.read_files)


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
    nothing (audit C-28).
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
    """One pinned commit, the read files only, as ordinary files; returns the weights path."""
    hub.snapshot_download(
        model.repo_id,
        revision=model.revision,
        allow_patterns=list(model.read_files),
        local_dir=str(model.directory),
    )
    return model.directory / model.weights_file


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


def _installed_and_verified(model: PinnedModel) -> Path | None:
    path = model.directory / model.weights_file
    if not installed(model) or mismatch_reason(path, model) is not None:
        return None
    return path


def ensure(model: PinnedModel, hub, *, download: bool) -> dict:
    """Present and verified, fetched and verified, missing, or a named mismatch.

    A weights file on disk that does not match is removed before the fetch, so
    the fetch writes the pinned bytes rather than keeping the bad ones.
    """
    path = _installed_and_verified(model)
    if path is not None:
        return _outcome(model, STATE_PRESENT, path, None)
    if not download:
        return _outcome(model, STATE_MISSING, None, "not installed under cache/models")
    (model.directory / model.weights_file).unlink(missing_ok=True)
    return _verified_download(model, hub)


def _holds(repo, repo_id: str, revision: str) -> bool:
    if repo.repo_id != repo_id or repo.repo_type != "model":
        return False
    return any(entry.commit_hash == revision for entry in repo.revisions)


def _cache_holding(hub, repo_id: str, revision: str):
    """The Hugging Face cache when it holds that pinned revision, else None."""
    try:
        cache = hub.scan_cache_dir()
    except hub.CacheNotFound:
        return None
    if any(_holds(repo, repo_id, revision) for repo in cache.repos):
        return cache
    return None


def retire_replaced(model: PinnedModel, hub) -> list[str]:
    """Delete the revision this model replaced from the Hugging Face cache."""
    if model.replaces is None:
        return []
    repo_id, revision = model.replaces
    cache = _cache_holding(hub, repo_id, revision)
    if cache is None:
        return []
    cache.delete_revisions(revision).execute()
    return [f"{repo_id}@{revision[:12]}"]


def _settled_and_retired(model: PinnedModel, hub, *, download: bool) -> dict:
    """Ensure the weights; only verified weights retire what they replace.

    A fetch that fails (offline, a server error) is this model's outcome, not the
    run's end: the next model is still tried (audit C-29 class).
    """
    try:
        outcome = ensure(model, hub, download=download)
    except _fetch_failures() as error:
        return _outcome(model, STATE_UNREACHABLE, None, f"{type(error).__name__}: fetch failed")
    if download and outcome["state"] in {STATE_PRESENT, STATE_FETCHED}:
        outcome["retired"] = retire_replaced(model, hub)
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
    """The pinned models whose runtime is installed: a vault fetches what it can load."""
    return [model for model in pinned_models() if _runtime_installed(model)]


def missing_models() -> list[PinnedModel]:
    """The wanted models whose files are not installed; a cheap probe."""
    return [model for model in wanted_models() if not installed(model)]


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
    """One model's outcome, naming the cached revision it removed."""
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
            "run `uv sync --locked --inexact --extra semantic` first",
            file=sys.stderr,
        )
        return EXIT_NO_LIBRARY
    decline_telemetry()
    outcomes = [
        _settled_and_retired(model, hub, download=not args.check) for model in wanted
    ]
    _print(outcomes, args.json)
    return _exit_code(outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
