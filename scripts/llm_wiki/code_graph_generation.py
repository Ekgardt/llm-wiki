"""Opening the repository's active evidence generation under a deadline and reading stored locations."""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

from . import runtime_roots


def _valid_monotonic_deadline(deadline: object) -> bool:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    return math.isfinite(deadline)


def _require_valid_deadline(deadline: object) -> None:
    if not _valid_monotonic_deadline(deadline):
        raise ValueError("deadline must be an absolute monotonic timestamp or None")


def _require_generation_deadline(deadline: float | None) -> None:
    if deadline is None:
        return
    _require_valid_deadline(deadline)
    if time.monotonic() >= deadline:
        raise TimeoutError("generation catalog deadline reached")


def check_generation_stop(deadline: float | None, cancelled) -> None:
    _require_generation_deadline(deadline)
    if cancelled is not None and cancelled():
        raise TimeoutError("generation catalog operation cancelled")


def _bounded_call(factory, *args, deadline, cancelled):
    """Pass deadline/cancelled through only when the caller supplied either."""
    if deadline is None and cancelled is None:
        return factory(*args)
    return factory(*args, deadline=deadline, cancelled=cancelled)


def _new_generation_catalog(
    catalog_class, read_only, state_root, catalog_path, deadline, cancelled
):
    if not read_only:
        return catalog_class(state_root, catalog_path=catalog_path)
    if deadline is None and cancelled is None:
        return catalog_class.open_existing_read_only(
            state_root, catalog_path=catalog_path
        )
    return catalog_class.open_existing_read_only(
        state_root, catalog_path=catalog_path, deadline=deadline, cancelled=cancelled
    )


def generation_catalog(
    directory: Path,
    *,
    read_only: bool = False,
    deadline: float | None = None,
    cancelled=None,
):
    """Open the shared generation catalog only when it already exists."""
    del directory
    if not isinstance(read_only, bool):
        raise TypeError("read_only must be a boolean")
    check_generation_stop(deadline, cancelled)
    try:
        from llm_wiki.generation_catalog_store import GenerationCatalog
    except ImportError:
        from llm_wiki.generation_catalog_store import GenerationCatalog

    state_root = runtime_roots.state_root()
    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    check_generation_stop(deadline, cancelled)
    if not catalog_path.is_file():
        return None
    check_generation_stop(deadline, cancelled)
    catalog = _new_generation_catalog(
        GenerationCatalog, read_only, state_root, catalog_path, deadline, cancelled
    )
    check_generation_stop(deadline, cancelled)
    return catalog


def _generation_catalog_for(directory, read_only, deadline, cancelled):
    """Keep the plain one-argument call, which callers and tests substitute for."""
    if read_only or deadline is not None or cancelled is not None:
        return generation_catalog(
            directory, read_only=read_only, deadline=deadline, cancelled=cancelled
        )
    return generation_catalog(directory)


def _active_graph_or_none(
    directory, read_only, deadline, cancelled, graph_class, resolve_scope
):
    check_generation_stop(deadline, cancelled)
    catalog = _generation_catalog_for(directory, read_only, deadline, cancelled)
    if catalog is None:
        return None
    scope = _bounded_call(
        resolve_scope, directory, deadline=deadline, cancelled=cancelled
    )
    check_generation_stop(deadline, cancelled)
    return opened_code_or_active(graph_class, catalog, scope, deadline, cancelled)


def opened_code_or_active(graph_class, catalog, scope, deadline, cancelled):
    """The repository's code generation when it has one, else the active pointer.

    A vault carries a memory generation and a code generation of the same
    checkout, and only the first is ever activated, so a code answer asks for
    the code one first. Every other repository has no memory generation and
    falls straight through. Decision:
    `docs/research/2026-09-12-the-vault-is-a-repository-too.md`.

    The pointer is asked only when the repository has no code generation
    registered at all. One that is registered and cannot be opened is "no usable
    index", never a confident empty answer out of the memory generation. See
    `docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
    """
    code = _bounded_call(
        graph_class.open_code_for_repository, catalog, scope,
        deadline=deadline, cancelled=cancelled,
    )
    if code is not None:
        return code
    registered = _bounded_call(
        catalog.code_generations_for_repository, scope,
        deadline=deadline, cancelled=cancelled,
    )
    if registered:
        return None
    return _bounded_call(
        graph_class.open_active_for_repository, catalog, scope,
        deadline=deadline, cancelled=cancelled,
    )


