"""Build unpublished Lance generations or rebuild the legacy mutable index.

Generation mode consumes one coherent CorpusSnapshot and writes only beneath
an explicit unpublished generation directory. It never activates a catalog.
With no generation options, the CLI retains the legacy mutable rebuild.

Usage:
    uv run python scripts/rebuild_lance_index.py  # legacy compatibility rebuild
    uv run python scripts/rebuild_lance_index.py --status
    uv run python scripts/rebuild_lance_index.py \
        --generation-root cache/evidence-graph/generations \
        --generation-dir cache/evidence-graph/generations/<id> \
        --embedding-model-id <model> --embedding-model-revision <revision> \
        --embedding-dimensions <dimensions>

Requires: uv sync --extra hybrid
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
from numbers import Real
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus_snapshot import CorpusSnapshot, collect_corpus  # noqa: E402
from memory_state import ROOT  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TIMESTAMP_RE = re.compile(r"^timestamp:\s*(.+?)\s*$", re.MULTILINE)

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
LANCE_GENERATION_DIR = "lance"
LANCE_GENERATION_TABLE = "chunks"
MAX_LANCE_ARTIFACTS = 1024
MAX_LANCE_ENTRIES = 4096
MAX_LANCE_DIRECTORIES = 1024
MAX_LANCE_DEPTH = 16
MAX_LANCE_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_LANCE_GENERATION_BYTES = 64 * 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024


class _LanceDBAdapter:
    def __init__(self, lancedb: Any, pyarrow: Any):
        self._lancedb = lancedb
        self._pyarrow = pyarrow

    def write(
        self,
        output_dir: Path,
        rows: list[dict[str, object]],
        *,
        vector_dimension: int,
    ) -> None:
        pa = self._pyarrow
        schema = pa.schema(
            [
                pa.field("chunk_id", pa.string(), nullable=False),
                pa.field("source_id", pa.string(), nullable=False),
                pa.field("source_path", pa.string(), nullable=False),
                pa.field("parent_page", pa.string(), nullable=False),
                pa.field("heading_ancestry", pa.list_(pa.string()), nullable=False),
                pa.field("byte_start", pa.int64(), nullable=False),
                pa.field("byte_end", pa.int64(), nullable=False),
                pa.field("line_start", pa.int64(), nullable=False),
                pa.field("line_end", pa.int64(), nullable=False),
                pa.field("text", pa.string(), nullable=False),
                pa.field("source_sha256", pa.string(), nullable=False),
                pa.field("span_sha256", pa.string(), nullable=False),
                pa.field("type", pa.string(), nullable=False),
                pa.field("project", pa.string()),
                pa.field("authority", pa.string()),
                pa.field("confidence", pa.string()),
                pa.field("status", pa.string(), nullable=False),
                pa.field("valid_from", pa.string()),
                pa.field("valid_to", pa.string()),
                pa.field("language", pa.string()),
                pa.field("embedding_model_id", pa.string(), nullable=False),
                pa.field("embedding_model_revision", pa.string(), nullable=False),
                pa.field("embedding_dimensions", pa.int32(), nullable=False),
                pa.field(
                    "vector", pa.list_(pa.float32(), vector_dimension), nullable=False
                ),
            ]
        )
        data = pa.Table.from_pylist(rows, schema=schema)
        database = self._lancedb.connect(str(output_dir))
        database.create_table(LANCE_GENERATION_TABLE, data=data, mode="create")


def _default_lance_adapter() -> _LanceDBAdapter:
    try:
        import lancedb
        import pyarrow
    except ImportError as exc:
        raise RuntimeError(
            "LanceDB is not installed; run: uv sync --extra hybrid"
        ) from exc
    return _LanceDBAdapter(lancedb, pyarrow)


def _model_value(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid Unicode") from exc
    return value


def _vectors(embedder: object, texts: list[str], dimensions: int) -> list[list[float]]:
    encode = getattr(embedder, "encode", None)
    if not callable(encode):
        raise TypeError("embedder must provide an encode(texts) method")
    encoded = encode(texts)
    if len(encoded) != len(texts):
        raise ValueError("embedder returned the wrong vector count")
    vectors: list[list[float]] = []
    for encoded_vector in encoded:
        vector = list(encoded_vector)
        if len(vector) != dimensions:
            raise ValueError("embedder returned a vector with the wrong dimensions")
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
            for value in vector
        ):
            raise ValueError("embedder returned a non-finite or non-numeric vector")
        vectors.append([float(value) for value in vector])
    return vectors


def _is_link_or_reparse(path: Path, metadata: os.stat_result | None = None) -> bool:
    info = path.lstat() if metadata is None else metadata
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _real_directory(path: Path, name: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{name} must be an existing real directory") from exc
    if _is_link_or_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError(f"{name} must be an existing real directory")
    return metadata


def _validated_generation_paths(
    generation_root: Path, generation_dir: Path
) -> tuple[
    Path,
    Path,
    os.stat_result,
    Path,
    tuple[str, ...],
    tuple[os.stat_result, ...],
]:
    root = Path(os.path.abspath(generation_root))
    target = Path(os.path.abspath(generation_dir))
    root_metadata = _real_directory(root, "generation_root")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("generation_dir must be contained under generation_root") from exc
    if not relative.parts:
        raise ValueError("generation_dir must be below generation_root")
    current = root
    target_metadata = None
    identity_chain = [root_metadata]
    for part in relative.parts:
        current /= part
        target_metadata = _real_directory(current, "generation_dir")
        identity_chain.append(target_metadata)
    assert target_metadata is not None
    output_dir = target / LANCE_GENERATION_DIR
    try:
        output_dir.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"Lance generation artifact already exists: {output_dir}")
    return (
        target,
        output_dir,
        target_metadata,
        root,
        relative.parts,
        tuple(identity_chain),
    )


def _cleanup_output(
    generation_dir: Path, expected: os.stat_result, output_dir: Path
) -> None:
    try:
        current = generation_dir.lstat()
    except FileNotFoundError:
        return
    if _is_link_or_reparse(generation_dir, current) or not os.path.samestat(
        expected, current
    ):
        return
    try:
        output_metadata = output_dir.lstat()
    except FileNotFoundError:
        return
    if _is_link_or_reparse(output_dir, output_metadata):
        return
    if stat.S_ISDIR(output_metadata.st_mode):
        shutil.rmtree(output_dir)
    elif stat.S_ISREG(output_metadata.st_mode):
        output_dir.unlink()


def _posix_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_posix_directory_at(
    parent_fd: int, name: str, expected: os.stat_result
) -> int:
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise PermissionError("Lance artifact directory is linked or changed")
    try:
        descriptor = os.open(
            name, _posix_directory_flags(), dir_fd=parent_fd
        )
    except OSError as exc:
        raise PermissionError("Lance artifact directory is linked or changed") from exc
    opened = os.fstat(descriptor)
    if not os.path.samestat(expected, opened):
        os.close(descriptor)
        raise PermissionError("Lance artifact directory changed before open")
    return descriptor


def _open_posix_generation_chain(
    generation_root: Path,
    parts: tuple[str, ...],
    validated_chain: tuple[os.stat_result, ...],
) -> tuple[list[int], tuple[os.stat_result, ...]]:
    descriptors: list[int] = []
    try:
        root_fd = os.open(generation_root, _posix_directory_flags())
        descriptors.append(root_fd)
        if not os.path.samestat(validated_chain[0], os.fstat(root_fd)):
            raise PermissionError("generation root changed before descriptor open")
        expected_chain = [os.fstat(root_fd)]
        current_fd = root_fd
        for index, name in enumerate(parts, 1):
            expected = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            if not os.path.samestat(validated_chain[index], expected):
                raise PermissionError("generation directory changed before descriptor open")
            child_fd = _open_posix_directory_at(current_fd, name, expected)
            descriptors.append(child_fd)
            expected_chain.append(os.fstat(child_fd))
            current_fd = child_fd
        return descriptors, tuple(expected_chain)
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _verify_posix_generation_chain(
    generation_root: Path,
    parts: tuple[str, ...],
    expected_chain: tuple[os.stat_result, ...],
) -> None:
    descriptors: list[int] = []
    try:
        root_fd = os.open(generation_root, _posix_directory_flags())
        descriptors.append(root_fd)
        if not os.path.samestat(expected_chain[0], os.fstat(root_fd)):
            raise PermissionError("generation root changed during Lance build")
        current_fd = root_fd
        for index, name in enumerate(parts, 1):
            current = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            child_fd = _open_posix_directory_at(current_fd, name, current)
            descriptors.append(child_fd)
            if not os.path.samestat(expected_chain[index], os.fstat(child_fd)):
                raise PermissionError("generation directory changed during Lance build")
            current_fd = child_fd
    except OSError as exc:
        raise PermissionError("generation path changed during Lance build") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _new_posix_staging(root_fd: int) -> tuple[str, int, os.stat_result]:
    for _attempt in range(32):
        name = f".lance-build-{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        expected = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        descriptor = _open_posix_directory_at(root_fd, name, expected)
        return name, descriptor, os.fstat(descriptor)
    raise FileExistsError("could not allocate a unique Lance staging directory")


def _posix_adapter_output_path(
    generation_root: Path, staging_name: str, staging_fd: int
) -> Path:
    if sys.platform.startswith("linux"):
        return Path("/proc/self/fd") / str(staging_fd) / LANCE_GENERATION_DIR
    return generation_root / staging_name / LANCE_GENERATION_DIR


def _hash_posix_file_at(
    parent_fd: int, name: str, expected: os.stat_result
) -> tuple[int, str]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise PermissionError("Lance artifact is linked or changed") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise PermissionError("Lance artifact changed before open")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, HASH_CHUNK_BYTES):
            total += len(chunk)
            if total > expected.st_size:
                raise PermissionError("Lance artifact changed while hashing")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if total != expected.st_size or not os.path.samestat(opened, after):
            raise PermissionError("Lance artifact changed while hashing")
    finally:
        os.close(descriptor)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(current.st_mode) or not os.path.samestat(after, current):
        raise PermissionError("Lance artifact changed after hashing")
    return total, digest.hexdigest()


def _artifact_descriptors_posix(
    staging_fd: int, output_name: str
) -> tuple[list[dict[str, object]], os.stat_result]:
    expected_output = os.stat(
        output_name, dir_fd=staging_fd, follow_symlinks=False
    )
    output_fd = _open_posix_directory_at(staging_fd, output_name, expected_output)
    output_identity = os.fstat(output_fd)
    artifacts: list[dict[str, object]] = []
    entry_count = 0
    directory_count = 1
    total_size = 0

    def scan(directory_fd: int, relative: tuple[str, ...], depth: int) -> None:
        nonlocal entry_count, directory_count, total_size
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_LANCE_ENTRIES:
                    raise ValueError(
                        "Lance artifact entry count exceeds the supported bound"
                    )
                expected = os.stat(
                    entry.name, dir_fd=directory_fd, follow_symlinks=False
                )
                if stat.S_ISLNK(expected.st_mode):
                    raise PermissionError("Lance artifacts must not contain links")
                child_relative = (*relative, entry.name)
                if stat.S_ISDIR(expected.st_mode):
                    child_depth = depth + 1
                    if child_depth > MAX_LANCE_DEPTH:
                        raise ValueError(
                            "Lance artifact depth exceeds the supported bound"
                        )
                    directory_count += 1
                    if directory_count > MAX_LANCE_DIRECTORIES:
                        raise ValueError(
                            "Lance artifact directory count exceeds the supported bound"
                        )
                    child_fd = _open_posix_directory_at(
                        directory_fd, entry.name, expected
                    )
                    try:
                        scan(child_fd, child_relative, child_depth)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(expected.st_mode):
                    raise PermissionError("Lance artifacts must be regular files")
                if len(artifacts) >= MAX_LANCE_ARTIFACTS:
                    raise ValueError("Lance artifact count exceeds the supported bound")
                if expected.st_size > MAX_LANCE_ARTIFACT_BYTES:
                    raise ValueError("Lance artifact exceeds the supported size")
                total_size += expected.st_size
                if total_size > MAX_LANCE_GENERATION_BYTES:
                    raise ValueError("Lance generation exceeds the supported size")
                size, digest = _hash_posix_file_at(
                    directory_fd, entry.name, expected
                )
                artifacts.append(
                    {
                        "path": "/".join((LANCE_GENERATION_DIR, *child_relative)),
                        "size": size,
                        "sha256": digest,
                    }
                )

    try:
        scan(output_fd, (), 0)
        current_output = os.stat(
            output_name, dir_fd=staging_fd, follow_symlinks=False
        )
        if not os.path.samestat(output_identity, current_output):
            raise PermissionError("Lance artifact root changed during traversal")
    finally:
        os.close(output_fd)
    if not artifacts:
        raise RuntimeError("Lance adapter produced no artifacts")
    artifacts.sort(key=lambda item: str(item["path"]))
    return artifacts, output_identity


def _remove_posix_tree_at(
    parent_fd: int, name: str, expected: os.stat_result | None = None
) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if expected is not None and not os.path.samestat(expected, current):
        return
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    child_fd = _open_posix_directory_at(parent_fd, name, current)
    opened = os.fstat(child_fd)
    try:
        with os.scandir(child_fd) as entries:
            for entry in entries:
                _remove_posix_tree_at(child_fd, entry.name)
    finally:
        os.close(child_fd)
    try:
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if os.path.samestat(opened, after):
        os.rmdir(name, dir_fd=parent_fd)


def _artifact_descriptors(output_dir: Path) -> list[dict[str, object]]:
    _real_directory(output_dir, "Lance artifact root")
    files: list[tuple[str, Path, os.stat_result]] = []
    stack = [(output_dir, 0)]
    entry_count = 0
    directory_count = 1
    total_size = 0
    while stack:
        current, depth = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_LANCE_ENTRIES:
                    raise ValueError("Lance artifact entry count exceeds the supported bound")
                metadata = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                if entry.is_symlink() or _is_link_or_reparse(path, metadata):
                    raise PermissionError("Lance artifacts must not contain links")
                metadata = path.lstat()
                if _is_link_or_reparse(path, metadata):
                    raise PermissionError("Lance artifacts must not contain links")
                try:
                    relative = path.relative_to(output_dir).as_posix()
                except ValueError as exc:
                    raise PermissionError("Lance artifact escaped its generation") from exc
                if stat.S_ISDIR(metadata.st_mode):
                    child_depth = depth + 1
                    if child_depth > MAX_LANCE_DEPTH:
                        raise ValueError("Lance artifact depth exceeds the supported bound")
                    directory_count += 1
                    if directory_count > MAX_LANCE_DIRECTORIES:
                        raise ValueError(
                            "Lance artifact directory count exceeds the supported bound"
                        )
                    stack.append((path, child_depth))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise PermissionError("Lance artifacts must be regular files")
                if len(files) >= MAX_LANCE_ARTIFACTS:
                    raise ValueError("Lance artifact count exceeds the supported bound")
                size = metadata.st_size
                if size > MAX_LANCE_ARTIFACT_BYTES:
                    raise ValueError("Lance artifact exceeds the supported size")
                total_size += size
                if total_size > MAX_LANCE_GENERATION_BYTES:
                    raise ValueError("Lance generation exceeds the supported size")
                files.append((relative, path, metadata))
    if not files:
        raise RuntimeError("Lance adapter produced no artifacts")
    files.sort(key=lambda item: item[0])
    artifacts: list[dict[str, object]] = []
    for relative, path, expected in files:
        size = expected.st_size
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
                expected, opened
            ):
                raise PermissionError("Lance artifact identity changed while opening")
            read_size = 0
            while chunk := os.read(descriptor, HASH_CHUNK_BYTES):
                read_size += len(chunk)
                if read_size > size:
                    raise PermissionError("Lance artifact changed while hashing")
                digest.update(chunk)
            after = os.fstat(descriptor)
            if read_size != size or not os.path.samestat(opened, after):
                raise PermissionError("Lance artifact changed while hashing")
        finally:
            os.close(descriptor)
        current = path.lstat()
        if _is_link_or_reparse(path, current) or not os.path.samestat(after, current):
            raise PermissionError("Lance artifact identity changed after hashing")
        artifacts.append(
            {
                "path": f"{LANCE_GENERATION_DIR}/{relative}",
                "size": size,
                "sha256": digest.hexdigest(),
            }
        )
    return artifacts


def _build_lance_generation_posix(
    generation_root: Path,
    parts: tuple[str, ...],
    validated_chain: tuple[os.stat_result, ...],
    writer: Any,
    rows: list[dict[str, object]],
    embedding_dimensions: int,
) -> list[dict[str, object]]:
    descriptors, expected_chain = _open_posix_generation_chain(
        generation_root, parts, validated_chain
    )
    root_fd = descriptors[0]
    generation_fd = descriptors[-1]
    staging_name = ""
    staging_fd = -1
    staging_identity = None
    published_identity = None
    try:
        staging_name, staging_fd, staging_identity = _new_posix_staging(root_fd)
        _verify_posix_generation_chain(generation_root, parts, expected_chain)
        output_dir = _posix_adapter_output_path(
            generation_root, staging_name, staging_fd
        )
        writer(output_dir, rows, vector_dimension=embedding_dimensions)
        artifacts, output_identity = _artifact_descriptors_posix(
            staging_fd, LANCE_GENERATION_DIR
        )
        _verify_posix_generation_chain(generation_root, parts, expected_chain)
        current_staging = os.stat(
            staging_name, dir_fd=root_fd, follow_symlinks=False
        )
        if not os.path.samestat(staging_identity, current_staging):
            raise PermissionError("Lance staging directory changed during build")
        try:
            os.stat(
                LANCE_GENERATION_DIR,
                dir_fd=generation_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("Lance generation artifact already exists")
        current_output = os.stat(
            LANCE_GENERATION_DIR, dir_fd=staging_fd, follow_symlinks=False
        )
        if not os.path.samestat(output_identity, current_output):
            raise PermissionError("Lance artifact root changed before publication")
        os.rename(
            LANCE_GENERATION_DIR,
            LANCE_GENERATION_DIR,
            src_dir_fd=staging_fd,
            dst_dir_fd=generation_fd,
        )
        published = os.stat(
            LANCE_GENERATION_DIR, dir_fd=generation_fd, follow_symlinks=False
        )
        if not os.path.samestat(output_identity, published):
            raise PermissionError("Lance artifact root changed during publication")
        published_identity = output_identity
        _verify_posix_generation_chain(generation_root, parts, expected_chain)
        os.close(staging_fd)
        staging_fd = -1
        os.rmdir(staging_name, dir_fd=root_fd)
        staging_name = ""
        return artifacts
    except Exception:
        try:
            if published_identity is not None:
                _remove_posix_tree_at(
                    generation_fd, LANCE_GENERATION_DIR, published_identity
                )
            if staging_name:
                if staging_fd >= 0:
                    _remove_posix_tree_at(staging_fd, LANCE_GENERATION_DIR)
                    os.close(staging_fd)
                    staging_fd = -1
                _remove_posix_tree_at(
                    root_fd, staging_name, staging_identity
                )
        except Exception:
            pass
        raise
    finally:
        if staging_fd >= 0:
            os.close(staging_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def build_lance_generation(
    snapshot: CorpusSnapshot,
    generation_dir: Path,
    *,
    generation_root: Path,
    embedder: object,
    embedding_model_id: str,
    embedding_model_revision: str,
    embedding_dimensions: int,
    lance_adapter: object | None = None,
) -> list[dict[str, object]]:
    """Build isolated Lance artifacts from one immutable corpus snapshot."""
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if (
        isinstance(embedding_dimensions, bool)
        or not isinstance(embedding_dimensions, int)
        or not 1 <= embedding_dimensions <= 65536
    ):
        raise ValueError("embedding_dimensions must be a positive bounded integer")
    model_id = _model_value(embedding_model_id, "embedding_model_id")
    model_revision = _model_value(
        embedding_model_revision, "embedding_model_revision"
    )
    (
        target,
        output_dir,
        target_metadata,
        validated_root,
        generation_parts,
        validated_chain,
    ) = _validated_generation_paths(
        generation_root, generation_dir
    )
    adapter = lance_adapter if lance_adapter is not None else _default_lance_adapter()
    writer = getattr(adapter, "write", None)
    if not callable(writer):
        raise TypeError("lance_adapter must provide a write method")

    chunks = snapshot.chunks
    vectors = _vectors(embedder, [chunk.text for chunk in chunks], embedding_dimensions)
    rows = [
        {
            "chunk_id": chunk.id,
            "source_id": chunk.source_id,
            "source_path": chunk.source_path,
            "parent_page": chunk.parent_page,
            "heading_ancestry": list(chunk.heading_ancestry),
            "byte_start": chunk.byte_start,
            "byte_end": chunk.byte_end,
            "line_start": chunk.line_start,
            "line_end": chunk.line_end,
            "text": chunk.text,
            "source_sha256": chunk.source_sha256,
            "span_sha256": chunk.span_sha256,
            "type": chunk.type,
            "project": chunk.project,
            "authority": chunk.authority,
            "confidence": chunk.confidence,
            "status": chunk.status,
            "valid_from": chunk.valid_from,
            "valid_to": chunk.valid_to,
            "language": chunk.language,
            "embedding_model_id": model_id,
            "embedding_model_revision": model_revision,
            "embedding_dimensions": embedding_dimensions,
            "vector": vector,
        }
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    if os.name == "posix":
        return _build_lance_generation_posix(
            validated_root,
            generation_parts,
            validated_chain,
            writer,
            rows,
            embedding_dimensions,
        )
    try:
        writer(output_dir, rows, vector_dimension=embedding_dimensions)
        return _artifact_descriptors(output_dir)
    except Exception:
        try:
            _cleanup_output(target, target_metadata, output_dir)
        except Exception:
            pass
        raise


def _extract_fm(content: str, pattern: re.Pattern) -> str | None:
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = pattern.search(fm.group(1))
    return m.group(1).strip() if m else None


def rebuild_lance(verbose: bool = True) -> dict:
    """Full rebuild: Markdown → embeddings → LanceDB. Returns stats."""
    if not KNOWLEDGE_DIR.exists():
        return {"pages": 0, "error": "no knowledge dir"}

    try:
        from lance_store import have_lancedb, upsert_vectors
    except ImportError:
        if verbose:
            print("LanceDB not installed. Run: uv sync --extra hybrid")
        return {"pages": 0, "error": "lancedb_not_installed"}

    if not have_lancedb():
        # LanceDB might be importable but not initialized — that's OK,
        # upsert_vectors will create the table.
        pass

    # Check for embedding model.
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        if verbose:
            print("sentence-transformers not installed. Run: uv sync --extra semantic")
        return {"pages": 0, "error": "no_sentence_transformers"}

    embedder = SentenceTransformer(EMBEDDING_MODEL)

    pages = []
    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue
        pages.append(md)

    if verbose:
        print(f"Rebuilding LanceDB index from {len(pages)} Markdown pages...")

    paths, titles, summaries, projects, timestamps, texts = [], [], [], [], [], []

    for md in pages:
        content = md.read_text(encoding="utf-8", errors="ignore")
        title_match = H1_RE.search(content)
        title = title_match.group(1).strip() if title_match else md.stem
        summary_match = SUMMARY_RE.search(content)
        summary = summary_match.group(1).strip() if summary_match else ""
        body = FRONTMATTER_RE.sub("", content, count=1)
        project = _extract_fm(content, PROJECT_RE) or ""
        timestamp = _extract_fm(content, TIMESTAMP_RE) or ""

        paths.append(str(md.relative_to(ROOT).as_posix()))
        titles.append(title)
        summaries.append(summary)
        projects.append(project.lower())
        timestamps.append(timestamp[:10] if timestamp else "")
        texts.append(f"{title}. {summary}. {body[:300]}")

    if not texts:
        return {"pages": 0, "error": "no_pages"}

    t0 = time.time()
    vectors = embedder.encode(texts, show_progress_bar=False, convert_to_numpy=True).tolist()
    count = upsert_vectors(paths, titles, summaries, projects, timestamps, vectors, EMBEDDING_MODEL)
    elapsed = time.time() - t0

    stats = {"pages": count, "elapsed_s": round(elapsed, 2)}
    if verbose:
        print(f"  Done in {elapsed:.2f}s: {count} vectors stored in LanceDB.")
    return stats


def _load_generation_embedder(model_id: str, revision: str) -> object:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is not installed; run: uv sync --extra semantic"
        ) from exc
    return SentenceTransformer(model_id, revision=revision)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description=(
            "Build an unpublished snapshot-driven Lance generation, or run the "
            "legacy mutable compatibility rebuild when no generation options are given."
        )
    )
    p.add_argument("--status", action="store_true", help="Show index statistics.")
    p.add_argument("--generation-root", type=Path)
    p.add_argument("--generation-dir", type=Path)
    p.add_argument("--embedding-model-id")
    p.add_argument("--embedding-model-revision")
    p.add_argument("--embedding-dimensions", type=int)
    args = p.parse_args(argv)

    generation_values = (
        args.generation_root,
        args.generation_dir,
        args.embedding_model_id,
        args.embedding_model_revision,
        args.embedding_dimensions,
    )
    if any(value is not None for value in generation_values):
        if args.status:
            p.error("--status cannot be combined with generation build options")
        if any(value is None for value in generation_values):
            p.error("all generation and embedding options are required together")
        snapshot = collect_corpus(ROOT)
        embedder = _load_generation_embedder(
            args.embedding_model_id, args.embedding_model_revision
        )
        artifacts = build_lance_generation(
            snapshot,
            args.generation_dir,
            generation_root=args.generation_root,
            embedder=embedder,
            embedding_model_id=args.embedding_model_id,
            embedding_model_revision=args.embedding_model_revision,
            embedding_dimensions=args.embedding_dimensions,
        )
        print(
            json.dumps(
                {
                    "mode": "unpublished_generation",
                    "chunks": len(snapshot.chunks),
                    "artifacts": artifacts,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    if args.status:
        from lance_store import have_lancedb, vector_count
        if not have_lancedb():
            print("LanceDB not available.")
            return 1
        print(f"LanceDB index: {vector_count()} vectors.")
        return 0

    print("Legacy compatibility mode: rebuilding mutable cache/lancedb.")
    stats = rebuild_lance()
    return 0 if "error" not in stats else 1


if __name__ == "__main__":
    raise SystemExit(main())
