"""Doctor's checks of the active evidence generation and its live corpus."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import generation_maintenance  # noqa: E402
from llm_wiki import doctor_base, monotonic_deadline
from llm_wiki.doctor_base import (
    MAX_MANIFEST_BYTES,
    MAX_OPERATIONAL_ROWS,
    database_tables,
    read_bounded_json,
)
from llm_wiki.doctor_lsp import generation_result
from reliable_memory import (
    read_runtime_bytes,
)
from settings import (
    setting_value,
)

# Above what the state's own writer can produce: `integration_adapter` keeps at
# most 40 pending checkpoint items per project, and 91 projects made 354 KiB on
# 2026-09-23 — over the old 256 KiB, which silenced the scheduler and capture
# checks exactly when they had something to say.
MAX_STATE_BYTES = 4 * 1024 * 1024


# A generation is stale once sources changed and a day passed without a refresh: the
# nightly refreshes it daily, so one missed nightly is what this reports
# (docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md).
GENERATION_FRESH_SECONDS = 24 * 60 * 60


class GenerationFacts(NamedTuple):
    delta: int
    unresolved: int
    extraction_faults: int
    scope_state: str
    corpus_extraction_state: str
    graph_extraction_state: str
    capacity_usage: dict | None = None


def _require_catalog_integrity(database: sqlite3.Connection, deadline: float) -> None:
    integrity = database.execute("PRAGMA integrity_check(1)").fetchone()
    tables = database_tables(database, deadline)
    required = {"generations", "catalog_state", "activation_history"}
    if integrity is None or integrity[0] != "ok" or not required.issubset(tables):
        raise sqlite3.DatabaseError("catalog integrity or schema failed")


def _require_catalog_durability(database: sqlite3.Connection) -> None:
    journal = str(database.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    synchronous = database.execute("PRAGMA synchronous").fetchone()[0]
    if journal != "delete" or synchronous != 2:
        raise sqlite3.DatabaseError("catalog durability contract failed")


def _registered_generation_count(database) -> int:
    row = database.execute("SELECT COUNT(*) FROM generations").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _no_active_generation(registered: int) -> dict:
    """Nothing to answer from: never built is fine, deactivated is not.

    A vault that has never published a generation is simply young, and legacy
    retrieval is all it ever had. A vault that published some and now points at
    none has lost its main read path — measured here on 2026-08-24, when a corpus
    rule change invalidated every candidate and the pointer was cleared: search
    returned zero rows while the report still said `ok`. The finding clears
    itself as soon as a generation is activated again.
    """
    if registered == 0:
        return generation_result(
            "ok",
            "Evidence generation has not been activated; legacy retrieval remains available.",
            catalog="valid",
            catalog_schema="valid",
            freshness="missing",
            repairable=True,
            recommended_action="rebuild_generation",
        )
    return generation_result(
        "degraded",
        (
            f"No evidence generation is active while {registered} are registered; "
            "retrieval has fallen back and semantic search is off until one is "
            "activated."
        ),
        catalog="valid",
        catalog_schema="valid",
        freshness="missing",
        repairable=True,
        recommended_action="rebuild_generation",
    )


def _catalog_active_generation(
    catalog_path: Path, state_root: Path, deadline: float, state: dict
) -> tuple[dict | None, str | None]:
    """The active generation id, or the result to return when there is none."""
    with doctor_base.readonly_database(catalog_path, state_root, deadline=deadline) as database:
        database.set_progress_handler(lambda: int(monotonic_deadline.deadline_reached(deadline)), 1000)
        _require_catalog_integrity(database, deadline)
        _require_catalog_durability(database)
        active = generation_maintenance.active_pointer(database)
        if not isinstance(active, str) or not active:
            return _no_active_generation(_registered_generation_count(database)), None
        registered = database.execute(
            "SELECT 1 FROM generations WHERE generation_id=?",
            (active,),
        ).fetchone()
        if registered is None:
            raise sqlite3.DatabaseError("active generation is not registered")
        state["invalid_details"].update(
            catalog="valid",
            active_generation=active,
            catalog_schema="valid",
        )
    return None, active


def _validated_generation_manifest(
    generation_path: Path, state_root: Path, deadline: float, state: dict
) -> tuple[dict, tuple]:
    from llm_wiki import generation_limits, generation_validation

    diagnostic_value = json.loads(
        read_runtime_bytes(
            generation_path / "manifest.json",
            state_root,
            max_bytes=generation_limits.MAX_MANIFEST_BYTES,
        )
    )
    if isinstance(diagnostic_value, dict):
        state["diagnostic_manifest"] = diagnostic_value
        state["invalid_details"]["generation_schema"] = diagnostic_value.get(
            "graph_schema_version"
        )
    return generation_validation.validate_generation(  # noqa: SLF001
        generation_path,
        state_root,
        deadline=deadline,
    )


def scope_state(manifest: dict, repository_scope: object) -> str:
    """Whether the active generation belongs here, and whether it is current.

    Comparing the whole scope made every commit read as `mismatched`, which
    says the generation belongs to another repository. It does not: only the
    commit moved, and how far behind the generation is already has its own
    signals. `superseded` is treated exactly like a mismatch by every caller —
    it only stops the report from saying something untrue.
    """
    from repository_scope import same_repository_record

    recorded = manifest.get("repository_scope")
    if recorded is None:
        return "missing"
    current = repository_scope.as_dict()
    if recorded == current:
        return "current"
    return "superseded" if same_repository_record(recorded, current) else "mismatched"


def _corpus_extraction_state(
    manifest: dict, collector_version: str, extractor_version: str
) -> str:
    if manifest.get("collector_version") != collector_version:
        return "stale"
    if manifest.get("extractor_version") != extractor_version:
        return "stale"
    return "current"


def _graph_extraction_state(manifest: dict) -> str:
    if manifest.get("graph_extractor_version") == generation_maintenance.maintenance_extractor_identity():
        return "current"
    return "stale"


def _source_delta(source_manifest: dict, snapshot: object) -> int:
    indexed = {
        item["relative_path"]: item["sha256"] for item in source_manifest["sources"]
    }
    current = {
        source.record.relative_path: source.record.sha256
        for source in snapshot.sources
    }
    return sum(
        indexed.get(path) != current.get(path)
        for path in indexed.keys() | current.keys()
    )


# What an observation's reason says about who is at fault. A reference the
# language cannot resolve statically, or a dependency the repository does not
# vendor, is the normal outcome of indexing real code and says nothing about
# the health of the generation. A parse error is the extractor failing at its
# own job, and is the one that should be surfaced.
_UNRESOLVED_REASONS = ("missing_dependency", "unresolved_reference")
_EXTRACTION_FAULT_REASONS = ("parse_error",)


def _count_observations(
    generation_path: Path, state_root: Path, deadline: float, reasons: Sequence[str]
) -> int:
    """How many observations carry one of these reasons, up to the row bound."""
    graph = generation_path / "evidence.sqlite3"
    placeholders = ",".join("?" for _ in reasons)
    with doctor_base.readonly_database(
        graph, state_root, max_bytes=16 * 1024 * 1024 * 1024, deadline=deadline
    ) as database:
        database.set_progress_handler(lambda: int(monotonic_deadline.deadline_reached(deadline)), 1000)
        return database.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM observation "  # noqa: S608
            f"WHERE reason IN ({placeholders}) LIMIT ?)",
            (*reasons, MAX_OPERATIONAL_ROWS + 1),
        ).fetchone()[0]


def _unresolved_observations(
    generation_path: Path, state_root: Path, deadline: float
) -> int:
    """References the graph could not resolve. Reported, never a health failure."""
    return _count_observations(
        generation_path, state_root, deadline, _UNRESOLVED_REASONS
    )


def _extraction_faults(
    generation_path: Path, state_root: Path, deadline: float
) -> int:
    """Files the extractor could not parse: its own failure, not the language's."""
    return _count_observations(
        generation_path, state_root, deadline, _EXTRACTION_FAULT_REASONS
    )


