"""The model cache environment, path identity checks and create-only publication of benchmark
outputs."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from retrieval_v2_contract import ROOT
from retrieval_v2_corpus import build_candidates
from retrieval_v2_scoring import candidate_text, hash_vector, tokens

# isort: split
from llm_wiki import runtime_roots


def validate_cache_root(cache_root: Path) -> Path:
    resolved = cache_root.expanduser().resolve()
    for root in _source_and_vault_roots():
        if resolved == root or root in resolved.parents:
            raise ValueError(f"cache root must be outside source and vault roots: {resolved}")
    return resolved


def is_reparse_point(path: Path) -> bool:
    try:
        stat = path.stat(follow_symlinks=False)
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


_MODEL_CACHE_SUBDIRECTORIES = {
    "HF_HOME": ("huggingface",),
    "HF_HUB_CACHE": ("huggingface", "hub"),
    "TRANSFORMERS_CACHE": ("transformers",),
    "SENTENCE_TRANSFORMERS_HOME": ("sentence-transformers",),
    "TORCH_HOME": ("torch",),
    "XDG_CACHE_HOME": ("xdg",),
}


def prepared_model_cache_root(cache_root: Path | str) -> Path:
    requested = Path(cache_root).expanduser()
    if requested.exists() and is_reparse_point(requested):
        raise ValueError("model cache root must not be a symlink or reparse point")
    requested.mkdir(parents=True, exist_ok=True)
    root = validate_cache_root(requested).resolve(strict=True)
    if is_reparse_point(root):
        raise ValueError("model cache root must not be a symlink or reparse point")
    return root


def _model_cache_paths(root: Path) -> dict[str, Path]:
    paths = {name: root.joinpath(*parts) for name, parts in _MODEL_CACHE_SUBDIRECTORIES.items()}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
        if is_reparse_point(path) or root not in path.resolve(strict=True).parents:
            raise ValueError("model cache path escaped isolated cache root")
    return paths


def _offline_flag(allow_download: bool) -> str:
    return "0" if allow_download else "1"


def _restore_environment(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@contextlib.contextmanager
def model_cache_environment(cache_root: Path | str, *, allow_download: bool):
    root = prepared_model_cache_root(cache_root)
    paths = _model_cache_paths(root)
    updates = {name: str(path) for name, path in paths.items()}
    updates.update(
        HF_HUB_OFFLINE=_offline_flag(allow_download),
        TRANSFORMERS_OFFLINE=_offline_flag(allow_download),
    )
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield "download-allowed-explicit-cache" if allow_download else "offline-local-files-only"
    finally:
        _restore_environment(previous)


def _use_posix_dir_fd() -> bool:
    return os.name == "posix"


def same_path_identity(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return os.path.samestat(expected, current)


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_POSIX_TEMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_NEW_OUTPUT_FLAGS = _POSIX_TEMP_FLAGS | getattr(os, "O_BINARY", 0)


def _fake_index_bytes(corpus: dict) -> bytes:
    payload = [
        {
            "evidence_id": candidate.evidence_id,
            "parent_id": candidate.parent_id,
            "tokens": list(tokens(candidate_text(candidate))),
            "vector": list(hash_vector(candidate_text(candidate))),
        }
        for candidate in build_candidates(corpus)
    ]
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"


def _write_and_sync(descriptor: int, data: bytes) -> None:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _require_descriptor_identity(directory_fd: int, identity: os.stat_result, message: str) -> None:
    if not os.path.samestat(identity, os.fstat(directory_fd)):
        raise PermissionError(message)


def _require_path_identity(path: Path, identity: os.stat_result, message: str) -> None:
    if not same_path_identity(path, identity):
        raise PermissionError(message)


def _publish_fake_index_posix(cache_root: Path, root_identity: os.stat_result, data: bytes, target_name: str) -> None:
    directory_fd = os.open(cache_root, _DIRECTORY_FLAGS)
    temporary_name = f".fake-index-{uuid.uuid4().hex}"
    try:
        _require_descriptor_identity(directory_fd, root_identity, "cache root changed during index build")
        descriptor = os.open(temporary_name, _POSIX_TEMP_FLAGS, 0o600, dir_fd=directory_fd)
        _write_and_sync(descriptor, data)
        _require_path_identity(cache_root, os.fstat(directory_fd), "cache root changed during index build")
        os.replace(temporary_name, target_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
        _require_path_identity(cache_root, os.fstat(directory_fd), "cache root changed during index publication")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)


def _publish_fake_index_portable(
    cache_root: Path, root_identity: os.stat_result, data: bytes, target_name: str
) -> None:
    path = cache_root / target_name
    descriptor, temporary_name = tempfile.mkstemp(prefix=".fake-index-", dir=cache_root)
    temporary = Path(temporary_name)
    try:
        _write_and_sync(descriptor, data)
        _require_path_identity(cache_root, root_identity, "cache root changed during index build")
        os.replace(temporary, path)
        _require_path_identity(cache_root, root_identity, "cache root changed during index publication")
    finally:
        temporary.unlink(missing_ok=True)


def write_fake_index(corpus: dict, cache_root: Path) -> int:
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_root = cache_root.resolve(strict=True)
    root_identity = cache_root.stat(follow_symlinks=False)
    data = _fake_index_bytes(corpus)
    if _use_posix_dir_fd():
        _publish_fake_index_posix(cache_root, root_identity, data, "fake-index.json")
    else:
        _publish_fake_index_portable(cache_root, root_identity, data, "fake-index.json")
    return len(data)


def _opened_parent_descriptor(parent: Path, parent_identity: os.stat_result) -> int:
    directory_fd = os.open(parent, _DIRECTORY_FLAGS)
    if not os.path.samestat(parent_identity, os.fstat(directory_fd)):
        os.close(directory_fd)
        raise PermissionError("output parent changed before publication")
    return directory_fd


class _OutputTarget:
    """One new output file, addressed through its parent descriptor on POSIX and by path elsewhere."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.parent_identity = output.parent.stat(follow_symlinks=False)
        self.directory_fd: int | None = None
        if _use_posix_dir_fd():
            self.directory_fd = _opened_parent_descriptor(output.parent, self.parent_identity)
        self.opened_identity: os.stat_result | None = None

    def open_new(self) -> int:
        if self.directory_fd is None:
            return os.open(self.output, _NEW_OUTPUT_FLAGS, 0o600)
        return os.open(self.output.name, _NEW_OUTPUT_FLAGS, 0o600, dir_fd=self.directory_fd)

    def published_identity(self) -> os.stat_result:
        if self.directory_fd is None:
            return self.output.stat(follow_symlinks=False)
        return os.stat(self.output.name, dir_fd=self.directory_fd, follow_symlinks=False)

    def expected_parent(self) -> os.stat_result:
        if self.directory_fd is None:
            return self.parent_identity
        return os.fstat(self.directory_fd)

    def unlink(self) -> None:
        if self.directory_fd is None:
            self.output.unlink()
            return
        os.unlink(self.output.name, dir_fd=self.directory_fd)

    def sync_parent(self) -> None:
        if self.directory_fd is not None:
            os.fsync(self.directory_fd)

    def close(self) -> None:
        if self.directory_fd is not None:
            os.close(self.directory_fd)


