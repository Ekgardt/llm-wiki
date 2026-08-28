"""CODE-03: generations for repositories other than the vault.

Three verbs over the one existing catalog -- `index` a named repository, `list`
what is indexed, `detect` what changed since. Nothing here adds a graph, a
catalog, a runtime root or a daemon: a foreign repository's generation is an
ordinary immutable generation under `cache/evidence-graph/generations/`,
disposable derived cache like every other one.

Two rules run through the whole module.

**A foreign repository is read-only.** Nothing is written into it -- no config,
no marker, no lock, not even a temporary file. Every path this module writes is
under the vault's own state root.

**A foreign generation is registered, never activated.** The catalog has one
active pointer and it belongs to the vault. Activating a foreign generation
would make the vault's own scope unresolvable and send every knowledge query
back to the legacy index -- NEW-65, recreated on purpose. Selection for a
foreign scope is `GenerationCatalog._scoped_generation`, which never moves the
pointer.

Design and sources: `docs/research/2026-08-28-generations-for-other-repositories.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "repository-index/v1"

# A listing names repositories, not files. 128 is far above the number of
# repositories one operator keeps on one machine and far below MAX_GENERATIONS.
MAX_LISTED_REPOSITORIES = 128
# Bounded path evidence in a change report. The counts are always exact; the
# named paths are the first of each kind in sorted order.
MAX_REPORTED_PATHS = 50
# Same ceiling the vault's own maintenance build uses for its corpus.
MAX_INDEXED_SOURCES = 20_000
# `_policy` accepts at most 128 roots; a repository with more tracked top-level
# directories than this is refused rather than silently narrowed.
MAX_CODE_ROOTS = 128
GIT_TIMEOUT_SECONDS = 10.0
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024


class RepositoryIndexRefused(ValueError):
    """A named, fail-closed refusal. `reason` is stable; the message explains."""

    def __init__(self, reason: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "refused",
            "reason": self.reason,
            "message": str(self),
            **self.details,
        }


def _refuse(reason: str, message: str, **details: object) -> RepositoryIndexRefused:
    return RepositoryIndexRefused(reason, message, **details)


# --------------------------------------------------------------------------
# state root and catalog
# --------------------------------------------------------------------------


def state_root_path(state_root: Path | str | None = None) -> Path:
    """The vault's runtime root -- the only place this module ever writes."""
    if state_root is not None:
        return Path(state_root).resolve()
    configured = os.environ.get("LLM_WIKI_STATE_ROOT")
    if configured:
        return Path(configured).resolve()
    import memory_state

    return Path(memory_state.STATE_ROOT).resolve()


def _open_catalog(state_root: Path, *, read_only: bool):
    from generation_catalog import GenerationCatalog

    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    if read_only:
        return _read_only_catalog(GenerationCatalog, state_root, catalog_path)
    return GenerationCatalog(state_root, catalog_path=catalog_path)


def _read_only_catalog(catalog_class, state_root: Path, catalog_path: Path):
    """A listing must not create a catalog just by asking what is indexed."""
    if not catalog_path.is_file():
        return None
    return catalog_class.open_existing_read_only(state_root, catalog_path=catalog_path)


# --------------------------------------------------------------------------
# admission: what this product will point itself at
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """A directory that passed every safety gate, with its resolved identity."""

    root: Path
    scope: object
    ownership_checked: bool


def _require_directory(directory: Path | str) -> Path:
    candidate = Path(directory).expanduser()
    if not candidate.is_absolute():
        raise _refuse(
            "repository_path_not_absolute",
            "repository path must be absolute",
            directory=str(candidate),
        )
    if not candidate.is_dir():
        raise _refuse(
            "repository_path_not_a_directory",
            f"repository path is not an existing directory: {candidate}",
            directory=str(candidate),
        )
    return candidate