class _LiveCorpusUnverified(RuntimeError):
    """Live-source collection failed after immutable artifacts were validated."""


def collect_generation_live_corpus(root, policy, max_sources, deadline):
    from corpus_snapshot import CorpusCapacityExceeded, CorpusChanged, collect_corpus
    from secret_redact import describe_error

    try:
        return collect_corpus(
            root, daily_paths=policy["daily_paths"], code_roots=policy["code_roots"],
            include_historical=policy["include_historical"], as_of=policy["as_of"],
            max_files=max_sources, deadline=deadline,
        )
    except (CorpusCapacityExceeded, TimeoutError):
        raise
    except (CorpusChanged, OSError, ValueError) as error:
        raise _LiveCorpusUnverified(describe_error(error)) from error


def _generation_facts(
    root: Path,
    state_root: Path,
    generation_path: Path,
    manifest: dict,
    max_sources: int,
    deadline: float,
    cancelled,
) -> GenerationFacts:
    """What the live sources and the stored generation currently say."""
    source_manifest = json.loads(
        read_runtime_bytes(
            generation_path / "source-manifest.json",
            state_root,
            max_bytes=MAX_MANIFEST_BYTES * 1024,
        )
    )
    policy = source_manifest["policy"]

    from corpus_snapshot import COLLECTOR_VERSION
    from llm_wiki import extractor_versions
    from repository_scope import resolve_repository_scope

    repository_scope = resolve_repository_scope(
        root, deadline=deadline, cancelled=cancelled
    )
    snapshot = collect_generation_live_corpus(root, policy, max_sources, deadline)
    return GenerationFacts(
        delta=_source_delta(source_manifest, snapshot),
        unresolved=_unresolved_observations(generation_path, state_root, deadline),
        extraction_faults=_extraction_faults(generation_path, state_root, deadline),
        scope_state=scope_state(manifest, repository_scope),
        corpus_extraction_state=_corpus_extraction_state(
            manifest, COLLECTOR_VERSION, extractor_versions.CORPUS_EXTRACTOR_VERSION
        ),
        graph_extraction_state=_graph_extraction_state(manifest),
        capacity_usage=generation_capacity_usage(snapshot, root),
    )