def leased_active_graph(directory, read_only, deadline, cancelled):
    """The cached validated reader for `directory`, or a fresh one on a miss.

    Measured 2026-09-10: opening a generation cost 458 of a warm answer's
    521 ms, all of it re-validating an immutable artifact. The cache keeps the
    validated reader and reuses it while the catalog, the artifact and the
    checkout's Git state keep their stat identity. See
    `docs/research/2026-09-10-warm-index-answers-inside-the-loop.md`.
    """
    try:
        from . import evidence_reader_cache
        from .evidence_graph import EvidenceGraph, SharedEvidenceGraph
        from .repository_scope import resolve_repository_scope
    except ImportError:
        import evidence_reader_cache
        from evidence_graph import EvidenceGraph, SharedEvidenceGraph
        from repository_scope import resolve_repository_scope

    if not evidence_reader_cache.shared_readers_supported():
        return _active_graph_or_none(
            directory, read_only, deadline, cancelled,
            EvidenceGraph, resolve_repository_scope,
        )
    check_generation_stop(deadline, cancelled)
    key = (str(Path(directory).resolve()), bool(read_only))
    catalog = _generation_catalog_for(directory, read_only, deadline, cancelled)
    if catalog is None:
        evidence_reader_cache.forget(key)
        return None
    return evidence_reader_cache.leased_graph(
        key,
        catalog_path=Path(catalog.catalog_path),
        resolve_scope=lambda: _bounded_call(
            resolve_repository_scope, directory, deadline=deadline, cancelled=cancelled
        ),
        open_graph=lambda scope: opened_code_or_active(
            SharedEvidenceGraph, catalog, scope, deadline, cancelled
        ),
    )


# Audit 3, B26. `None` used to mean three different things: this repository has
# no generation, this repository has one that could not be opened, and a
# programming error inside the opening path (`TypeError` was caught). The first
# is normal and falls back to a live parse; the other two are failures and must
# say so. `PermissionError` was redundant with `OSError`, and `TimeoutError` is
# itself an `OSError`, which is why its clause stands first. Research:
# `docs/research/2026-09-17-graph-a-generation-that-cannot-be-read-is-not-a-generation-that-is-absent.md`.
NO_GENERATION = "no_generation"


class GenerationUnreadable(RuntimeError):
    """This repository has a generation and it could not be opened.

    `reason` names the class of the underlying error and nothing else: no
    message and no path, so the reason can be carried into an answer without
    carrying vault content with it.
    """

    def __init__(self, error: BaseException) -> None:
        self.reason = f"generation_unreadable:{type(error).__name__}"
        super().__init__(self.reason)


def active_evidence_graph(
    directory: Path,
    *,
    read_only: bool = False,
    deadline: float | None = None,
    cancelled=None,
):
    try:
        return leased_active_graph(directory, read_only, deadline, cancelled)
    except TimeoutError:
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        raise GenerationUnreadable(error) from error


def stored_or_reason(attempt) -> tuple[object, str]:
    """The stored answer, and when there is none, why there is none."""
    try:
        return attempt(), NO_GENERATION
    except GenerationUnreadable as error:
        return None, error.reason


def stored_location(graph, node_id: str, _directory: Path) -> tuple[str, int]:
    source_root = Path(graph.repository_scope.checkout_root)
    occurrences = graph.occurrences(node_id, max_rows=1)
    if not occurrences:
        node = graph.node(node_id)
        path = "" if node is None else str(node["metadata"].get("path", ""))
        return (str(source_root / path) if path else "", 0)
    occurrence = occurrences[0]
    return str(source_root / occurrence["relative_path"]), occurrence["line_start"]


def stored_edge_location(graph, assertion_id: str, _directory: Path) -> tuple[str, int]:
    evidence = graph.evidence(assertion_id=assertion_id, max_rows=1)
    if not evidence:
        return "", 0
    span = evidence[0]
    source_root = Path(graph.repository_scope.checkout_root)
    return str(source_root / span["relative_path"]), span["line_start"]


def stored_qualified_name(node: dict[str, object]) -> str:
    owner = str(node["metadata"].get("owner", ""))
    name = str(node["metadata"].get("name", node["identity_key"]))
    return f"{owner}.{name}" if owner else name


def stored_architecture_node(graph, node: dict[str, object], directory: Path) -> dict:
    location = stored_location(graph, node["node_id"], directory)
    return {
        **node["metadata"],
        "node_id": node["node_id"],
        "file": location[0],
        "line": location[1],
    }
