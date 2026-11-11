"""Hybrid search over the vault's knowledge.

Uses Python's built-in sqlite3 + FTS5 for BM25 full-text search over the active
generation. When the `semantic` extra is installed, the pinned E5 encoder runs
through ONNX Runtime (`scripts/onnx_encoder.py`) for the dense leg. Results are
fused via Reciprocal Rank Fusion (RRF) for hybrid ranking.

Costs measured on this vault:
- lexical only: tens of milliseconds, zero optional dependencies
- with vectors: loading the encoder costs about 1.5 s once per process
  (2026-09-25; it was 6.5 s through `sentence-transformers` and `torch`), and
  finds semantically related pages ("database performance" → "N+1 query fix")

Usage:
    uv run python scripts/search_memory.py "auth decision"
    uv run python scripts/search_memory.py "database performance" --semantic
    uv run python scripts/search_memory.py "hook timing gotcha" --limit 5
    uv run python scripts/search_memory.py "JWT" --scope wiki --project your-project
    uv run python scripts/search_memory.py --rebuild  # force index rebuild
    uv run python scripts/search_memory.py --status   # show index stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus_snapshot import (  # noqa: E402
    CorpusSnapshot,
)
from llm_wiki.generation_catalog_store import GenerationCatalog  # noqa: E402
from reliable_memory import validate_runtime_file  # noqa: E402
from settings import setting_value  # noqa: E402

SEARCH_INDEX_COLUMNS = (
    "path", "title", "summary", "body", "project", "timestamp", "slug",
)
# The model, its revision and its prefixes live in one module, because the
# generation builder encodes with the same model and a drifting copy would
# embed questions and pages with different ones.
from generation_fts_validation import (  # noqa: E402
    FTS_CHUNK_SELECT,
    check_generation_stop,
    generation_authoritative_sources,
    generation_sqlite_guard,
    validated_source_manifest,
)
from generation_search import (  # noqa: E402
    active_generation_catalog,
    blank_query,
    built_generation_vectors,
    collect_pages,
    generation_connection,
    generation_consumption_seal,
    generation_consumption_unchanged,
    resolved_generation_embedder,
    seal_observation_lock,
    seal_observations,
)
from generation_search import (  # noqa: E402
    degradation_reasons as degradation_reasons,
)
from generation_search import (  # noqa: E402
    embedder_unavailable_reason as embedder_unavailable_reason,
)
from generation_search import (  # noqa: E402
    publish_generation as _publish_generation,
)
from llm_wiki import runtime_roots  # noqa: E402


def _validate_search_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ValueError("limit must be a positive integer")
    return value


def _cli_search_limit(value: str) -> int:
    try:
        return _validate_search_limit(int(value))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_generation_numpy_vectors(
    snapshot: CorpusSnapshot,
    generation_directory: Path,
    *,
    embedder: object,
    model_id: str,
    model_revision: str,
    dimensions: int,
    reuse_from: Path | None = None,
) -> list[dict[str, object]]:
    """Build an exact NumPy matrix and closed metadata from one chunk sequence."""
    return built_generation_vectors(
        snapshot,
        generation_directory,
        embedder=embedder,
        model_id=model_id,
        model_revision=model_revision,
        dimensions=dimensions,
        reuse_from=reuse_from,
    )[0]


def publish_generation(
    snapshot: CorpusSnapshot,
    vault: Path,
    catalog: GenerationCatalog,
    generation_id: str,
    *,
    expected_active: str | None,
    coordinator: object | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Fence live bytes, register a complete generation, then CAS-activate it."""
    return _publish_generation(
        snapshot,
        vault,
        catalog,
        generation_id,
        expected_active=expected_active,
        coordinator=coordinator,
        deadline=deadline,
        cancelled=cancelled,
    )


_FTS5_TABLE_RE = re.compile(
    r"\bCREATE\s+VIRTUAL\s+TABLE\b.*\bUSING\s+fts5\s*\(", re.IGNORECASE | re.DOTALL
)
_SEARCH_INDEX_MARKERS = (
    "path unindexed",
    "project unindexed",
    "timestamp unindexed",
    "tokenize = 'porter unicode61'",
)