def _generation_age_source(seal: tuple, catalog_info) -> tuple[int, str]:
    manifest_seal = next(
        (entry for entry in seal if entry.path == "manifest.json"), None
    )
    if manifest_seal is not None:
        return manifest_seal.mtime_ns, "manifest_mtime"
    if catalog_info is not None:
        return catalog_info.st_mtime_ns, "catalog_mtime"
    raise OSError("generation age timestamp is unavailable")


def _generation_age(seal: tuple, catalog_info, now: datetime) -> tuple[int, str]:
    timestamp_ns, age_source = _generation_age_source(seal, catalog_info)
    now_ns = int(generation_maintenance.as_utc(now).timestamp() * 1_000_000_000)
    return max(0, (now_ns - timestamp_ns) // 1_000_000_000), age_source


def _identity_stale(facts: GenerationFacts, complete_v2: bool) -> bool:
    if facts.scope_state != "current" or facts.corpus_extraction_state != "current":
        return True
    return facts.graph_extraction_state != "current" or not complete_v2


# The search artifact family a validated generation carries. The artifact names its own
# version (`corpus-search/v1` without the keys column, `/v2` with it) and the validator
# accepts both, so health names the family rather than guessing one version for all.
# See `docs/research/2026-09-17-one-table-one-scale-for-the-keys.md`.
_SEARCH_SCHEMA_FAMILY = "corpus-search"


def _generation_search_fields(complete_v2: bool) -> dict:
    if not complete_v2:
        return {
            "search_index": "missing",
            "search_schema": None,
            "search_integrity": "missing",
        }
    return {
        "search_index": "valid",
        "search_schema": _SEARCH_SCHEMA_FAMILY,
        "search_integrity": "valid",
    }


def generation_message(degraded: bool, extraction_faults: int) -> str:
    """What this generation's state is, saying so when files would not parse."""
    if degraded:
        return "Evidence generation requires refresh."
    if extraction_faults:
        return f"Evidence generation is healthy; {extraction_faults} file(s) did not parse."
    return "Evidence generation is healthy."


def generation_is_stale(facts: GenerationFacts, age: float, complete_v2: bool) -> bool:
    """Stale when its identity is, or when sources changed and a day passed unrefreshed.

    Not for a delta alone: every capture appends to a daily log, so that made the
    vault degraded for most of every day. Not for age alone: an unchanged vault's
    refresh builds nothing, so its age grows while it is current. See
    `docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.
    """
    if facts.delta and age > GENERATION_FRESH_SECONDS:
        return True
    return _identity_stale(facts, complete_v2)


def _generation_health_result(
    active: str,
    manifest: dict,
    seal: tuple,
    catalog_info,
    now: datetime,
    facts: GenerationFacts,
) -> dict:
    age, age_source = _generation_age(seal, catalog_info, now)
    vector_state = str(manifest["vector_state"])
    complete_v2 = manifest.get("schema_version") == "corpus-generation/v2"
    stale = generation_is_stale(facts, age, complete_v2)
    # What this status answers is whether the generation is usable and current,
    # so it degrades on the things a refresh fixes. An unresolved reference is
    # what indexing real code looks like — this repository alone has 21199 of
    # them and 5481 missing dependencies — and a file that will not parse is
    # usually one the repository keeps deliberately broken. Counting either as
    # ill health left every real vault permanently degraded and pointed the
    # operator at a refresh that changes nothing. Both are still reported, and
    # a parse error is named rather than buried.
    degraded = stale or vector_state == "stale"
    message = generation_message(degraded, facts.extraction_faults)
    result = generation_result(
        "degraded" if degraded else "ok",
        message,
        catalog="valid",
        active_generation=active,
        catalog_schema="valid",
        generation_schema=manifest["graph_schema_version"],
        source_manifest="valid",
        evidence_integrity="valid",
        vector_state=vector_state,
        vector_model=manifest["embedding_model_id"],
        vector_dimensions=manifest["vector_dimensions"],
        freshness="stale" if stale else "fresh",
        repository_scope=facts.scope_state,
        extraction_identity=facts.graph_extraction_state,
        corpus_extraction_identity=facts.corpus_extraction_state,
        unindexed_delta=facts.delta,
        unresolved_observations=facts.unresolved,
        extraction_faults=facts.extraction_faults,
        age_seconds=age,
        age_source=age_source,
        repairable=degraded,
        **_generation_search_fields(complete_v2),
    )
    return _with_generation_capacity(result, facts.capacity_usage)


def _capacity_use(used, ceiling, setting):
    return {"used": used, "ceiling": ceiling, "setting": setting, "share": used / ceiling}


def generation_capacity_usage(snapshot, root):
    if getattr(snapshot, "inspected_entries", None) is None:
        return None
    policy = snapshot.policy
    extraction_ceiling = setting_value("extraction.max_sources", root)
    return {
        "entries": _capacity_use(snapshot.inspected_entries, policy.max_entries, "corpus.max_files"),
        "sources": _capacity_use(snapshot.discovered_sources, policy.max_files, "corpus.max_files"),
        "bytes": _capacity_use(snapshot.discovered_bytes, policy.max_total_bytes, "corpus.max_total_bytes"),
        "knowledge_extraction_sources": _capacity_use(
            len(generation_maintenance.knowledge_extraction_sources(snapshot)),
            extraction_ceiling, "extraction.max_sources",
        ),
        "project_extraction_sources_per_call": _capacity_use(
            _project_extraction_batch_size(snapshot), extraction_ceiling, "extraction.max_sources",
        ),
    }


def _project_extraction_batch_size(snapshot):
    # _project_extraction passes a singleton, even when many projects exist.
    return int(any(
        generation_maintenance.is_memory_source(snapshot, source)
        and source.record.relative_path.startswith("knowledge/projects/")
        for source in snapshot.sources
    ))


def _with_generation_capacity(result, usage):
    result["details"]["capacity_usage"] = usage
    if usage is None:
        return result
    near = [name for name, item in usage.items() if item["share"] >= CEILING_WARNING_SHARE]
    result["details"]["capacity_near_ceiling"] = near
    if near:
        result["status"] = "degraded"
        result["message"] += " Corpus capacity has reached a warning threshold; measure resource capacity before changing its limits."
    return result


def _validated_search_artifact(
    generation_path: Path, diagnostic: dict, state_root: Path, deadline: float
) -> dict:
    try:
        from generation_fts_validation import (
            validate_generation_fts_artifact,
        )

        # The operator's own deep check: the read path trusts the digest, and
        # this is where a re-derivation belongs (2026-09-12).
        validate_generation_fts_artifact(
            generation_path,
            diagnostic,
            state_root=state_root,
            deep=True,
            deadline=deadline,
        )
    except (OSError, PermissionError, TypeError, ValueError, sqlite3.Error):
        return {"search_index": "corrupt", "search_integrity": "invalid"}
    return {"search_index": "valid", "search_integrity": "valid"}


def _diagnostic_search_state(
    generation_path: Path, diagnostic: dict, state_root: Path, deadline: float
) -> dict:
    search_kind = generation_maintenance.safe_kind(generation_path / "search.sqlite3", state_root)[0]
    if search_kind == "missing":
        return {"search_index": "missing", "search_integrity": "missing"}
    if search_kind != "regular":
        return {"search_index": "corrupt", "search_integrity": "invalid"}
    return _validated_search_artifact(
        generation_path, diagnostic, state_root, deadline
    )


def _diagnose_invalid_generation(
    state: dict, state_root: Path, deadline: float
) -> None:
    """Say what the search artifact looks like when the generation is invalid."""
    generation_path = state["generation_path"]
    diagnostic = state["diagnostic_manifest"]
    if generation_path is None or not isinstance(diagnostic, dict):
        return
    if diagnostic.get("schema_version") != "corpus-generation/v2":
        return
    state["invalid_details"]["search_schema"] = _SEARCH_SCHEMA_FAMILY
    state["invalid_details"].update(
        _diagnostic_search_state(generation_path, diagnostic, state_root, deadline)
    )


def _checked_generation(
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float,
    catalog_path: Path,
    catalog_info,
    max_sources: int,
    cancelled,
    state: dict,
) -> dict:
    from corpus_snapshot import CorpusCapacityExceeded

    early, active = _catalog_active_generation(
        catalog_path, state_root, deadline, state
    )
    if early is not None:
        return early
    if monotonic_deadline.deadline_reached(deadline):
        raise TimeoutError("generation check deadline")
    generation_path = state_root / "cache" / "evidence-graph" / "generations" / active
    state["generation_path"] = generation_path
    manifest, seal = _validated_generation_manifest(
        generation_path, state_root, deadline, state
    )
    try:
        facts = _generation_facts(
            root, state_root, generation_path, manifest, max_sources, deadline, cancelled
        )
    except CorpusCapacityExceeded as error:
        return _generation_capacity_refusal(active, manifest, error)
    except _LiveCorpusUnverified as error:
        return _generation_live_source_refusal(active, manifest, error)
    return _generation_health_result(active, manifest, seal, catalog_info, now, facts)


def _generation_live_source_refusal(active: str, manifest: dict, error) -> dict:
    return generation_result(
        "error", "Live corpus could not be verified; freshness is unknown. " + str(error),
        catalog="valid", active_generation=active,
        generation_schema=manifest.get("graph_schema_version"),
        live_corpus_state="unverified", validation_error=str(error),
        freshness="unknown", partial=True, repairable=False,
        recommended_action="review_live_corpus", **_vector_fields(manifest),
    )


def _generation_capacity_refusal(active: str, manifest: dict, error) -> dict:
    return generation_result(
        "degraded",
        "Live corpus exceeded its configured budget; freshness is unverified. " + str(error),
        catalog="valid",
        active_generation=active,
        generation_schema=manifest.get("graph_schema_version"),
        capacity_exceeded=True,
        limit_name=error.setting,
        configured_limit=error.limit,
        observed_minimum=error.observed,
        freshness="unknown",
        partial=True,
        repairable=False,
        recommended_action="review_corpus_budget",
        **_vector_fields(manifest),
    )


def _quick_vector_state(state_root: Path) -> dict | None:
    """The active generation's vector fields from its manifest, or None."""
    from llm_wiki.generation_catalog_store import GenerationCatalog

    try:
        active = GenerationCatalog(state_root).get_active() or {}
        generation = str(active.get("generation_id") or "")
        path = Path(state_root) / "cache" / "evidence-graph" / "generations" / generation / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a hint, never a failure
        return None
    return {key: manifest.get(key) for key in ("vector_state", "embedding_model_id", "vector_dimensions")}


def _vector_hint(vector: dict | None) -> str:
    if not vector or vector.get("vector_state") != "absent":
        return ""
    return " Vectors are absent: run `uv run python scripts/doctor.py --repair` after installing the semantic extra."


def _vector_fields(vector: dict | None) -> dict:
    if not vector:
        return {}
    return {
        "vector_state": vector.get("vector_state") or "unknown",
        "vector_model": vector.get("embedding_model_id"),
        "vector_dimensions": vector.get("vector_dimensions"),
    }


def generation_check(
    root: Path,
    state_root: Path,
    now: datetime,
    deadline: float = float("inf"),
    *,
    max_sources: int | None = None,
    cancelled=None,
) -> dict:
    """Validate the catalog-selected immutable generation without writing."""
    max_sources = generation_maintenance.generation_source_limit(max_sources, root)
    generation_maintenance.require_positive_source_limit(max_sources)
    catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
    kind, catalog_info = generation_maintenance.safe_kind(catalog_path, state_root)
    if kind == "missing":
        # Since 2026-09-23 there is no legacy index: until a generation is
        # built, a search reads Markdown directly and says so. The installer's
        # sync and `doctor --repair` build the first one from this verdict.
        return generation_result(
            "degraded",
            "No evidence generation has been built; search reads Markdown directly until one is.",
            catalog="missing",
            freshness="missing",
            repairable=True,
            recommended_action="rebuild_generation",
        )
    if kind != "regular":
        return generation_result(
            "error",
            "Evidence generation catalog is unsafe.",
            catalog="invalid",
            repairable=False,
        )
    state: dict[str, Any] = {
        "invalid_details": {"catalog": "invalid", "repairable": True},
        "diagnostic_manifest": None,
        "generation_path": None,
    }
    try:
        return _checked_generation(
            root,
            state_root,
            now,
            deadline,
            catalog_path,
            catalog_info,
            max_sources,
            cancelled,
            state,
        )
    except TimeoutError:
        # The full check did not fit, but the active manifest is one small
        # file: its vector state is reported regardless of the budget (issue
        # #29 found "vector_state: unknown" hiding absent vectors for a day).
        vector = _quick_vector_state(state_root)
        return generation_result(
            "degraded",
            "Evidence generation check was deferred by its time bound."
            + _vector_hint(vector),
            catalog="valid",
            budget_exhausted=vector is None,
            partial=True,
            repairable=False,
            **_vector_fields(vector),
        )
    except (
        KeyError,
        OSError,
        PermissionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ):
        _diagnose_invalid_generation(state, state_root, deadline)
        return generation_result(
            "error",
            "Evidence generation catalog or active artifacts are invalid.",
            **state["invalid_details"],
        )


def state_size_hint(state_root: Path) -> str:
    """Why the state file could not be read, in the numbers that say it.

    Two checks report "could not be read within safety bounds" and neither says
    what the bound was or how far past it the file is. On this vault on
    2026-09-07 the answer was 2.8 MB against a 256 KiB bound, all of it one
    project's undrained checkpoint queue — a diagnosis the operator had to
    reach with a Python one-liner. The reader stays bounded; only the sentence
    changes.
    """
    path = Path(state_root) / "run" / "state.json"
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    return f" run/state.json is {size} bytes against a {MAX_STATE_BYTES}-byte bound."


def read_state(state_root: Path, deadline: float) -> tuple[dict, str | None]:
    path = state_root / "run" / "state.json"
    if generation_maintenance.safe_kind(path, state_root)[0] == "missing":
        return {}, None
    value, problem = read_bounded_json(
        path,
        state_root,
        max_bytes=MAX_STATE_BYTES,
        deadline=deadline,
    )
    return (value or {}, problem)


def deferred_sentence(details: dict) -> str:
    deferred = int(details.get("deferred", 0))
    if not deferred:
        return ""
    return (
        f" {deferred} write(s) were deferred by a writer race and retried;"
        " they are not counted as lost."
    )


# Warn at 80 % of a vault-size ceiling: the research note's rule, so the operator
# sees the stop coming while a quarter of the ceiling's growth is still left
# (docs/research/2026-09-27-every-limit-states-its-reason.md).
CEILING_WARNING_SHARE = 0.8