def _require_unsymlinked(candidate: Path) -> Path:
    """A symlinked path is refused by name, and the real path is offered back.

    Following it silently would index one repository under another's identity:
    `repository_id` is derived from the canonical path, so the operator would
    get answers filed under a name they never asked for.
    """
    real = Path(os.path.realpath(candidate))
    if real != candidate:
        raise _refuse(
            "repository_path_is_symlinked",
            f"repository path resolves elsewhere; pass the real path: {real}",
            directory=str(candidate),
            real_path=str(real),
        )
    return real


def _require_owned_by_caller(root: Path) -> bool:
    """True when ownership was checked and held; False when the platform cannot.

    There is no privilege boundary in a local-first single-operator product, so
    ownership is the only boundary available. On a platform where `st_uid` means
    nothing the receipt says the check was not performed rather than claiming it
    passed.
    """
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return False
    if root.stat().st_uid != os.geteuid():
        raise _refuse(
            "repository_not_owned_by_caller",
            f"repository is owned by another user: {root}",
            directory=str(root),
        )
    return True


def _git_text(root: Path, *arguments: str) -> str:
    """One bounded, non-interactive Git read inside `root`. Never writes."""
    from repository_scope import sanitized_git_environment

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        shell=False,
        env=sanitized_git_environment(),
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise _refuse(
            "repository_git_probe_failed",
            f"git {arguments[0]} failed in {root}",
            directory=str(root),
        )
    if len(completed.stdout) > MAX_GIT_OUTPUT_BYTES:
        raise _refuse(
            "repository_git_output_too_large",
            f"git {arguments[0]} produced more than {MAX_GIT_OUTPUT_BYTES} bytes",
            directory=str(root),
        )
    return completed.stdout.decode("utf-8", errors="strict")


def _resolved_scope(root: Path, deadline: float | None, cancelled):
    from repository_scope import RepositoryScopeUnavailable, resolve_repository_scope

    try:
        return resolve_repository_scope(root, deadline=deadline, cancelled=cancelled)
    except TimeoutError:
        raise
    except (RepositoryScopeUnavailable, OSError, ValueError) as error:
        raise _refuse(
            "repository_identity_unavailable",
            f"repository identity could not be resolved: {error}",
            directory=str(root),
        ) from error


def _require_git_checkout_root(root: Path, scope) -> None:
    if scope.git_common_dir is None:
        raise _refuse(
            "repository_not_git",
            f"not a Git repository: {root}",
            directory=str(root),
        )
    if str(root) != scope.checkout_root:
        raise _refuse(
            "repository_not_checkout_root",
            "index the checkout root, not a directory inside it: "
            f"{scope.checkout_root}",
            directory=str(root),
            checkout_root=scope.checkout_root,
        )


def _require_not_submodule(root: Path) -> None:
    """A submodule's files belong to its superproject's tree, not to itself.

    Two generations claiming overlapping paths is a contradiction with no reader
    to resolve it, so the submodule is refused and its superproject named.
    """
    superproject = _git_text(root, "rev-parse", "--show-superproject-working-tree")
    if superproject.strip():
        raise _refuse(
            "repository_is_submodule",
            f"this is a submodule of {superproject.strip()}; index that instead",
            directory=str(root),
            superproject=superproject.strip(),
        )


def _require_not_the_vault(root: Path, state_root: Path) -> None:
    """The vault builds its own generation nightly, and activates it."""
    from memory_state import ROOT

    vault = Path(ROOT).resolve()
    if root in {vault, state_root}:
        raise _refuse(
            "repository_is_the_vault",
            "this is the vault itself; its generation is built and activated by "
            "the nightly pass",
            directory=str(root),
        )