_SEARCH_INDEX_DDL = """
    CREATE VIRTUAL TABLE pages USING fts5(
        path UNINDEXED,
        title,
        summary,
        body,
        project UNINDEXED,
        timestamp UNINDEXED,
        slug,
        tokenize = 'porter unicode61'
    )
    """


def reset_seal_observations() -> None:
    """Tests that rewrite an artifact in place need the next seal to re-hash."""
    with seal_observation_lock:
        seal_observations.clear()


_ANSWER_CORPUS_ARTIFACTS = ("evidence.sqlite3", "search.sqlite3", "source-manifest.json")


def published_corpus(vault: Path, *, deadline: float) -> CorpusSnapshot | None:
    """The verified published read model; selected live sources still need rechecking."""
    from repository_scope import resolve_repository_scope

    catalog_path = runtime_roots.state_root() / "cache/evidence-graph/catalog.sqlite3"
    if not catalog_path.exists():
        return None
    catalog = GenerationCatalog(runtime_roots.state_root(), catalog_path=catalog_path)
    scope = resolve_repository_scope(vault, deadline=deadline)
    manifest = catalog.get_active_for_repository(scope, deadline=deadline)
    if manifest is None:
        return None
    return _published_corpus(catalog, manifest, vault, deadline)


def _published_corpus(catalog, manifest, vault, deadline):
    seal = generation_consumption_seal(
        catalog, manifest, _ANSWER_CORPUS_ARTIFACTS, deadline=deadline
    )
    if seal is None:
        raise ValueError("published corpus could not be sealed")
    directory = catalog.generations_path / manifest["generation_id"]
    snapshot = read_published_corpus(catalog, directory, manifest, vault, deadline)
    if not generation_consumption_unchanged(
        catalog, manifest, _ANSWER_CORPUS_ARTIFACTS, seal, deadline=deadline
    ):
        from corpus_snapshot import CorpusChanged

        raise CorpusChanged("published corpus changed during read")
    return snapshot


def read_published_corpus(catalog, directory, manifest, vault, deadline):
    header = validated_source_manifest(
        directory, manifest, state_root=runtime_roots.state_root(), deadline=deadline, cancelled=None
    )
    verified = generation_authoritative_sources(
        directory, manifest, state_root=runtime_roots.state_root(), deadline=deadline, cancelled=None
    )
    sources = published_sources(directory, verified, deadline)
    chunks = _published_chunks(catalog, manifest, sources, deadline)
    return CorpusSnapshot(
        tuple(sources.values()), chunks, manifest["source_manifest_sha256"],
        published_policy(header["policy"], vault), header["collector"], header["extractor"],
    )


def published_policy(fields, vault):
    import corpus_snapshot as corpus

    return corpus.policy(
        **fields, approved_code_roots=tuple(fields["code_roots"]),
        max_files=setting_value("corpus.max_files", vault),
        max_file_bytes=corpus.MAX_CORPUS_FILE_BYTES,
        max_total_bytes=setting_value("corpus.max_total_bytes", vault),
        max_entries=corpus.MAX_CORPUS_INSPECTED_ENTRIES,
        max_directories=corpus.MAX_CORPUS_DIRECTORIES, max_depth=corpus.MAX_CORPUS_DEPTH,
    )


