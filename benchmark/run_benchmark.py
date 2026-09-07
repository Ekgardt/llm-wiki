"""Benchmark entry point for LLM-wiki retrieval evaluation.

The frozen public retrieval-v2 corpus is the default. The generated title and
summary benchmark remains available only as the explicit ``--legacy-only``
regression slice. Real semantic and report runs belong to Task 10 and fail
closed here until that implementation exists.

The legacy-only slice measures Recall@K, MRR, and search latency for queries
derived from public page titles and summaries.

Legacy-only methodology:
- Generates synthetic Q&A pairs from existing knowledge pages
- Each page's title + summary → exact title query and summary-derived keyword query
- Runs search_memory.py with BM25-only and optional BM25+Vector
- Measures standard IR metrics (Recall@K, MRR)

That slice is a known-item retrieval regression check, not the primary quality
claim.

Usage:
    uv run python benchmark/run_benchmark.py                 # retrieval-v2 fake orchestration
    uv run python benchmark/run_benchmark.py --legacy-only   # legacy BM25 regression slice
    uv run python benchmark/run_benchmark.py --semantic      # Task 10: currently fails closed
    uv run python benchmark/run_benchmark.py --report        # Task 10: currently fails closed
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

# Benchmarks describe this source checkout, not a separately installed vault
# that may be selected by LLM_WIKI_ROOT in the developer's shell.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

INDEX_DIR = ROOT / "benchmark"
KNOWLEDGE = ROOT / "knowledge" / "notes"
LEGACY_CORPUS = INDEX_DIR / "legacy-60-v1.json"
CURRENT_RECALL_5_FLOOR = 0.95
LEGACY_RECALL_5_FLOOR = 1.0
DESCRIPTION = "Run LLM-wiki benchmark suite."
MODEL_CACHE_ENV = (
    "HF_HOME",
    "HF_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "SENTENCE_TRANSFORMERS_HOME",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
# Flat notes (current layout) + optional typed subdirs (legacy/aspirational).
_SEARCH_SUBDIRS = ("decisions", "patterns", "debugging", "concepts", "qa")
_SKIPPED_NAMES = frozenset({"readme.md", "index.md", "log.md"})
_STOP_WORDS = frozenset(
    {
        "that", "this", "with", "from", "have", "they", "will",
        "been", "were", "more", "than", "when", "what", "which",
        "should", "would", "could", "their", "there", "where",
        "page", "file", "using", "used", "into",
    }
)


def _run_retrieval_v2(args: list[str]) -> int:
    """Dispatch the default benchmark to retrieval-v2."""
    from run_retrieval_v2 import main as retrieval_v2_main

    return retrieval_v2_main(args)


# --- synthetic queries ---------------------------------------------------------


def _pages_in(directory: Path) -> list[Path]:
    """Direct children only; a subdirectory uses its own glob."""
    if not directory.exists():
        return []
    return [page for page in sorted(directory.glob("*.md")) if page.name.lower() not in _SKIPPED_NAMES]


def _candidate_pages() -> list[Path]:
    seen: dict[Path, None] = {}
    for directory in (KNOWLEDGE, *(KNOWLEDGE / name for name in _SEARCH_SUBDIRS)):
        seen.update(dict.fromkeys(_pages_in(directory)))
    return list(seen)


def _page_content(page: Path) -> str | None:
    try:
        return page.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _summary_of(content: str) -> str:
    match = SUMMARY_RE.search(content)
    if not match:
        return ""
    return match.group(1).strip()


def _pair(query: str, gold_path: str, query_type: str) -> dict:
    return {"query": query, "gold_path": gold_path, "query_type": query_type}


def _summary_keywords(summary: str) -> list[str]:
    words = re.findall(r"\b[a-zA-Z]{4,}\b", summary.lower())
    return [word for word in words if word not in _STOP_WORDS][:4]


def _summary_pair(summary: str, gold_path: str) -> dict | None:
    """Key words from the summary (medium)."""
    if not summary:
        return None
    keywords = _summary_keywords(summary)
    if len(keywords) < 2:
        return None
    return _pair(" ".join(keywords), gold_path, "keywords_from_summary")


def _partial_title_pair(title: str, gold_path: str) -> dict | None:
    """The first two words of the title — tests truncation robustness."""
    words = title.split()
    if len(words) < 3:
        return None
    return _pair(" ".join(words[:2]).lower(), gold_path, "partial_title")


def _slug_pair(page: Path, title: str, gold_path: str) -> dict | None:
    """The filename as a search — tests the filename boost."""
    slug = page.stem.lower().replace("-", " ")
    if slug == title.lower() or len(slug) <= 5:
        return None
    return _pair(slug, gold_path, "slug_match")


def _pairs_for_page(page: Path) -> list[dict]:
    content = _page_content(page)
    if content is None:
        return []
    title_match = H1_RE.search(content)
    if not title_match:
        return []
    title = title_match.group(1).strip()
    gold_path = page.relative_to(ROOT).as_posix()
    candidates = (
        _pair(title.lower(), gold_path, "exact_title"),
        _summary_pair(_summary_of(content), gold_path),
        _partial_title_pair(title, gold_path),
        _slug_pair(page, title, gold_path),
    )
    return [item for item in candidates if item is not None]


def _generate_qa_pairs() -> list[dict]:
    """Generate synthetic Q&A pairs from existing knowledge pages.

    For each page: title → query, page path → gold answer.
    Also generates a keyword query using key words from the summary.
    """
    pairs: list[dict] = []
    for page in _candidate_pages():
        pairs.extend(_pairs_for_page(page))
    return pairs


def _tracked_knowledge_paths() -> list[Path]:
    """Return the public corpus exactly as a clean git checkout sees it."""
    result = subprocess.run(
        ["git", "ls-files", "knowledge/notes/*.md", "knowledge/notes/**/*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files failed; reproducible benchmark corpus unavailable")
    return [ROOT / line for line in result.stdout.splitlines() if line]


def _current_corpus_queries() -> list[dict]:
    tracked = {path.relative_to(ROOT).as_posix() for path in _tracked_knowledge_paths()}
    return [pair for pair in _generate_qa_pairs() if pair["gold_path"] in tracked]


def _load_legacy_corpus() -> dict:
    """Load the frozen query text and gold paths without consulting page content."""
    manifest = json.loads(LEGACY_CORPUS.read_text(encoding="utf-8"))
    queries = manifest["queries"]
    expected = int(manifest["expected_queries"])
    if len(queries) != expected:
        raise ValueError(
            f"{manifest['version']} expected {expected} queries, generated {len(queries)}"
        )
    return manifest


# --- an isolated runtime --------------------------------------------------------


_MISSING = object()


def _runtime_environment(runtime_root: Path, cache: Path) -> dict[str, str]:
    model_cache = cache / "models"
    return {
        "LLM_WIKI_ROOT": str(ROOT),
        "LLM_WIKI_STATE_ROOT": str(runtime_root),
        "HF_HOME": str(model_cache / "huggingface"),
        "HF_HUB_CACHE": str(model_cache / "huggingface" / "hub"),
        "TRANSFORMERS_CACHE": str(model_cache / "transformers"),
        "SENTENCE_TRANSFORMERS_HOME": str(model_cache / "sentence-transformers"),
        "TORCH_HOME": str(model_cache / "torch"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
    }


def _search_overrides(runtime_root: Path, search_cache: Path) -> dict[str, object]:
    return {
        "ROOT": ROOT,
        "STATE_ROOT": runtime_root,
        "WIKI_DIR": KNOWLEDGE,
        "KNOWLEDGE_DIR": KNOWLEDGE,
        "INDEX_DIR": search_cache,
        "INDEX_FILE": search_cache / "index.sqlite",
        "INDEX_MANIFEST": search_cache / "manifest.json",
        "VECTOR_NPY": search_cache / "vectors.npy",
        "VECTOR_META": search_cache / "vectors_meta.json",
        "_embedder_cache": None,
    }


def _swap_environment(values: Mapping[str, str]) -> dict[str, object]:
    """Set the variables and return what they were, `_MISSING` where unset."""
    previous = {key: os.environ.get(key, _MISSING) for key in values}
    os.environ.update(values)
    return previous


def _restore_environment(previous: Mapping[str, object]) -> None:
    for key, value in previous.items():
        if value is _MISSING:
            os.environ.pop(key, None)
            continue
        os.environ[key] = str(value)


def _swap_attributes(module: object, values: Mapping[str, object]) -> dict[str, object]:
    previous = {key: getattr(module, key) for key in values}
    for key, value in values.items():
        setattr(module, key, value)
    return previous


def _discard(temporary: tempfile.TemporaryDirectory | None) -> None:
    if temporary is None:
        return
    gc.collect()
    temporary.cleanup()


@contextlib.contextmanager
def _isolated_search_runtime(runtime_root: Path | None = None):
    """Point every search write and model cache at a disposable directory."""
    temporary = None
    if runtime_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="llm-wiki-benchmark-")
        runtime_root = Path(temporary.name)
    runtime_root = runtime_root.resolve()
    cache = runtime_root / "cache"
    previous_env = _swap_environment(_runtime_environment(runtime_root, cache))

    import search_memory

    previous_search = _swap_attributes(
        search_memory, _search_overrides(runtime_root, cache / "search")
    )
    try:
        yield search_memory
    finally:
        _swap_attributes(search_memory, previous_search)
        _restore_environment(previous_env)
        _discard(temporary)


def _passes_regression_gates(current: dict, legacy: dict) -> bool:
    return (
        current["recall_at_k"].get(5, 0) >= CURRENT_RECALL_5_FLOOR
        and legacy["recall_at_k"].get(5, 0) >= LEGACY_RECALL_5_FLOOR
    )


def _run_benchmark(
    qa_pairs: list[dict],
    semantic: bool = False,
    k_values: list[int] | None = None,
    corpus_version: str = "current-generated-v2",
    page_paths: list[Path] | None = None,
) -> dict:
    """Run search against all Q&A pairs and measure metrics."""
    if k_values is None:
        k_values = [1, 3, 5, 10]
    with _isolated_search_runtime() as search_memory:
        return _execute_benchmark(
            search_memory.search, qa_pairs, semantic, k_values, corpus_version, page_paths
        )


# --- measuring one corpus -------------------------------------------------------


def _empty_results(qa_pairs: list[dict], corpus_version: str, semantic: bool, k_values: list[int]) -> dict:
    return {
        "total_queries": len(qa_pairs),
        "corpus_version": corpus_version,
        "semantic": semantic,
        "k_values": k_values,
        "recall_at_k": {k: 0 for k in k_values},
        "mrr_sum": 0.0,
        "latencies_ms": [],
        "per_query": [],
    }


def _measure_one(search, qa: dict, semantic: bool, k_values: list[int], page_paths) -> tuple[list, float]:
    started = time.perf_counter()
    rows = search(
        qa["query"],
        scope="all",
        limit=max(k_values),
        semantic=semantic,
        page_paths=page_paths,
        graph=False,
        rerank=False,
    )
    return rows, (time.perf_counter() - started) * 1000


def _found_rank(rows: Iterable[Mapping[str, object]], gold: str) -> int | None:
    for rank, row in enumerate(rows, 1):
        if row["path"] == gold:
            return rank
    return None


def _tally(results: dict, found: int | None, k_values: list[int]) -> None:
    if not found:
        return
    for k in k_values:
        if found <= k:
            results["recall_at_k"][k] += 1
    results["mrr_sum"] += 1.0 / found


def _query_record(qa: dict, found: int | None, elapsed_ms: float, count: int) -> dict:
    return {
        "query": qa["query"][:80],
        "query_type": qa["query_type"],
        "gold": qa["gold_path"],
        "found_at": found,
        "latency_ms": round(elapsed_ms, 1),
        "num_results": count,
    }


def _percentile_95(latencies: list[float], p50: float) -> float:
    n = len(latencies)
    if n <= 1:
        return p50
    return round(sorted(latencies)[max(0, math.ceil(0.95 * n) - 1)], 1)


def _finalized(results: dict, n: int) -> dict:
    results["recall_at_k"] = {k: round(count / n, 4) for k, count in results["recall_at_k"].items()}
    results["mrr"] = round(results["mrr_sum"] / n, 4) if n > 0 else 0
    latencies = results["latencies_ms"]
    results["latency_p50_ms"] = round(statistics.median(latencies), 1)
    results["latency_p95_ms"] = _percentile_95(latencies, results["latency_p50_ms"])
    results["latency_avg_ms"] = round(statistics.mean(latencies), 1) if latencies else 0
    return results


def _execute_benchmark(
    search,
    qa_pairs: list[dict],
    semantic: bool,
    k_values: list[int],
    corpus_version: str,
    page_paths: list[Path] | None,
) -> dict:
    """Measure one corpus using an already-isolated search function."""
    results = _empty_results(qa_pairs, corpus_version, semantic, k_values)
    for qa in qa_pairs:
        rows, elapsed_ms = _measure_one(search, qa, semantic, k_values, page_paths)
        found = _found_rank(rows, qa["gold_path"])
        results["latencies_ms"].append(elapsed_ms)
        _tally(results, found, k_values)
        results["per_query"].append(_query_record(qa, found, elapsed_ms, len(rows)))
    return _finalized(results, len(qa_pairs))


# --- the report -----------------------------------------------------------------


def _mode_label(results: dict) -> str:
    return "BM25 + Vector (hybrid RRF)" if results["semantic"] else "BM25 only"


def _header_lines(results: dict) -> list[str]:
    lines = [
        "# LLM-Wiki Benchmark Report",
        "",
        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Mode: {_mode_label(results)}",
        f"Queries: {results['total_queries']}",
        f"Corpus: {results.get('corpus_version', 'unspecified')}",
        "",
        "## Results",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    lines.extend(f"| Recall@{k} | **{results['recall_at_k'][k]:.1%}** |" for k in results["k_values"])
    lines.extend(
        [
            f"| MRR | **{results['mrr']:.4f}** |",
            f"| Latency p50 | **{results['latency_p50_ms']}ms** |",
            f"| Latency p95 | **{results['latency_p95_ms']}ms** |",
            f"| Latency avg | **{results['latency_avg_ms']}ms** |",
        ]
    )
    return lines


def _context_lines(results: dict) -> list[str]:
    mode = "hybrid" if results["semantic"] else "BM25"
    return [
        "",
        "## Context only: published results on different corpora",
        "",
        "| System | Recall@5 | MRR | Latency p50 |",
        "|---|---|---|---|",
        f"| **LLM-Wiki ({mode})** | **{results['recall_at_k'].get(5, 0):.1%}** | **{results['mrr']:.4f}** | **{results['latency_p50_ms']}ms** |",
        "| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |",
        "| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |",
        "| Zep | 94.7% (LoCoMo) | n/a | 155ms |",
        "| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |",
        "",
        "These rows are not head-to-head comparisons: datasets and tasks differ.",
    ]


def _by_type(per_query: Iterable[dict]) -> dict[str, dict]:
    by_type: dict[str, dict] = {}
    for query in per_query:
        data = by_type.setdefault(query["query_type"], {"total": 0, "found": 0, "ranks": []})
        data["total"] += 1
        if query["found_at"]:
            data["found"] += 1
            data["ranks"].append(query["found_at"])
    return by_type


def _type_row(query_type: str, data: dict) -> str:
    recall_5 = data["found"] / data["total"] if data["total"] else 0
    avg_rank = statistics.mean(data["ranks"]) if data["ranks"] else 0
    return f"| {query_type} | {data['total']} | {recall_5:.1%} | {avg_rank:.1f} |"


def _breakdown_lines(results: dict) -> list[str]:
    lines = [
        "",
        "## Breakdown by query type",
        "",
        "| Query type | Count | Recall@5 | Avg rank when found |",
        "|---|---|---|---|",
    ]
    lines.extend(_type_row(kind, data) for kind, data in sorted(_by_type(results["per_query"]).items()))
    return lines


def _legacy_lines(legacy_results: dict | None) -> list[str]:
    if legacy_results is None:
        return []
    return [
        "",
        "## Legacy 60-query gate",
        "",
        f"Corpus: `{legacy_results['corpus_version']}` (60 frozen query/gold-path pairs).",
        f"Recall@5: **{legacy_results['recall_at_k'][5]:.1%}**; MRR: **{legacy_results['mrr']:.4f}**.",
        f"Gate: Recall@5 >= {LEGACY_RECALL_5_FLOOR:.0%}.",
    ]


def _missed_row(query: dict) -> str:
    rank = f"rank {query['found_at']}" if query["found_at"] else "not found"
    return f"- `{query['query'][:60]}` -> gold: {query['gold']} ({rank})"


def _is_missed(query: dict) -> bool:
    return not query["found_at"] or query["found_at"] > 5


def _missed_lines(results: dict) -> list[str]:
    missed = [query for query in results["per_query"] if _is_missed(query)]
    lines = ["", "## Missed at Recall@5", ""]
    lines.extend(_missed_row(query) for query in missed[:10])
    if len(missed) > 10:
        lines.append(f"- ... and {len(missed) - 10} more")
    return lines


def _format_report(results: dict, legacy_results: dict | None = None) -> str:
    """Format benchmark results as a readable report."""
    return "\n".join(
        [
            *_header_lines(results),
            *_context_lines(results),
            *_breakdown_lines(results),
            *_legacy_lines(legacy_results),
            *_missed_lines(results),
        ]
    )


# --- the command ----------------------------------------------------------------


def _refuse_conflicting_flags(arguments: list[str]) -> None:
    if "--legacy-only" not in arguments:
        return
    if not ({"--semantic", "--report"} & set(arguments)):
        return
    conflict = argparse.ArgumentParser(description=DESCRIPTION)
    conflict.error("--legacy-only cannot be combined with --semantic or --report")


def _dispatched(arguments: list[str]) -> int | None:
    """The retrieval-v2 exit code when that is what was asked, else None."""
    _refuse_conflicting_flags(arguments)
    if "--retrieval-v2" in arguments or "--legacy-only" not in arguments:
        return _run_retrieval_v2([arg for arg in arguments if arg != "--retrieval-v2"])
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--semantic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--legacy-only",
        action="store_true",
        help="Run only the fixed, fast legacy-60 BM25 regression gate",
    )
    return parser


def _legacy_results(legacy_corpus: dict, semantic: bool, tracked_pages: list[Path]) -> dict:
    return _run_benchmark(
        legacy_corpus["queries"],
        semantic=semantic,
        corpus_version=legacy_corpus["version"],
        page_paths=tracked_pages,
    )


def _print_legacy(as_json: bool, legacy_results: dict) -> None:
    if as_json:
        print(json.dumps({"legacy": legacy_results}, indent=2, default=str))
        return
    print(
        f"Legacy Recall@5: {legacy_results['recall_at_k'].get(5, 0):.1%} "
        f"(gate {LEGACY_RECALL_5_FLOOR:.0%})"
    )


def _legacy_only_run(args: argparse.Namespace, legacy_corpus: dict, tracked_pages: list[Path]) -> int:
    legacy_results = _legacy_results(legacy_corpus, False, tracked_pages)
    _print_legacy(args.json, legacy_results)
    passed = legacy_results["recall_at_k"].get(5, 0) >= LEGACY_RECALL_5_FLOOR
    return 0 if passed else 2


def _gate_code(results: dict, legacy_results: dict, *, loud: bool) -> int:
    if _passes_regression_gates(results, legacy_results):
        return 0
    if loud:
        print("Benchmark regression gate failed.", file=sys.stderr)
    return 2


def _print_report(save: bool, results: dict, legacy_results: dict) -> None:
    report = _format_report(results, legacy_results)
    print(report)
    if not save:
        return
    report_path = INDEX_DIR / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")


def _run_label(semantic: bool) -> str:
    return "hybrid BM25+Vector" if semantic else "BM25 only"


def _full_run(args: argparse.Namespace, legacy_corpus: dict, tracked_pages: list[Path]) -> int:
    qa_pairs = _current_corpus_queries()
    print(f"  Generated {len(qa_pairs)} queries")
    if not qa_pairs:
        print("No knowledge pages found to benchmark against.")
        return 1
    print(f"Running benchmark ({_run_label(args.semantic)})...")
    results = _run_benchmark(qa_pairs, semantic=args.semantic, page_paths=tracked_pages)
    legacy_results = _legacy_results(legacy_corpus, args.semantic, tracked_pages)
    if args.json:
        print(json.dumps({"current": results, "legacy": legacy_results}, indent=2, default=str))
        return _gate_code(results, legacy_results, loud=False)
    _print_report(args.report, results, legacy_results)
    return _gate_code(results, legacy_results, loud=True)


def main() -> int:
    arguments = sys.argv[1:]
    dispatched = _dispatched(arguments)
    if dispatched is not None:
        return dispatched
    args = _parser().parse_args()
    print("Generating Q&A pairs from knowledge pages...")
    legacy_corpus = _load_legacy_corpus()
    tracked_pages = _tracked_knowledge_paths()
    if args.legacy_only:
        return _legacy_only_run(args, legacy_corpus, tracked_pages)
    return _full_run(args, legacy_corpus, tracked_pages)


if __name__ == "__main__":
    raise SystemExit(main())
