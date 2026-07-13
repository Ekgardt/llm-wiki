"""Benchmark suite for LLM-wiki memory system.

Measures objective metrics that can be compared to competitors
(agentmemory, Mem0, Zep, ReMe):

1. Recall@K — can search find the right page when given a query derived from the page's title and summary?
2. MRR (Mean Reciprocal Rank) — how high is the correct result ranked?
3. Search latency — p50/p95 response time
4. Token efficiency — tokens consumed per operation
5. Context injection quality — is SessionStart context informative?

Methodology:
- Generates synthetic Q&A pairs from existing knowledge pages
- Each page's title + summary → exact title query and summary-derived keyword query
- Runs search_memory.py with BM25-only and optional BM25+Vector
- Measures standard IR metrics (Recall@K, MRR)

This is a "known-item retrieval" benchmark — the simplest and most
relevant test for a personal memory vault.

Usage:
    uv run python benchmark/run_benchmark.py                # full suite
    uv run python benchmark/run_benchmark.py --semantic      # with vector search
    uv run python benchmark/run_benchmark.py --report        # write report
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


def _generate_qa_pairs() -> list[dict]:
    """Generate synthetic Q&A pairs from existing knowledge pages.

    For each page: title → query, page path → gold answer.
    Also generates a keyword query using key words from the summary.
    """
    pairs = []
    # Flat notes (current layout) + optional typed subdirs (legacy/aspirational).
    search_dirs = [
        KNOWLEDGE,  # flat knowledge/notes/*.md
        KNOWLEDGE / "decisions",
        KNOWLEDGE / "patterns",
        KNOWLEDGE / "debugging",
        KNOWLEDGE / "concepts",
        KNOWLEDGE / "qa",
    ]
    seen: set[Path] = set()

    for d in search_dirs:
        if not d.exists():
            continue
        # Only direct children for flat root; subdirs use their own glob.
        for md in sorted(d.glob("*.md")):
            if md in seen:
                continue
            if md.name.lower() in {"readme.md", "index.md", "log.md"}:
                continue
            seen.add(md)
            try:
                content = md.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            title_match = H1_RE.search(content)
            summary_match = SUMMARY_RE.search(content)
            if not title_match:
                continue

            title = title_match.group(1).strip()
            summary = summary_match.group(1).strip() if summary_match else ""
            rel_path = md.relative_to(ROOT).as_posix()

            # Query 1: exact title (easy)
            pairs.append({
                "query": title.lower(),
                "gold_path": rel_path,
                "query_type": "exact_title",
            })

            # Query 2: key words from summary (medium)
            if summary:
                # Extract 3-5 key words from summary
                words = re.findall(r"\b[a-zA-Z]{4,}\b", summary.lower())
                # Remove common words
                stop = {"that", "this", "with", "from", "have", "they", "will",
                        "been", "were", "more", "than", "when", "what", "which",
                        "should", "would", "could", "their", "there", "where",
                        "page", "file", "using", "used", "into"}
                keywords = [w for w in words if w not in stop][:4]
                if len(keywords) >= 2:
                    pairs.append({
                        "query": " ".join(keywords),
                        "gold_path": rel_path,
                        "query_type": "keywords_from_summary",
                    })

            # Query 3: partial title (first 2-3 words) — tests truncation robustness
            title_words = title.split()
            if len(title_words) >= 3:
                pairs.append({
                    "query": " ".join(title_words[:2]).lower(),
                    "gold_path": rel_path,
                    "query_type": "partial_title",
                })

            # Query 4: slug-derived (filename as search) — tests filename boost
            slug = md.stem.lower().replace("-", " ")
            if slug != title.lower() and len(slug) > 5:
                pairs.append({
                    "query": slug,
                    "gold_path": rel_path,
                    "query_type": "slug_match",
                })

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


@contextlib.contextmanager
def _isolated_search_runtime(runtime_root: Path | None = None):
    """Point every search write and model cache at a disposable directory."""
    temporary = None
    if runtime_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="llm-wiki-benchmark-")
        runtime_root = Path(temporary.name)
    runtime_root = runtime_root.resolve()
    cache = runtime_root / "cache"
    search_cache = cache / "search"
    model_cache = cache / "models"
    env_values = {
        "LLM_WIKI_ROOT": str(ROOT),
        "LLM_WIKI_STATE_ROOT": str(runtime_root),
        "HF_HOME": str(model_cache / "huggingface"),
        "HF_HUB_CACHE": str(model_cache / "huggingface" / "hub"),
        "TRANSFORMERS_CACHE": str(model_cache / "transformers"),
        "SENTENCE_TRANSFORMERS_HOME": str(model_cache / "sentence-transformers"),
        "TORCH_HOME": str(model_cache / "torch"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
    }
    missing = object()
    old_env = {key: os.environ.get(key, missing) for key in env_values}
    os.environ.update(env_values)

    import lance_store
    import search_memory

    search_overrides = {
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
    lance_overrides = {
        "STATE_ROOT": runtime_root,
        "LANCEDB_DIR": cache / "lancedb",
        "_lancedb": None,
    }
    old_search = {key: getattr(search_memory, key) for key in search_overrides}
    old_lance = {key: getattr(lance_store, key) for key in lance_overrides}
    for key, value in search_overrides.items():
        setattr(search_memory, key, value)
    for key, value in lance_overrides.items():
        setattr(lance_store, key, value)
    try:
        yield search_memory
    finally:
        for key, value in old_search.items():
            setattr(search_memory, key, value)
        for key, value in old_lance.items():
            setattr(lance_store, key, value)
        for key, value in old_env.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if temporary is not None:
            gc.collect()
            temporary.cleanup()


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


def _execute_benchmark(
    search,
    qa_pairs: list[dict],
    semantic: bool,
    k_values: list[int],
    corpus_version: str,
    page_paths: list[Path] | None,
) -> dict:
    """Measure one corpus using an already-isolated search function."""

    results = {
        "total_queries": len(qa_pairs),
        "corpus_version": corpus_version,
        "semantic": semantic,
        "k_values": k_values,
        "recall_at_k": {k: 0 for k in k_values},
        "mrr_sum": 0.0,
        "latencies_ms": [],
        "per_query": [],
    }

    for i, qa in enumerate(qa_pairs):
        t0 = time.perf_counter()
        search_results = search(
            qa["query"],
            scope="all",
            limit=max(k_values),
            semantic=semantic,
            page_paths=page_paths,
            graph=False,
            rerank=False,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        results["latencies_ms"].append(elapsed_ms)

        # Find gold path in results
        gold = qa["gold_path"]
        found_at_rank = None
        for rank, r in enumerate(search_results, 1):
            if r["path"] == gold:
                found_at_rank = rank
                break

        # Recall@K
        for k in k_values:
            if found_at_rank and found_at_rank <= k:
                results["recall_at_k"][k] += 1

        # MRR
        if found_at_rank:
            results["mrr_sum"] += 1.0 / found_at_rank

        results["per_query"].append({
            "query": qa["query"][:80],
            "query_type": qa["query_type"],
            "gold": gold,
            "found_at": found_at_rank,
            "latency_ms": round(elapsed_ms, 1),
            "num_results": len(search_results),
        })

    # Compute final metrics
    n = len(qa_pairs)
    results["recall_at_k"] = {
        k: round(count / n, 4) for k, count in results["recall_at_k"].items()
    }
    results["mrr"] = round(results["mrr_sum"] / n, 4) if n > 0 else 0
    results["latency_p50_ms"] = round(statistics.median(results["latencies_ms"]), 1)
    sorted_lat = sorted(results["latencies_ms"])
    n = len(sorted_lat)
    results["latency_p95_ms"] = round(
        sorted_lat[max(0, math.ceil(0.95 * n) - 1)], 1,
    ) if n > 1 else results["latency_p50_ms"]
    results["latency_avg_ms"] = round(
        statistics.mean(results["latencies_ms"]), 1
    ) if results["latencies_ms"] else 0

    return results


def _format_report(results: dict, legacy_results: dict | None = None) -> str:
    """Format benchmark results as a readable report."""
    lines = [
        "# LLM-Wiki Benchmark Report",
        "",
        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Mode: {'BM25 + Vector (hybrid RRF)' if results['semantic'] else 'BM25 only'}",
        f"Queries: {results['total_queries']}",
        f"Corpus: {results.get('corpus_version', 'unspecified')}",
        "",
        "## Results",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]

    for k in results["k_values"]:
        lines.append(f"| Recall@{k} | **{results['recall_at_k'][k]:.1%}** |")
    lines.append(f"| MRR | **{results['mrr']:.4f}** |")
    lines.append(f"| Latency p50 | **{results['latency_p50_ms']}ms** |")
    lines.append(f"| Latency p95 | **{results['latency_p95_ms']}ms** |")
    lines.append(f"| Latency avg | **{results['latency_avg_ms']}ms** |")

    lines.append("")
    lines.append("## Context only: published results on different corpora")
    lines.append("")
    lines.append("| System | Recall@5 | MRR | Latency p50 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| **LLM-Wiki ({'hybrid' if results['semantic'] else 'BM25'})** | **{results['recall_at_k'].get(5, 0):.1%}** | **{results['mrr']:.4f}** | **{results['latency_p50_ms']}ms** |")
    lines.append("| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |")
    lines.append("| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |")
    lines.append("| Zep | 94.7% (LoCoMo) | n/a | 155ms |")
    lines.append("| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |")
    lines.append("")
    lines.append("These rows are not head-to-head comparisons: datasets and tasks differ.")

    # Per-query-type breakdown
    lines.append("")
    lines.append("## Breakdown by query type")
    lines.append("")
    by_type: dict[str, dict] = {}
    for q in results["per_query"]:
        qt = q["query_type"]
        if qt not in by_type:
            by_type[qt] = {"total": 0, "found": 0, "ranks": []}
        by_type[qt]["total"] += 1
        if q["found_at"]:
            by_type[qt]["found"] += 1
            by_type[qt]["ranks"].append(q["found_at"])

    lines.append("| Query type | Count | Recall@5 | Avg rank when found |")
    lines.append("|---|---|---|---|")
    for qt, data in sorted(by_type.items()):
        r5 = data["found"] / data["total"] if data["total"] else 0
        avg_rank = statistics.mean(data["ranks"]) if data["ranks"] else 0
        lines.append(f"| {qt} | {data['total']} | {r5:.1%} | {avg_rank:.1f} |")

    if legacy_results is not None:
        lines.extend([
            "",
            "## Legacy 60-query gate",
            "",
            f"Corpus: `{legacy_results['corpus_version']}` (60 frozen query/gold-path pairs).",
            f"Recall@5: **{legacy_results['recall_at_k'][5]:.1%}**; MRR: **{legacy_results['mrr']:.4f}**.",
            f"Gate: Recall@5 >= {LEGACY_RECALL_5_FLOOR:.0%}.",
        ])

    lines.append("")
    lines.append("## Missed at Recall@5")
    lines.append("")
    missed = [
        q for q in results["per_query"]
        if not q["found_at"] or q["found_at"] > 5
    ]
    for q in missed[:10]:
        rank = f"rank {q['found_at']}" if q["found_at"] else "not found"
        lines.append(f"- `{q['query'][:60]}` -> gold: {q['gold']} ({rank})")
    if len(missed) > 10:
        lines.append(f"- ... and {len(missed) - 10} more")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Run LLM-wiki benchmark suite.")
    p.add_argument("--semantic", action="store_true", help="Enable vector search")
    p.add_argument("--report", action="store_true", help="Write report to benchmark/report.md")
    p.add_argument("--json", action="store_true", help="Output raw JSON")
    p.add_argument(
        "--legacy-only",
        action="store_true",
        help="Run only the fixed, fast legacy-60 BM25 regression gate",
    )
    args = p.parse_args()

    print("Generating Q&A pairs from knowledge pages...")
    legacy_corpus = _load_legacy_corpus()
    tracked_pages = _tracked_knowledge_paths()
    if args.legacy_only:
        legacy_results = _run_benchmark(
            legacy_corpus["queries"],
            semantic=False,
            corpus_version=legacy_corpus["version"],
            page_paths=tracked_pages,
        )
        if args.json:
            print(json.dumps({"legacy": legacy_results}, indent=2, default=str))
        else:
            print(
                f"Legacy Recall@5: {legacy_results['recall_at_k'].get(5, 0):.1%} "
                f"(gate {LEGACY_RECALL_5_FLOOR:.0%})"
            )
        return (
            0
            if legacy_results["recall_at_k"].get(5, 0) >= LEGACY_RECALL_5_FLOOR
            else 2
        )

    qa_pairs = _current_corpus_queries()
    print(f"  Generated {len(qa_pairs)} queries")

    if not qa_pairs:
        print("No knowledge pages found to benchmark against.")
        return 1

    print(f"Running benchmark ({'hybrid BM25+Vector' if args.semantic else 'BM25 only'})...")
    results = _run_benchmark(
        qa_pairs, semantic=args.semantic, page_paths=tracked_pages
    )
    legacy_results = _run_benchmark(
        legacy_corpus["queries"],
        semantic=args.semantic,
        corpus_version=legacy_corpus["version"],
        page_paths=tracked_pages,
    )

    if args.json:
        print(json.dumps({"current": results, "legacy": legacy_results}, indent=2, default=str))
        return 0 if _passes_regression_gates(results, legacy_results) else 2

    report = _format_report(results, legacy_results)
    print(report)

    if args.report:
        report_path = INDEX_DIR / "report.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"\nReport saved to: {report_path}")

    if not _passes_regression_gates(results, legacy_results):
        print("Benchmark regression gate failed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