def published_sources(directory, verified, deadline):
    """Retain recorded source fields; derive claim authority from verified Markdown."""
    from dataclasses import replace

    from corpus_snapshot import SourceRecord, canonical_captured_source

    evidence = directory / "evidence.sqlite3"
    validate_runtime_file(evidence, runtime_roots.state_root(), max_bytes=16 * 1024 * 1024 * 1024)
    uri = f"{evidence.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    sources = {}
    with (
        closing(sqlite3.connect(uri, uri=True, timeout=0)) as database,
        generation_sqlite_guard(database, deadline, None),
    ):
        rows = database.execute(
            "SELECT source_id,relative_path,sha256,size,media_type,language,git_oid "
            "FROM source ORDER BY relative_path,source_id"
        )
        for row in rows:
            check_generation_stop(deadline, None)
            record = SourceRecord(*row)
            captured = canonical_captured_source(
                source_id=record.logical_id, source_path=record.relative_path,
                source_sha256=record.sha256, content=verified[record.logical_id]["content"],
                deadline=deadline, code_roots=getattr(verified, "code_roots", ()),
            )
            sources[record.logical_id] = replace(captured, record=record)
    if set(sources) != set(verified):
        raise ValueError("published source membership changed during read")
    return sources


def _published_chunks(catalog, manifest, sources, deadline):
    from corpus_snapshot import newline_offsets

    connection = generation_connection(catalog, manifest, deadline=deadline)
    if connection is None:
        raise ValueError("published corpus chunks could not be verified")
    chunks = []
    line_indexes = {}
    with closing(connection), generation_sqlite_guard(connection, deadline, None):
        for row in connection.execute(FTS_CHUNK_SELECT):
            check_generation_stop(deadline, None)
            source = sources[row[2]]
            offsets = _cached_line_index(line_indexes, row[2], source, newline_offsets)
            chunks.append(published_chunk(row, source, manifest, offsets))
    return tuple(chunks)


def _cached_line_index(line_indexes, source_id, source, newline_offsets):
    if source_id not in line_indexes:
        line_indexes[source_id] = newline_offsets(source.content)
    return line_indexes[source_id]


def _require_published_span(row, source, manifest, offsets):
    from corpus_snapshot import canonical_chunk_id, line_at

    start, end = row[7:9]
    if not 0 <= start <= end <= len(source.content):
        raise ValueError("published chunk span is outside its source")
    span = source.content[start:end]
    expected_id = canonical_chunk_id(
        source_id=source.record.logical_id, source_path=source.record.relative_path,
        byte_start=start, byte_end=end, span_sha256=hashlib.sha256(span).hexdigest(),
        extractor_version=manifest["extractor_version"],
    )
    expected = (
        expected_id, source.record.relative_path, source.record.sha256,
        source.record.relative_path, line_at(offsets, start), line_at(offsets, end),
        hashlib.sha256(span).hexdigest(), span.decode("utf-8"),
    )
    if (row[0], row[3], row[4], row[5], row[9], row[10], row[11], row[21]) != expected:
        raise ValueError("published chunk differs from its verified source bytes")


def published_chunk(row, source, manifest, offsets=None):
    from corpus_snapshot import RetrievalChunk, newline_offsets

    if offsets is None:
        offsets = newline_offsets(source.content)
    _require_published_span(row, source, manifest, offsets)
    metadata = source.metadata
    return RetrievalChunk(
        id=row[0], source_id=row[2], source_path=row[3], source_sha256=row[4],
        parent_page=row[5], heading_ancestry=tuple(json.loads(row[6])),
        byte_start=row[7], byte_end=row[8], line_start=row[9], line_end=row[10],
        span_sha256=row[11], text=row[21], type=metadata.type, project=metadata.project,
        authority=metadata.authority, confidence=metadata.confidence, status=metadata.status,
        valid_from=metadata.valid_from, valid_to=metadata.valid_to, language=row[19],
    )