def _publish_bytes(target: _OutputTarget, data: bytes) -> os.stat_result:
    descriptor = target.open_new()
    with os.fdopen(descriptor, "wb") as handle:
        target.opened_identity = os.fstat(handle.fileno())
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        if not os.path.samestat(target.opened_identity, target.published_identity()):
            raise PermissionError("output changed during publication")
    if not same_path_identity(target.output.parent, target.expected_parent()):
        raise PermissionError("output parent changed during publication")
    target.sync_parent()
    return target.opened_identity


def _discard_partial_output(target: _OutputTarget) -> None:
    if target.opened_identity is None:
        return
    with contextlib.suppress(FileNotFoundError):
        if os.path.samestat(target.opened_identity, target.published_identity()):
            target.unlink()


def write_new_bytes(output: Path, data: bytes) -> os.stat_result:
    target = _OutputTarget(output)
    try:
        return _publish_bytes(target, data)
    except BaseException:
        _discard_partial_output(target)
        raise
    finally:
        target.close()


def write_new_output(output: Path, serialized: str) -> None:
    write_new_bytes(output, serialized.encode("utf-8"))


def canonical_report_bytes(report: dict) -> bytes:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _source_and_vault_roots() -> list[Path]:
    configured_vault = runtime_roots.configured_vault_root()
    return [ROOT.resolve(), *([configured_vault] if configured_vault else [])]


def require_outside_source_and_vault(output: Path) -> None:
    if any(output == root or root in output.parents for root in _source_and_vault_roots()):
        raise ValueError("--output must be outside source and vault roots")
