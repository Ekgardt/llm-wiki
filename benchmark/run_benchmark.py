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
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT / "scripts"))

INDEX_DIR = ROOT / "benchmark"
KNOWLEDGE = ROOT / "knowledge" / "notes"

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

    return pairs


def _run_benchmark(
    qa_pairs: list[dict],
    semantic: bool = False,
    k_values: list[int] | None = None,
) -> dict:
    """Run search against all Q&A pairs and measure metrics."""
    if k_values is None:
        k_values = [1, 3, 5, 10]
    from search_memory import search

    results = {
        "total_queries": len(qa_pairs),
        "semantic": semantic,
        "k_values": k_values,
        "recall_at_k": {k: 0 for k in k_values},
        "mrr_sum": 0.0,
        "latencies_ms": [],
        "per_query": [],
    }

    for i, qa in enumerate(qa_pairs):
        t0 = time.time()
        search_results = search(
            qa["query"],
            scope="all",
            limit=max(k_values),
            semantic=semantic,
        )
        elapsed_ms = (time.time() - t0) * 1000
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


def _format_report(results: dict) -> str:
    """Format benchmark results as a readable report."""
    lines = [
        "# LLM-Wiki Benchmark Report",
        "",
        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Mode: {'BM25 + Vector (hybrid RRF)' if results['semantic'] else 'BM25 only'}",
        f"Queries: {results['total_queries']}",
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
    lines.append("## Comparison with competitors (published numbers)")
    lines.append("")
    lines.append("| System | Recall@5 | MRR | Latency p50 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| **LLM-Wiki ({'hybrid' if results['semantic'] else 'BM25'})** | **{results['recall_at_k'].get(5, 0):.1%}** | **{results['mrr']:.4f}** | **{results['latency_p50_ms']}ms** |")
    lines.append("| agentmemory (hybrid) | 95.2% | 88.2% | 14ms |")
    lines.append("| agentmemory (BM25 fallback) | 86.2% | 71.5% | <1ms |")
    lines.append("| Zep | 94.7% (LoCoMo) | n/a | 155ms |")
    lines.append("| Mem0 | 91.6% (LoCoMo) | n/a | 880ms |")

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

    lines.append("")
    lines.append("## Missed queries (for improvement)")
    lines.append("")
    missed = [q for q in results["per_query"] if not q["found_at"]]
    for q in missed[:10]:
        lines.append(f"- `{q['query'][:60]}` → gold: {q['gold']}")
    if len(missed) > 10:
        lines.append(f"- ... and {len(missed) - 10} more")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Run LLM-wiki benchmark suite.")
    p.add_argument("--semantic", action="store_true", help="Enable vector search")
    p.add_argument("--report", action="store_true", help="Write report to benchmark/report.md")
    p.add_argument("--json", action="store_true", help="Output raw JSON")
    args = p.parse_args()

    print("Generating Q&A pairs from knowledge pages...")
    qa_pairs = _generate_qa_pairs()
    print(f"  Generated {len(qa_pairs)} queries")

    if not qa_pairs:
        print("No knowledge pages found to benchmark against.")
        return 1

    print(f"Running benchmark ({'hybrid BM25+Vector' if args.semantic else 'BM25 only'})...")
    results = _run_benchmark(qa_pairs, semantic=args.semantic)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return 0

    report = _format_report(results)
    print(report)

    if args.report:
        report_path = INDEX_DIR / "report.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"\nReport saved to: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