def search(
    query: str,
    scope: str = "all",
    limit: int = 10,
    project: str | None = None,
    since: str | None = None,
    as_of: str | None = None,
    semantic: bool = True,
    page_paths: list[Path] | None = None,
    graph: bool = True,
    rerank: bool = True,
    source_tool: str = "search_memory",
    emit_telemetry: bool = True,
    *,
    profile: str | None = None,
    catalog: GenerationCatalog | None = None,
    generation_embedder: object | None = None,
    generation_model_id: str | None = None,
    generation_model_revision: str | None = None,
    deadline_monotonic: float | None = None,
    max_candidates: int | None = None,
    cancelled: Callable[[], bool] | None = None,
    trace_sink: dict[str, object] | None = None,
) -> list[dict]:
    """Public search API — always routes through retrieval.retrieve().

    `trace_sink`, when given, receives the planner trace of the run — which
    generation was selected, which signals ran, what fell back — even when
    the run returns no rows (#26.1).
    """
    limit = _validate_search_limit(limit)
    if blank_query(query):
        return []
    from retrieval import foreground_retrieval, retrieve_via_search_memory

    with foreground_retrieval(source_tool):

        generation_embedder, generation_model_id, generation_model_revision = (
            resolved_generation_embedder(
                semantic, generation_embedder, generation_model_id, generation_model_revision
            )
        )

        return retrieve_via_search_memory(
            query,
            scope=scope,
            limit=limit,
            project=project,
            since=since,
            as_of=as_of,
            semantic=semantic,
            page_paths=page_paths,
            graph=graph,
            rerank=rerank,
            source_tool=source_tool,
            emit_telemetry=emit_telemetry,
            profile=profile,
            catalog=catalog,
            generation_embedder=generation_embedder,
            generation_model_id=generation_model_id,
            generation_model_revision=generation_model_revision,
            deadline_monotonic=deadline_monotonic,
            max_candidates=max_candidates,
            cancelled=cancelled,
            trace_sink=trace_sink,
        )


_PAGE_STATUS_RE = re.compile(r"^status:\s*[\"\']?([^\"\'\n]+)[\"\']?\s*$", re.MULTILINE)


def main() -> int:
    p = argparse.ArgumentParser(description="Hybrid search over the vault's knowledge.")
    p.add_argument("query", nargs="?", default=None, help="Search query")
    p.add_argument("--scope", choices=["all", "wiki", "memory", "knowledge"], default="all")
    p.add_argument("--limit", type=_cli_search_limit, default=10)
    p.add_argument("--project", default=None, help="Boost results from this project slug")
    p.add_argument("--since", default=None, help="Only results since YYYY-MM-DD")
    p.add_argument("--as-of", dest="as_of", default=None, help="Only results valid on YYYY-MM-DD")
    # On by default: measured on this vault, "почему systemd таймер, а не cron"
    # returns nothing without it and four results with it. The dense leg costs
    # about three seconds once the model is warm, and it fails soft — no model or
    # no vectors in the active generation simply means the lexical answer.
    p.add_argument(
        "--semantic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Vector search over the active generation (default: on)",
    )
    p.add_argument(
        "--profile",
        choices=[
            "DIRECT",
            "EXACT",
            "BASE",
            "HYBRID",
            "GRAPH",
            "TEMPORAL",
            "REPO_MAP",
            "IMPACT",
            "GLOBAL",
            "CACHED_FULL",
        ],
        default=None,
        help="Requested retrieval profile (Task 11 planner)",
    )
    p.add_argument("--no-graph", action="store_true", help="Disable graph-neighbor signal")
    # The cross-encoder costs about twenty seconds to load in a fresh process
    # and buys `hit@1` 0.0 → 0.1 and `hit@5` 0.6 → 0.7 on this vault's stand
    # (measured 2026-08-24). A resident MCP server pays that once and keeps it;
    # a one-shot CLI call pays it every time, which is why the CLI leaves it off
    # unless asked. Measured 2026-08-25: 30.3 s with it, 10.7 s without.
    p.add_argument(
        "--rerank",
        action="store_true",
        help="Load the cross-encoder reranker (about 20s in a cold process)",
    )
    p.add_argument(
        "--no-rerank",
        action="store_true",
        help="Kept for compatibility: the CLI already skips the reranker",
    )
    p.add_argument(
        "--rebuild", action="store_true", help="Rebuild the evidence generation the search reads"
    )
    p.add_argument("--status", action="store_true", help="Show what the search reads")
    p.add_argument("--stdin", action="store_true", help="Read query from stdin (injection-safe)")
    args = p.parse_args()

    if args.stdin:
        args.query = sys.stdin.read().strip()
    return _run_cli_command(args)