def admit_repository(
    directory: Path | str,
    *,
    state_root: Path | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Admission:
    """Every safety gate, in the order that makes each refusal nameable."""
    root = _require_unsymlinked(_require_directory(directory))
    _require_not_the_vault(root, state_root_path(state_root))
    ownership_checked = _require_owned_by_caller(root)
    scope = _resolved_scope(root, deadline, cancelled)
    _require_git_checkout_root(root, scope)
    _require_not_submodule(root)
    return Admission(root=root, scope=scope, ownership_checked=ownership_checked)


# --------------------------------------------------------------------------
# code roots
# --------------------------------------------------------------------------


def tracked_top_level_directories(root: Path) -> tuple[str, ...]:
    """Top-level directories Git tracks, which is what belongs to the repository.

    Using the tracked set rather than a directory walk excludes build output,
    virtual environments and caches by construction, and honours `.gitignore`
    for free. Measured on the real second repository here: the tracked
    directories hold 963 files while the checkout is 2.2 GB.
    """
    names = _top_level_names(_git_text(root, "ls-files", "-z"))
    return tuple(sorted(name for name in names if (root / name).is_dir()))


def _top_level_names(listing: str) -> set[str]:
    """The first path component of every tracked file that lives in a directory."""
    entries = (entry for entry in listing.split("\0") if entry)
    return {entry.split("/", 1)[0] for entry in entries if "/" in entry}


def _approved_root_names() -> frozenset[str]:
    from corpus_snapshot import APPROVED_CODE_ROOTS

    return frozenset(APPROVED_CODE_ROOTS)


def _require_root_count(selected: tuple[str, ...], root: Path) -> None:
    if not selected:
        raise _refuse(
            "repository_has_no_code_roots",
            f"no indexable top-level directory is tracked in {root}",
            directory=str(root),
        )
    if len(selected) > MAX_CODE_ROOTS:
        raise _refuse(
            "repository_has_too_many_code_roots",
            f"{len(selected)} tracked top-level directories exceed the "
            f"{MAX_CODE_ROOTS} the collector accepts",
            directory=str(root),
            roots=len(selected),
        )


def _require_admissible_roots(selected: tuple[str, ...], root: Path) -> None:
    """Fail closed on a root the collector will not take. See §6 of the note.

    `corpus_snapshot.APPROVED_CODE_ROOTS` is a frozen set of *this vault's own*
    directory names, and `_code_root()` refuses anything else, so a repository
    whose code lives in `src/` cannot be collected whole. A partial index is
    worse than none, because *absent* would then read as *does not exist* --
    exactly NEW-67 -- so the whole index is refused and the roots are named. An
    operator who wants the admissible subset asks for it explicitly.
    """
    approved = _approved_root_names()
    refused = tuple(name for name in selected if name not in approved)
    if not refused:
        return
    raise _roots_not_collectable(selected, refused, approved, root)


def _roots_not_collectable(
    selected: tuple[str, ...],
    refused: tuple[str, ...],
    approved: frozenset[str],
    root: Path,
) -> RepositoryIndexRefused:
    return _refuse(
        "repository_roots_not_collectable",
        "the corpus collector accepts only these root names: "
        f"{', '.join(sorted(approved))}; this repository also tracks "
        f"{', '.join(refused)}. Indexing part of a repository would make "
        "'no result' indistinguishable from 'not indexed', so pass `roots` "
        "explicitly to accept a narrower index.",
        directory=str(root),
        refused_roots=list(refused),
        admissible_roots=[name for name in selected if name in approved],
    )


def _explicit_roots(requested: Iterable[str], root: Path) -> tuple[str, ...]:
    selected = tuple(sorted({str(name) for name in requested}))
    missing = tuple(name for name in selected if not (root / name).is_dir())
    if missing:
        raise _refuse(
            "repository_root_missing",
            f"requested root is not a directory in {root}: {', '.join(missing)}",
            directory=str(root),
            missing_roots=list(missing),
        )
    return selected


def selected_code_roots(root: Path, requested: Iterable[str] | None) -> tuple[str, ...]:
    """Explicit roots when given, otherwise every tracked top-level directory."""
    if requested is not None:
        selected = _explicit_roots(requested, root)
        _require_root_count(selected, root)
        _require_admissible_roots(selected, root)
        return selected
    discovered = tracked_top_level_directories(root)
    _require_root_count(discovered, root)
    _require_admissible_roots(discovered, root)
    return discovered


# --------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------


def _collect(root: Path, roots: tuple[str, ...], deadline: float | None):
    from corpus_snapshot import collect_corpus

    try:
        return collect_corpus(
            root,
            code_roots=roots,
            max_files=MAX_INDEXED_SOURCES,
            deadline=deadline,
        )
    except TimeoutError:
        raise
    except (OSError, ValueError) as error:
        raise _refuse(
            "repository_exceeds_corpus_bounds",
            f"the corpus collector refused this repository: {error}",
            directory=str(root),
            roots=list(roots),
        ) from error


# --------------------------------------------------------------------------
# index
# --------------------------------------------------------------------------


def _newest_generation_for(catalog, scope, deadline: float | None):
    """The newest generation already registered for this repository, or None."""
    from generation_catalog import _manifest_belongs_to

    for identifier, _registered_at, manifest in catalog.registered_manifests(
        deadline=deadline
    ):
        if _manifest_belongs_to(manifest, scope):
            return identifier, manifest
    return None, None


def _reuse_config(snapshot, workspace_sha256: str):
    import doctor
    from evidence_graph_builder import GRAPH_SCHEMA_VERSION, IncrementalReuseConfig

    return IncrementalReuseConfig(
        extractor_version=doctor._maintenance_extractor_identity(),  # noqa: SLF001
        grammar_version="builtin-grammars/v1",
        compiler_version=f"python-{sys.version_info.major}.{sys.version_info.minor}",
        resolver_config_sha256=hashlib.sha256(
            b"llm-wiki-foreign-repository-resolver/v1"
        ).hexdigest(),
        schema_version=GRAPH_SCHEMA_VERSION,
        workspace_manifest_sha256=workspace_sha256,
    )


def _fresh_generation_id(catalog) -> str:
    import doctor

    return doctor._fresh_generation_id(catalog)  # noqa: SLF001


def _build(catalog, admission: Admission, snapshot, parent_id, deadline, cancelled):
    """Build and register; `activate=False` is the whole safety property here."""
    import doctor
    from evidence_graph_builder import build_incremental_generation

    return build_incremental_generation(
        catalog,
        sources=doctor._generation_source_rows(snapshot),  # noqa: SLF001
        source_bytes=doctor._generation_source_bytes(snapshot),  # noqa: SLF001
        extractor=doctor._generation_source_extractor(  # noqa: SLF001
            snapshot, admission.scope.repository_id
        ),
        reuse_config=_reuse_config(
            snapshot,
            doctor._workspace_manifest_sha256(snapshot),  # noqa: SLF001
        ),
        generation_id=_fresh_generation_id(catalog),
        parent_generation_id=parent_id,
        policy=doctor._corpus_policy(snapshot),  # noqa: SLF001
        activate=False,
        deadline=deadline,
        cancelled=cancelled,
        repository_scope=admission.scope,
        snapshot=snapshot,
        # No `publication_root`: it fences live bytes for an *activation*, and
        # nothing here activates. Passing the foreign root would be the one
        # place this module reached back into a repository it must only read.
    )


def _index_receipt(admission: Admission, built, snapshot, roots, parent_id, seconds):
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "indexed",
        "directory": str(admission.root),
        "repository_id": admission.scope.repository_id,
        "checkout_id": admission.scope.checkout_id,
        "git_commit": admission.scope.git_commit,
        "generation_id": built.generation_id,
        "parent_generation_id": parent_id,
        "activated": False,
        "code_roots": list(roots),
        "sources": len(snapshot.sources),
        "chunks": len(snapshot.chunks),
        "rebuilt_sources": len(built.rebuilt_sources),
        "reused_sources": len(built.reused_sources),
        "ownership_checked": admission.ownership_checked,
        "seconds": round(seconds, 2),
        "disk_bytes": _generation_bytes(built.generation_path),
    }


