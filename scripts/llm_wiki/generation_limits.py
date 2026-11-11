"""The bounds and manifest shapes every generation is checked against."""

from __future__ import annotations

import re

from bounded_io import IO_CHUNK_BYTES

# A generation's `manifest.json`, the bound doctor reads it under too.
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACTS = 1024


# Absurdity ceilings for the sizes a manifest declares, not read bounds: a
# reader reads an artifact to the size the sealed manifest names, which
# `validate_generation` verified by hashing. These refuse a manifest whose
# numbers could not be true (audit M8).
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024 * 1024
MAX_GENERATION_BYTES = 64 * 1024 * 1024 * 1024
MAX_GENERATION_CHILDREN = 4096


# The catalog database file; it holds rows, not artifacts, so 256 MiB refuses only a runaway
# catalog. Basis unknown: value predates measurement; review when doctor reports the catalog
# nearing it.
MAX_CATALOG_BYTES = 256 * 1024 * 1024


HASH_CHUNK_BYTES = IO_CHUNK_BYTES


# A caller with a deadline gets whatever is left of it, capped here. A caller
# without one waits out contention instead of surfacing `database is locked`:
# two writers doing a compare-and-swap on a loaded machine can hold the write
# lock for longer than five seconds.
BUSY_MS = 5000
# A commit whose body already did what cannot be undone still waits this long
# for the write lock, whatever is left of the caller's deadline (audit 3, G-L11).
COMMIT_BUSY_FLOOR_MS = 250
UNBOUNDED_BUSY_MS = 30_000
CLEANUP_CATALOG_FENCE_SECONDS = 1.0


# How many superseded generations retention keeps behind the active one.
#
# One, and the number is reachability plus one fallback step, not a guess.
# Nothing reads a generation except through the active pointer: retrieval
# resolves it, and the incremental rebuild names the *active* generation as its
# reuse parent and reads `incremental-manifest.json` and the vectors out of that
# one tree. The single retained ancestor buys exactly one thing — it is the
# first alternative `_fallback_order` offers `_select_fallback` when the active
# tree stops validating, and it covers a reader that resolved the pointer just
# before the last activation. A generation two steps back buys nothing: it sits
# behind a younger candidate that is already ahead of it in the same order.
#
# This is rpm-ostree's two-deployment rule (current plus one rollback), not
# Kubernetes' ten revisions. Ten is affordable when a revision is metadata; a
# generation here is a ~180 MB tree, and 35 of them were 6.3 GB on a disk at
# 94%. See `docs/research/2026-08-29-how-many-superseded-generations-to-keep.md`.
RETAINED_ANCESTOR_GENERATIONS = 1


GENERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


SHA256_RE = re.compile(r"[0-9a-f]{64}")


WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


MANIFEST_KEYS = {
    "generation_id",
    "parent_generation_id",
    "schema_version",
    "collector_version",
    "extractor_version",
    "tokenizer_version",
    "tokenizer_config_sha256",
    "embedding_model_id",
    "embedding_model_revision",
    "vector_dimensions",
    "graph_schema_version",
    "graph_extractor_version",
    "source_manifest_sha256",
    "artifacts",
    "vector_state",
    "repository_scope",
    # A generation that holds code names its roots; a memory generation has no
    # such key, which is how one checkout can carry both and a reader can tell
    # them apart (decision 2026-09-12, the vault is a repository too).
    "code_roots",
}


REQUIRED_MANIFEST_KEYS = MANIFEST_KEYS - {
    "parent_generation_id",
    "repository_scope",
    "code_roots",
}


ARTIFACT_KEYS = {"path", "size", "sha256"}


VECTOR_FILES = {"vectors.npy", "vectors.json"}


V2_REQUIRED_ARTIFACTS = {
    "source-manifest.json",
    "evidence.sqlite3",
    "search.sqlite3",
}


V2_OPTIONAL_ARTIFACTS = {"incremental-manifest.json", *VECTOR_FILES}