def _run_cli_command(args: argparse.Namespace) -> int:
    """The one mode the arguments asked for, in the order they take priority."""
    modes = (
        (args.status, print_status),
        (args.rebuild, _rebuild_generation_cli),
        (not args.query, _print_cli_usage),
    )
    for chosen, run in modes:
        if chosen:
            return run()
    return _run_cli_search(args)


def _print_cli_usage() -> int:
    print("Usage: python search_memory.py \"<query>\"", file=sys.stderr)
    return 1


def _active_generation_line() -> str:
    """What the search reads: the active generation, else Markdown directly."""
    catalog = active_generation_catalog()
    manifest = None if catalog is None else catalog.get_active()
    if not isinstance(manifest, dict):
        return "Active generation: none (search reads Markdown directly; run --rebuild)"
    return (
        f"Active generation: {manifest.get('generation_id')} "
        f"({manifest.get('extractor_version')}, vectors {manifest.get('vector_state')}, "
        f"model {manifest.get('embedding_model_id')})"
    )


def print_status() -> int:
    """The generation the search reads and the pages it would index."""
    print(_active_generation_line())
    print(f"Searchable pages (superseded excluded): {len(collect_pages('all'))}")
    return 0


def _rebuild_generation_cli() -> int:
    """Rebuild the evidence generation under the maintenance fence: the one index."""
    import generation_maintenance

    result = generation_maintenance.run_generation_maintenance(runtime_roots.vault_root(), runtime_roots.state_root(), force_rebuild=True)
    status = str(result.get("status"))
    print(f"Generation rebuild: {status} (id={result.get('generation_id') or 'none'})")
    if result.get("reason"):
        print(f"  reason: {result['reason']}")
    return 0 if status in {"built", "current"} else 1


def _run_cli_search(args: argparse.Namespace) -> int:
    t0 = time.time()
    results = search(
        args.query, args.scope, args.limit,
        project=args.project,
        since=args.since,
        as_of=args.as_of,
        semantic=args.semantic,
        profile=args.profile,
        graph=not args.no_graph,
        rerank=args.rerank and not args.no_rerank,
    )
    elapsed = time.time() - t0

    if not results:
        print(f"No results for '{args.query}' ({elapsed:.3f}s)")
        return 0

    _print_search_results(args.query, results, elapsed)
    return 0


def _print_search_results(query: str, results: list[dict], elapsed: float) -> None:
    print(f"Found {len(results)} result(s) for '{query}' ({elapsed:.3f}s):\n")
    for i, r in enumerate(results, 1):
        proj_tag = f" [{r['project']}]" if r["project"] else ""
        ts_tag = f" ({r['timestamp']})" if r["timestamp"] else ""
        print(f"{i}. [{r['score']}] {r['title']}{proj_tag}{ts_tag}")
        print(f"   {r['path']}")
        snippet = result_snippet(r)
        if snippet:
            print(f"   {snippet}")
        print()


# A printed search snippet, cut with an ellipsis so the cut shows. A readability trade-off, not
# measured.
SNIPPET_CHARS = 240


def result_snippet(result: dict) -> str:
    """The first words of the hit's text after its heading; the summary otherwise.

    A chunk's summary is its heading, and a heading such as "Consequences" says
    nothing on its own line. See
    `docs/research/2026-09-24-an-answer-says-how-old-its-index-is.md`.
    """
    text = _body_text(str(result.get("content") or ""))
    if not text:
        return str(result.get("summary") or "")
    return _clipped(text, SNIPPET_CHARS)


def _body_text(content: str) -> str:
    lines = [line.strip() for line in content.splitlines()]
    return " ".join(line for line in lines if _is_body_line(line))


def _is_body_line(line: str) -> bool:
    return bool(line) and not line.startswith("#")


def _clipped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


if __name__ == "__main__":
    raise SystemExit(main())