def _generation_bytes(generation_path: Path) -> int:
    total = 0
    for entry in sorted(Path(generation_path).iterdir()):
        if entry.is_file():
            total += entry.stat().st_size
    return total


def index_repository(
    directory: Path | str,
    *,
    roots: Iterable[str] | None = None,
    state_root: Path | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Build and register one generation for a repository that is not the vault."""
    started = time.monotonic()
    admission = admit_repository(
        directory, state_root=state_root, deadline=deadline, cancelled=cancelled
    )
    selected = selected_code_roots(admission.root, roots)
    snapshot = _collect(admission.root, selected, deadline)
    catalog = _open_catalog(state_root_path(state_root), read_only=False)
    parent_id, _parent = _newest_generation_for(catalog, admission.scope, deadline)
    built = _build(catalog, admission, snapshot, parent_id, deadline, cancelled)
    return _index_receipt(
        admission, built, snapshot, selected, parent_id, time.monotonic() - started
    )


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def _repository_row(identifier: str, registered_at: str, manifest: Mapping) -> dict:
    """One generation, flattened. `code_roots` is filled in later; see `_covered`.

    The generation manifest deliberately does not carry the snapshot policy --
    that lives in `source-manifest.json`, whose digest the manifest binds -- so
    what a generation actually covered costs one extra verified file read, paid
    once per repository rather than once per generation.
    """
    scope = manifest.get("repository_scope") or {}
    return {
        "repository_id": scope.get("repository_id"),
        "checkout_id": scope.get("checkout_id"),
        "checkout_root": scope.get("checkout_root"),
        "git_commit": scope.get("git_commit"),
        "generation_id": identifier,
        "registered_at": registered_at,
        "graph_schema_version": manifest.get("graph_schema_version"),
        "vector_state": manifest.get("vector_state"),
        "manifest": manifest,
        "generations": 1,
    }


def _covered(catalog, row: dict) -> list[str] | None:
    """The code roots this generation covered, or None when that cannot be read."""
    try:
        source_manifest = _verified_source_manifest(
            catalog, row["generation_id"], row.pop("manifest")
        )
    except (RepositoryIndexRefused, OSError, ValueError):
        return None
    return list((source_manifest.get("policy") or {}).get("code_roots") or [])


def _merge_repository_rows(rows: Iterable[dict]) -> list[dict]:
    """One row per checkout, newest generation winning, older ones only counted."""
    merged: dict[object, dict] = {}
    for row in rows:
        key = (row["repository_id"], row["checkout_id"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        existing["generations"] += 1
    return list(merged.values())


def _active_generation_id(catalog, deadline: float | None) -> str | None:
    try:
        active = catalog.get_active(deadline=deadline)
    except (OSError, ValueError):
        return None
    return None if active is None else str(active["generation_id"])


def list_repositories(
    *,
    state_root: Path | None = None,
    deadline: float | None = None,
) -> dict[str, object]:
    """Every repository with a registered generation, newest generation first."""
    catalog = _open_catalog(state_root_path(state_root), read_only=True)
    if catalog is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "repositories": [],
            "active_generation_id": None,
            "truncated": False,
        }
    manifests = catalog.registered_manifests(deadline=deadline)
    active_id = _active_generation_id(catalog, deadline)
    rows = _merge_repository_rows(
        _repository_row(identifier, registered_at, manifest)
        for identifier, registered_at, manifest in manifests
    )
    return _listing(catalog, rows, active_id)


def _listing(catalog, rows: list[dict], active_id: str | None) -> dict[str, object]:
    for row in rows:
        row["active"] = row["generation_id"] == active_id
        row["code_roots"] = _covered(catalog, row)
    ordered = sorted(rows, key=lambda row: str(row["registered_at"]), reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "repositories": ordered[:MAX_LISTED_REPOSITORIES],
        "active_generation_id": active_id,
        "truncated": len(ordered) > MAX_LISTED_REPOSITORIES,
    }


# --------------------------------------------------------------------------
# detect
# --------------------------------------------------------------------------


def _artifact_digest(manifest: Mapping, name: str) -> str | None:
    for artifact in manifest.get("artifacts") or []:
        if artifact.get("path") == name:
            return str(artifact.get("sha256"))
    return None


def _verified_source_manifest(catalog, generation_id: str, manifest: Mapping) -> dict:
    """Read `source-manifest.json` only after its bytes match the manifest digest."""
    path = Path(catalog.generations_path) / generation_id / "source-manifest.json"
    expected = _artifact_digest(manifest, "source-manifest.json")
    raw = path.read_bytes()
    if expected is None or hashlib.sha256(raw).hexdigest() != expected:
        raise _refuse(
            "repository_index_unreadable",
            "the recorded source manifest does not match its digest; re-index",
            generation_id=generation_id,
        )
    return json.loads(raw)


def _recorded_hashes(source_manifest: Mapping) -> dict[str, str]:
    return {
        str(source["relative_path"]): str(source["sha256"])
        for source in source_manifest.get("sources") or []
    }


def _current_hashes(snapshot) -> dict[str, str]:
    return {
        source.record.relative_path: source.record.sha256 for source in snapshot.sources
    }


def _difference(recorded: Mapping[str, str], current: Mapping[str, str]) -> dict:
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    modified = sorted(
        path
        for path in set(recorded) & set(current)
        if recorded[path] != current[path]
    )
    return {"added": added, "removed": removed, "modified": modified}


def _change_report(admission: Admission, generation_id: str, roots, difference: dict):
    counts = {kind: len(paths) for kind, paths in difference.items()}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "directory": str(admission.root),
        "repository_id": admission.scope.repository_id,
        "generation_id": generation_id,
        "git_commit": admission.scope.git_commit,
        "code_roots": list(roots),
        "stale": any(counts.values()),
        "counts": counts,
        "added": difference["added"][:MAX_REPORTED_PATHS],
        "removed": difference["removed"][:MAX_REPORTED_PATHS],
        "modified": difference["modified"][:MAX_REPORTED_PATHS],
        "paths_truncated": any(
            len(paths) > MAX_REPORTED_PATHS for paths in difference.values()
        ),
    }


def _not_indexed(admission: Admission) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "not_indexed",
        "directory": str(admission.root),
        "repository_id": admission.scope.repository_id,
        "generation_id": None,
        "stale": True,
        "message": "this repository has no registered generation; index it first",
    }


def detect_repository_changes(
    directory: Path | str,
    *,
    state_root: Path | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """What changed in a repository since its newest generation was built.

    The comparison is by content digest, not by mtime: an edit that preserves
    size and timestamp is still an edit, and a caller asking whether the index
    is stale is asking exactly that.
    """
    admission = admit_repository(
        directory, state_root=state_root, deadline=deadline, cancelled=cancelled
    )
    catalog = _open_catalog(state_root_path(state_root), read_only=True)
    if catalog is None:
        return _not_indexed(admission)
    generation_id, manifest = _newest_generation_for(
        catalog, admission.scope, deadline
    )
    if generation_id is None:
        return _not_indexed(admission)
    return _detected(catalog, admission, generation_id, manifest, deadline)


def _detected(catalog, admission: Admission, generation_id, manifest, deadline):
    source_manifest = _verified_source_manifest(catalog, generation_id, manifest)
    roots = tuple((source_manifest.get("policy") or {}).get("code_roots") or ())
    snapshot = _collect(admission.root, roots, deadline)
    difference = _difference(
        _recorded_hashes(source_manifest), _current_hashes(snapshot)
    )
    return _change_report(admission, generation_id, roots, difference)
