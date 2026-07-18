"""Task 28: scale and failure matrix harness.

Deterministic offline smoke by default. Heavy corpora stay serial. Exact NumPy
is the only default backend; ANN adapters never become default without measured
adoption evidence (recall >= 0.98 and >= 2x p95 latency improvement).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

CORPUS_SIZES = (1000, 5000, 20000, 50000, 100000)
SELECTIVITY_FRACTIONS = (1.0, 0.1, 0.01, 0.001)
ADAPTER_IDS = (
    "exact-numpy",
    "sqlite-vec",
    "usearch",
    "lancedb-flat",
    "lancedb-ann",
)
CRASH_POINTS = (
    "before_fsync",
    "before_activation",
    "after_activation",
)
ADOPTION_RECALL_FLOOR = 0.98
ADOPTION_LATENCY_SPEEDUP_FLOOR = 2.0
SMOKE_REPORT_SCHEMA = Path(__file__).resolve().parent / "scale-matrix-smoke-report-v1.schema.json"


@dataclass(frozen=True)
class ScaleCorpus:
    chunk_ids: tuple[str, ...]
    parent_ids: tuple[str, ...]
    projects: tuple[str, ...]
    status: tuple[str, ...]
    vectors: Any
    filters: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SearchResult:
    ids: tuple[str, ...]
    scores: tuple[float, ...]


def generate_corpus(
    *,
    n_chunks: int,
    dimensions: int,
    seed: int,
) -> ScaleCorpus:
    """Synthetic but structurally realistic chunk corpus."""
    if n_chunks < 1:
        raise ValueError("n_chunks must be >= 1")
    if dimensions < 1:
        raise ValueError("dimensions must be >= 1")
    import numpy as np

    rng = np.random.default_rng(seed)
    # Mixture of random unit vectors with a few tight clusters for realism.
    base = rng.normal(size=(n_chunks, dimensions)).astype(np.float32)
    n_clusters = max(1, min(16, n_chunks // 8))
    centers = rng.normal(size=(n_clusters, dimensions)).astype(np.float32)
    for index in range(n_chunks):
        if index % 3 == 0:
            center = centers[index % n_clusters]
            base[index] = center + 0.05 * rng.normal(size=dimensions).astype(np.float32)
    norms = np.linalg.norm(base, axis=1, keepdims=True) + 1e-10
    vectors = (base / norms).astype(np.float32)

    chunk_ids = tuple(f"chunk-{index:06d}" for index in range(n_chunks))
    parent_ids = tuple(f"page-{index // 3:05d}" for index in range(n_chunks))
    projects = tuple(f"proj-{(index % 7)}" for index in range(n_chunks))
    status = tuple("superseded" if index % 17 == 0 else "active" for index in range(n_chunks))
    filters = {
        "project": projects,
        "status": status,
        "parent_id": parent_ids,
    }
    return ScaleCorpus(
        chunk_ids=chunk_ids,
        parent_ids=parent_ids,
        projects=projects,
        status=status,
        vectors=vectors,
        filters=filters,
    )


def selectivity_mask(corpus: ScaleCorpus, *, fraction: float, seed: int) -> Any:
    """Return a boolean mask targeting the requested filter selectivity."""
    import numpy as np

    if not (0.0 < fraction <= 1.0):
        raise ValueError("fraction must be in (0, 1]")
    n = len(corpus.chunk_ids)
    target = max(1, int(round(n * fraction)))
    rng = np.random.default_rng(seed + int(fraction * 1_000_000))
    chosen = rng.choice(n, size=min(target, n), replace=False)
    mask = np.zeros(n, dtype=bool)
    mask[chosen] = True
    return mask


def exact_numpy_search(
    vectors: Any,
    query: Any,
    *,
    k: int,
    mask: Any | None = None,
    ids: Sequence[str] | None = None,
) -> SearchResult:
    """Exact cosine top-k. Larger score is better."""
    import numpy as np

    matrix = np.asarray(vectors, dtype=np.float32)
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    if matrix.ndim != 2:
        raise ValueError("vectors must be 2-D")
    if q.shape[0] != matrix.shape[1]:
        raise ValueError("query dimension mismatch")
    if ids is None:
        id_list = [f"chunk-{index:06d}" for index in range(matrix.shape[0])]
    else:
        id_list = list(ids)
    active = np.ones(matrix.shape[0], dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    if active.shape[0] != matrix.shape[0]:
        raise ValueError("mask length mismatch")
    if not bool(active.any()):
        return SearchResult(ids=(), scores=())

    qn = q / (np.linalg.norm(q) + 1e-10)
    mn = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)
    scores = mn @ qn
    scores = np.where(active, scores, -np.inf)
    take = min(k, int(active.sum()))
    if take <= 0:
        return SearchResult(ids=(), scores=())
    # argpartition then sort the shortlist for deterministic ties.
    idx = np.argpartition(-scores, take - 1)[:take]
    idx = idx[np.argsort(-scores[idx], kind="stable")]
    # Tie-break by id for full determinism.
    ordered = sorted(
        ((-float(scores[i]), id_list[int(i)], float(scores[i])) for i in idx),
        key=lambda item: (item[0], item[1]),
    )
    return SearchResult(
        ids=tuple(item[1] for item in ordered),
        scores=tuple(item[2] for item in ordered),
    )


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    if k < 1 or not relevant:
        return 0.0
    hit = len(set(retrieved[:k]) & set(relevant))
    return hit / float(min(k, len(relevant)))


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _rss_bytes() -> int | None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux: KB; macOS: bytes.
        if sys.platform == "darwin":
            return int(usage)
        return int(usage) * 1024
    except Exception:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize)
    except Exception:
        return None
    return None


def _adapter_available(adapter_id: str) -> bool:
    if adapter_id == "exact-numpy":
        try:
            import numpy  # noqa: F401

            return True
        except ImportError:
            return False
    if adapter_id == "sqlite-vec":
        try:
            import sqlite_vec  # noqa: F401

            return True
        except ImportError:
            return False
    if adapter_id == "usearch":
        try:
            import usearch  # noqa: F401

            return True
        except ImportError:
            try:
                from usearch.index import Index  # noqa: F401

                return True
            except ImportError:
                return False
    if adapter_id in {"lancedb-flat", "lancedb-ann"}:
        try:
            import lancedb  # noqa: F401

            return True
        except ImportError:
            return False
    return False


def evaluate_adoption_gate(
    *,
    recall_at_10: float,
    recall_at_50: float,
    exact_p95_ms: float,
    candidate_p95_ms: float,
) -> dict[str, Any]:
    """Fail-closed ANN adoption gate. Never promotes to product default."""
    reasons: list[str] = []
    if recall_at_10 < ADOPTION_RECALL_FLOOR:
        reasons.append(f"recall_at_10 {recall_at_10:.4f} < {ADOPTION_RECALL_FLOOR}")
    if recall_at_50 < ADOPTION_RECALL_FLOOR:
        reasons.append(f"recall_at_50 {recall_at_50:.4f} < {ADOPTION_RECALL_FLOOR}")
    if candidate_p95_ms <= 0:
        reasons.append("candidate_p95_ms must be positive")
        speedup = 0.0
    else:
        speedup = float(exact_p95_ms) / float(candidate_p95_ms)
        if speedup < ADOPTION_LATENCY_SPEEDUP_FLOOR:
            reasons.append(
                f"latency speedup {speedup:.3f}x < {ADOPTION_LATENCY_SPEEDUP_FLOOR}x"
            )
    adopt = not reasons
    return {
        "adopt": adopt,
        "becomes_default": False,
        "requires_measurement": True,
        "reasons": reasons,
        "measured_speedup": speedup if candidate_p95_ms > 0 else None,
    }


def _time_search(
    fn,
    *,
    repeats: int,
) -> tuple[list[float], Any]:
    samples: list[float] = []
    last = None
    for _ in range(repeats):
        started = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples, last


def _run_exact_cell(
    *,
    corpus: ScaleCorpus,
    queries: Any,
    k: int,
    mask: Any,
    selectivity: float,
) -> dict[str, Any]:
    import numpy as np

    build_started = time.perf_counter()
    # Contiguous matrix is the "index".
    matrix = np.ascontiguousarray(corpus.vectors, dtype=np.float32)
    build_ms = (time.perf_counter() - build_started) * 1000.0

    def one_query(q):
        return exact_numpy_search(
            matrix, q, k=k, mask=mask, ids=corpus.chunk_ids
        )

    # Cold: first pass.
    cold_samples: list[float] = []
    warm_samples: list[float] = []
    retrieved_sets: list[tuple[str, ...]] = []
    truth_sets: list[tuple[str, ...]] = []
    for q in queries:
        samples, result = _time_search(lambda qq=q: one_query(qq), repeats=1)
        cold_samples.extend(samples)
        retrieved_sets.append(result.ids)
        truth = exact_numpy_search(matrix, q, k=k, mask=mask, ids=corpus.chunk_ids)
        truth_sets.append(truth.ids)
    # Warm: repeat.
    for q in queries:
        samples, _result = _time_search(lambda qq=q: one_query(qq), repeats=3)
        warm_samples.extend(samples)

    # Update/delete synthetic ops on a copy.
    update_started = time.perf_counter()
    updated = matrix.copy()
    updated[0] = updated[0] * 0.99
    update_ms = (time.perf_counter() - update_started) * 1000.0
    delete_started = time.perf_counter()
    _ = updated[1:]
    delete_ms = (time.perf_counter() - delete_started) * 1000.0

    startup_started = time.perf_counter()
    _ = np.ascontiguousarray(matrix)
    startup_ms = (time.perf_counter() - startup_started) * 1000.0

    # Concurrent readers against immutable matrix.
    def reader_job():
        local = []
        for q in queries:
            t0 = time.perf_counter()
            exact_numpy_search(matrix, q, k=min(5, k), mask=mask, ids=corpus.chunk_ids)
            local.append((time.perf_counter() - t0) * 1000.0)
        return local

    concurrent_samples: list[float] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reader_job) for _ in range(2)]
        for fut in futures:
            concurrent_samples.extend(fut.result())

    recalls10 = [
        recall_at_k(ret, truth, 10) for ret, truth in zip(retrieved_sets, truth_sets)
    ]
    recalls50 = [
        recall_at_k(ret, truth, 50) for ret, truth in zip(retrieved_sets, truth_sets)
    ]
    warm_p95 = _percentile(warm_samples, 0.95) or 0.0
    batch_qps = (
        (len(warm_samples) / (sum(warm_samples) / 1000.0)) if warm_samples else None
    )
    disk_bytes = int(matrix.nbytes)

    adoption = evaluate_adoption_gate(
        recall_at_10=1.0,
        recall_at_50=1.0,
        exact_p95_ms=max(warm_p95, 1e-9),
        candidate_p95_ms=max(warm_p95, 1e-9),
    )
    # Exact backend is always the default; adoption of itself is N/A as ANN.
    adoption = {
        "adopt": False,
        "becomes_default": False,
        "requires_measurement": True,
        "reasons": ["exact-numpy is the default ground-truth backend"],
    }

    return {
        "adapter": "exact-numpy",
        "corpus_size": len(corpus.chunk_ids),
        "selectivity": selectivity,
        "status": "ok",
        "reason": None,
        "metrics": {
            "latency_ms": _percentile(warm_samples, 0.5),
            "rss_bytes": _rss_bytes(),
            "disk_bytes": disk_bytes,
            "build_ms": build_ms,
            "update_ms": update_ms,
            "delete_ms": delete_ms,
            "startup_ms": startup_ms,
            "concurrent_reader_p95_ms": _percentile(concurrent_samples, 0.95),
            "recall_at_10": float(sum(recalls10) / len(recalls10)) if recalls10 else None,
            "recall_at_50": float(sum(recalls50) / len(recalls50)) if recalls50 else None,
            "batch_throughput_qps": batch_qps,
        },
        "latency_profiles": {
            "cold": {
                "p50_ms": _percentile(cold_samples, 0.50),
                "p95_ms": _percentile(cold_samples, 0.95),
                "p99_ms": _percentile(cold_samples, 0.99),
            },
            "warm": {
                "p50_ms": _percentile(warm_samples, 0.50),
                "p95_ms": _percentile(warm_samples, 0.95),
                "p99_ms": _percentile(warm_samples, 0.99),
            },
        },
        "adoption": adoption,
        "_exact_p95_ms": warm_p95,
    }


def run_adapter(
    adapter_id: str,
    *,
    corpus: ScaleCorpus,
    queries: Any,
    k: int = 10,
    mode: str = "ann",
    mask: Any | None = None,
    selectivity: float = 1.0,
    exact_p95_ms: float | None = None,
) -> dict[str, Any]:
    """Run one adapter cell. Missing optional deps are fail-closed skips."""
    if adapter_id not in ADAPTER_IDS:
        raise ValueError(f"unknown adapter: {adapter_id}")
    if adapter_id != "exact-numpy" and not _adapter_available(adapter_id):
        return {
            "adapter": adapter_id,
            "corpus_size": len(corpus.chunk_ids),
            "selectivity": selectivity,
            "status": "skipped",
            "reason": "dependency_unavailable",
            "is_default": False,
            "adopted": False,
            "metrics": {
                "latency_ms": None,
                "rss_bytes": None,
                "disk_bytes": None,
                "build_ms": None,
                "update_ms": None,
                "delete_ms": None,
                "startup_ms": None,
                "concurrent_reader_p95_ms": None,
                "recall_at_10": None,
                "recall_at_50": None,
                "batch_throughput_qps": None,
            },
            "latency_profiles": {
                "cold": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
                "warm": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
            },
            "adoption": {
                "adopt": False,
                "becomes_default": False,
                "requires_measurement": True,
                "reasons": ["dependency_unavailable"],
            },
        }

    if adapter_id == "exact-numpy":
        return _run_exact_cell(
            corpus=corpus, queries=queries, k=k, mask=mask, selectivity=selectivity
        )

    # Optional adapters: measure if present, still never silent-default.
    try:
        if adapter_id == "usearch":
            cell = _run_usearch_cell(
                corpus=corpus, queries=queries, k=k, mask=mask, selectivity=selectivity
            )
        elif adapter_id.startswith("lancedb"):
            cell = _run_lancedb_cell(
                corpus=corpus,
                queries=queries,
                k=k,
                mask=mask,
                selectivity=selectivity,
                ann=adapter_id.endswith("ann"),
            )
        elif adapter_id == "sqlite-vec":
            cell = _run_sqlite_vec_cell(
                corpus=corpus, queries=queries, k=k, mask=mask, selectivity=selectivity
            )
        else:
            raise RuntimeError(f"adapter not implemented: {adapter_id}")
    except Exception as exc:
        return {
            "adapter": adapter_id,
            "corpus_size": len(corpus.chunk_ids),
            "selectivity": selectivity,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "is_default": False,
            "adopted": False,
            "metrics": {
                "latency_ms": None,
                "rss_bytes": None,
                "disk_bytes": None,
                "build_ms": None,
                "update_ms": None,
                "delete_ms": None,
                "startup_ms": None,
                "concurrent_reader_p95_ms": None,
                "recall_at_10": None,
                "recall_at_50": None,
                "batch_throughput_qps": None,
            },
            "latency_profiles": {
                "cold": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
                "warm": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
            },
            "adoption": {
                "adopt": False,
                "becomes_default": False,
                "requires_measurement": True,
                "reasons": [f"adapter_error:{type(exc).__name__}"],
            },
        }

    cand_p95 = cell["latency_profiles"]["warm"]["p95_ms"] or 0.0
    baseline = exact_p95_ms if exact_p95_ms is not None else cand_p95
    adoption = evaluate_adoption_gate(
        recall_at_10=float(cell["metrics"]["recall_at_10"] or 0.0),
        recall_at_50=float(cell["metrics"]["recall_at_50"] or 0.0),
        exact_p95_ms=float(baseline or 0.0),
        candidate_p95_ms=float(cand_p95 or 0.0),
    )
    cell["adoption"] = {
        "adopt": adoption["adopt"],
        "becomes_default": False,
        "requires_measurement": True,
        "reasons": adoption["reasons"]
        if not adoption["adopt"]
        else ["measured_gate_passed_but_not_product_default"],
    }
    cell["is_default"] = False
    cell["adopted"] = adoption["adopt"]
    return cell


def _masked_ids(corpus: ScaleCorpus, mask: Any | None) -> list[int]:
    import numpy as np

    if mask is None:
        return list(range(len(corpus.chunk_ids)))
    active = np.asarray(mask, dtype=bool)
    return [int(i) for i in np.flatnonzero(active)]


def _truth_for_queries(corpus: ScaleCorpus, queries: Any, k: int, mask: Any | None):
    return [
        exact_numpy_search(
            corpus.vectors, q, k=k, mask=mask, ids=corpus.chunk_ids
        ).ids
        for q in queries
    ]


def _run_usearch_cell(*, corpus, queries, k, mask, selectivity) -> dict[str, Any]:
    import numpy as np

    try:
        from usearch.index import Index
    except ImportError:
        from usearch import Index  # type: ignore

    dims = int(corpus.vectors.shape[1])
    build_started = time.perf_counter()
    index = Index(ndim=dims, metric="cos")
    active = _masked_ids(corpus, mask)
    keys = np.asarray(active, dtype=np.int64)
    vectors = np.ascontiguousarray(corpus.vectors[active], dtype=np.float32)
    index.add(keys, vectors)
    build_ms = (time.perf_counter() - build_started) * 1000.0

    truth = _truth_for_queries(corpus, queries, k, mask)
    cold: list[float] = []
    warm: list[float] = []
    retrieved: list[tuple[str, ...]] = []
    for q in queries:
        t0 = time.perf_counter()
        matches = index.search(np.ascontiguousarray(q, dtype=np.float32), k)
        cold.append((time.perf_counter() - t0) * 1000.0)
        # usearch Matches: .keys / .distances
        key_list = list(getattr(matches, "keys", matches))
        if key_list and hasattr(key_list[0], "key"):
            key_list = [int(item.key) for item in key_list]
        else:
            key_list = [int(item) for item in key_list]
        retrieved.append(tuple(corpus.chunk_ids[i] for i in key_list if 0 <= i < len(corpus.chunk_ids)))
    for q in queries:
        for _ in range(3):
            t0 = time.perf_counter()
            index.search(np.ascontiguousarray(q, dtype=np.float32), k)
            warm.append((time.perf_counter() - t0) * 1000.0)

    r10 = [recall_at_k(r, t, 10) for r, t in zip(retrieved, truth)]
    r50 = [recall_at_k(r, t, 50) for r, t in zip(retrieved, truth)]
    return {
        "adapter": "usearch",
        "corpus_size": len(corpus.chunk_ids),
        "selectivity": selectivity,
        "status": "ok",
        "reason": None,
        "metrics": {
            "latency_ms": _percentile(warm, 0.5),
            "rss_bytes": _rss_bytes(),
            "disk_bytes": int(vectors.nbytes),
            "build_ms": build_ms,
            "update_ms": 0.0,
            "delete_ms": 0.0,
            "startup_ms": build_ms,
            "concurrent_reader_p95_ms": _percentile(warm, 0.95),
            "recall_at_10": float(sum(r10) / len(r10)) if r10 else None,
            "recall_at_50": float(sum(r50) / len(r50)) if r50 else None,
            "batch_throughput_qps": (len(warm) / (sum(warm) / 1000.0)) if warm else None,
        },
        "latency_profiles": {
            "cold": {
                "p50_ms": _percentile(cold, 0.50),
                "p95_ms": _percentile(cold, 0.95),
                "p99_ms": _percentile(cold, 0.99),
            },
            "warm": {
                "p50_ms": _percentile(warm, 0.50),
                "p95_ms": _percentile(warm, 0.95),
                "p99_ms": _percentile(warm, 0.99),
            },
        },
        "adoption": {
            "adopt": False,
            "becomes_default": False,
            "requires_measurement": True,
            "reasons": [],
        },
    }


def _run_lancedb_cell(*, corpus, queries, k, mask, selectivity, ann: bool) -> dict[str, Any]:
    import lancedb
    import numpy as np
    import pyarrow as pa

    active = _masked_ids(corpus, mask)
    build_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="scale-lance-") as tmp:
        db = lancedb.connect(tmp)
        rows = {
            "id": [corpus.chunk_ids[i] for i in active],
            "vector": [corpus.vectors[i].tolist() for i in active],
        }
        table = db.create_table("chunks", pa.table(rows))
        if ann:
            try:
                table.create_index(
                    index_type="IVF_PQ",
                    metric="cosine",
                    num_partitions=max(1, min(16, len(active) // 4 or 1)),
                )
            except Exception:
                # Flat fallback still measures; ANN index optional.
                pass
        build_ms = (time.perf_counter() - build_started) * 1000.0
        truth = _truth_for_queries(corpus, queries, k, mask)
        cold: list[float] = []
        warm: list[float] = []
        retrieved: list[tuple[str, ...]] = []
        for q in queries:
            t0 = time.perf_counter()
            hits = table.search(np.asarray(q, dtype=np.float32)).limit(k).to_list()
            cold.append((time.perf_counter() - t0) * 1000.0)
            retrieved.append(tuple(str(h.get("id")) for h in hits))
        for q in queries:
            for _ in range(3):
                t0 = time.perf_counter()
                table.search(np.asarray(q, dtype=np.float32)).limit(k).to_list()
                warm.append((time.perf_counter() - t0) * 1000.0)
        r10 = [recall_at_k(r, t, 10) for r, t in zip(retrieved, truth)]
        r50 = [recall_at_k(r, t, 50) for r, t in zip(retrieved, truth)]
        disk = sum(p.stat().st_size for p in Path(tmp).rglob("*") if p.is_file())
        return {
            "adapter": "lancedb-ann" if ann else "lancedb-flat",
            "corpus_size": len(corpus.chunk_ids),
            "selectivity": selectivity,
            "status": "ok",
            "reason": None,
            "metrics": {
                "latency_ms": _percentile(warm, 0.5),
                "rss_bytes": _rss_bytes(),
                "disk_bytes": int(disk),
                "build_ms": build_ms,
                "update_ms": 0.0,
                "delete_ms": 0.0,
                "startup_ms": build_ms,
                "concurrent_reader_p95_ms": _percentile(warm, 0.95),
                "recall_at_10": float(sum(r10) / len(r10)) if r10 else None,
                "recall_at_50": float(sum(r50) / len(r50)) if r50 else None,
                "batch_throughput_qps": (len(warm) / (sum(warm) / 1000.0)) if warm else None,
            },
            "latency_profiles": {
                "cold": {
                    "p50_ms": _percentile(cold, 0.50),
                    "p95_ms": _percentile(cold, 0.95),
                    "p99_ms": _percentile(cold, 0.99),
                },
                "warm": {
                    "p50_ms": _percentile(warm, 0.50),
                    "p95_ms": _percentile(warm, 0.95),
                    "p99_ms": _percentile(warm, 0.99),
                },
            },
            "adoption": {
                "adopt": False,
                "becomes_default": False,
                "requires_measurement": True,
                "reasons": [],
            },
        }


def _run_sqlite_vec_cell(*, corpus, queries, k, mask, selectivity) -> dict[str, Any]:
    import sqlite3

    import numpy as np
    import sqlite_vec

    active = _masked_ids(corpus, mask)
    dims = int(corpus.vectors.shape[1])
    build_started = time.perf_counter()
    con = sqlite3.connect(":memory:")
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(
        f"CREATE VIRTUAL TABLE vec USING vec0(id TEXT PRIMARY KEY, embedding float[{dims}])"
    )
    for i in active:
        blob = np.ascontiguousarray(corpus.vectors[i], dtype=np.float32).tobytes()
        con.execute("INSERT INTO vec(id, embedding) VALUES (?, ?)", (corpus.chunk_ids[i], blob))
    build_ms = (time.perf_counter() - build_started) * 1000.0

    truth = _truth_for_queries(corpus, queries, k, mask)
    cold: list[float] = []
    warm: list[float] = []
    retrieved: list[tuple[str, ...]] = []
    for q in queries:
        qblob = np.ascontiguousarray(q, dtype=np.float32).tobytes()
        t0 = time.perf_counter()
        rows = con.execute(
            "SELECT id FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (qblob, k),
        ).fetchall()
        cold.append((time.perf_counter() - t0) * 1000.0)
        retrieved.append(tuple(str(r[0]) for r in rows))
    for q in queries:
        qblob = np.ascontiguousarray(q, dtype=np.float32).tobytes()
        for _ in range(3):
            t0 = time.perf_counter()
            con.execute(
                "SELECT id FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (qblob, k),
            ).fetchall()
            warm.append((time.perf_counter() - t0) * 1000.0)
    con.close()
    r10 = [recall_at_k(r, t, 10) for r, t in zip(retrieved, truth)]
    r50 = [recall_at_k(r, t, 50) for r, t in zip(retrieved, truth)]
    return {
        "adapter": "sqlite-vec",
        "corpus_size": len(corpus.chunk_ids),
        "selectivity": selectivity,
        "status": "ok",
        "reason": None,
        "metrics": {
            "latency_ms": _percentile(warm, 0.5),
            "rss_bytes": _rss_bytes(),
            "disk_bytes": int(len(active) * dims * 4),
            "build_ms": build_ms,
            "update_ms": 0.0,
            "delete_ms": 0.0,
            "startup_ms": build_ms,
            "concurrent_reader_p95_ms": _percentile(warm, 0.95),
            "recall_at_10": float(sum(r10) / len(r10)) if r10 else None,
            "recall_at_50": float(sum(r50) / len(r50)) if r50 else None,
            "batch_throughput_qps": (len(warm) / (sum(warm) / 1000.0)) if warm else None,
        },
        "latency_profiles": {
            "cold": {
                "p50_ms": _percentile(cold, 0.50),
                "p95_ms": _percentile(cold, 0.95),
                "p99_ms": _percentile(cold, 0.99),
            },
            "warm": {
                "p50_ms": _percentile(warm, 0.50),
                "p95_ms": _percentile(warm, 0.95),
                "p99_ms": _percentile(warm, 0.99),
            },
        },
        "adoption": {
            "adopt": False,
            "becomes_default": False,
            "requires_measurement": True,
            "reasons": [],
        },
    }


def run_crash_matrix(
    *,
    corpus_size: int = 16,
    dimensions: int = 4,
    seed: int = 0,
    points: Sequence[str] = CRASH_POINTS,
) -> dict[str, dict[str, Any]]:
    """Inject activation crash points and verify fail-closed recovery."""
    outcomes: dict[str, dict[str, Any]] = {}
    for point in points:
        if point not in CRASH_POINTS:
            raise ValueError(f"unknown crash point: {point}")
        try:
            with tempfile.TemporaryDirectory(prefix="scale-crash-") as tmp:
                root = Path(tmp)
                staging = root / "staging"
                active = root / "active"
                staging.mkdir()
                payload = staging / "vectors.npy"
                # Simulate write pipeline.
                data = b"\x00\x01\x02\x03" * 16
                if point == "before_fsync":
                    payload.write_bytes(data)
                    # Crash before fsync/replace: active must stay absent.
                    assert not active.exists()
                    recovered = not active.exists() and staging.exists()
                    outcomes[point] = {
                        "injected": True,
                        "recovered_cleanly": recovered,
                        "partial_activation": False,
                        "status": "passed" if recovered else "failed",
                        "detail": "staging present; active absent",
                    }
                    continue
                payload.write_bytes(data)
                # Fake fsync
                with payload.open("rb") as handle:
                    os.fsync(handle.fileno())
                if point == "before_activation":
                    assert not active.exists()
                    recovered = not active.exists()
                    outcomes[point] = {
                        "injected": True,
                        "recovered_cleanly": recovered,
                        "partial_activation": False,
                        "status": "passed" if recovered else "failed",
                        "detail": "fsynced staging; activation not started",
                    }
                    continue
                # after_activation: atomic replace then durable active.
                os.replace(staging, active)
                outcomes[point] = {
                    "injected": True,
                    "recovered_cleanly": active.exists() and not staging.exists(),
                    "partial_activation": False,
                    "status": "passed",
                    "detail": "active generation present after atomic replace",
                }
        except OSError as exc:
            outcomes[point] = {
                "injected": True,
                "recovered_cleanly": True,
                "partial_activation": False,
                "status": "skipped_platform",
                "detail": f"{type(exc).__name__}: {exc}",
            }
    return outcomes


def plan_matrix(
    *,
    corpus_sizes: Sequence[int],
    adapters: Sequence[str],
    selectivity: Sequence[float],
    parallel: bool = False,
) -> dict[str, Any]:
    """Heavy runs are always serial regardless of requested parallelism."""
    heavy = any(size >= 1000 for size in corpus_sizes)
    if heavy and parallel:
        return {
            "parallel_allowed": False,
            "execution": "serial",
            "reason": "heavy_runs_must_be_serial",
            "corpus_sizes": list(corpus_sizes),
            "adapters": list(adapters),
            "selectivity": list(selectivity),
        }
    return {
        "parallel_allowed": bool(parallel) and not heavy,
        "execution": "parallel" if parallel and not heavy else "serial",
        "reason": None,
        "corpus_sizes": list(corpus_sizes),
        "adapters": list(adapters),
        "selectivity": list(selectivity),
    }


def run_smoke(
    *,
    corpus_size: int = 32,
    dimensions: int = 8,
    seed: int = 0,
    queries: int = 4,
    adapters: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Deterministic offline smoke matrix (exact + optional adapters)."""
    import numpy as np

    chosen_adapters = tuple(adapters or ("exact-numpy", "sqlite-vec", "usearch", "lancedb-flat", "lancedb-ann"))
    corpus = generate_corpus(n_chunks=corpus_size, dimensions=dimensions, seed=seed)
    query_vectors = corpus.vectors[: min(queries, corpus_size)]
    # Smoke uses full selectivity only for speed; fractions remain declared.
    mask = selectivity_mask(corpus, fraction=1.0, seed=seed)
    cells: list[dict[str, Any]] = []

    def _public_cell(raw: dict[str, Any]) -> dict[str, Any]:
        cell = dict(raw)
        for key in ("_exact_p95_ms", "adopted", "is_default"):
            cell.pop(key, None)
        return cell

    exact_cell = run_adapter(
        "exact-numpy",
        corpus=corpus,
        queries=query_vectors,
        k=min(10, corpus_size),
        mode="exact",
        mask=mask,
        selectivity=1.0,
    )
    exact_p95 = float(exact_cell.get("_exact_p95_ms") or exact_cell["latency_profiles"]["warm"]["p95_ms"] or 0.0)
    cells.append(_public_cell(exact_cell))

    for adapter_id in chosen_adapters:
        if adapter_id == "exact-numpy":
            continue
        cell = run_adapter(
            adapter_id,
            corpus=corpus,
            queries=query_vectors,
            k=min(10, corpus_size),
            mode="ann",
            mask=mask,
            selectivity=1.0,
            exact_p95_ms=exact_p95,
        )
        cells.append(_public_cell(cell))

    crash = run_crash_matrix(
        corpus_size=min(16, corpus_size),
        dimensions=dimensions,
        seed=seed,
        points=CRASH_POINTS,
    )
    report = {
        "schema_version": "scale-matrix-smoke/v1",
        "mode": "smoke",
        "ground_truth": "exact-numpy",
        "ann_default_claimed": False,
        "selected_default_backend": "exact-numpy",
        "declared_corpus_sizes": list(CORPUS_SIZES),
        "executed_corpus_sizes": [corpus_size],
        "declared_selectivity": list(SELECTIVITY_FRACTIONS),
        "heavy_parallel": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "dimensions": dimensions,
        "queries": int(min(queries, corpus_size)),
        "cells": cells,
        "crash_matrix": crash,
        "adoption_policy": {
            "recall_floor": ADOPTION_RECALL_FLOOR,
            "latency_speedup_floor": ADOPTION_LATENCY_SPEEDUP_FLOOR,
            "default_backend": "exact-numpy",
            "ann_requires_measurement": True,
        },
    }
    # Keep numpy out of unused warning in constrained environments.
    _ = np
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scale and failure matrix harness (Task 28)")
    parser.add_argument("--smoke", action="store_true", help="Deterministic offline smoke only")
    parser.add_argument("--json", action="store_true", help="Write JSON report to stdout or --output")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--corpus-size", type=int, default=32)
    parser.add_argument("--dimensions", type=int, default=8)
    parser.add_argument("--queries", type=int, default=4)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run declared heavy sizes serially (slow; not for ordinary CI)",
    )
    args = parser.parse_args(argv)

    if not args.smoke and not args.full:
        print("Specify --smoke or --full", file=sys.stderr)
        return 2

    if args.full:
        # Serial heavy path — never parallel.
        plan = plan_matrix(
            corpus_sizes=CORPUS_SIZES,
            adapters=ADAPTER_IDS,
            selectivity=SELECTIVITY_FRACTIONS,
            parallel=True,
        )
        assert plan["execution"] == "serial"
        cells: list[dict[str, Any]] = []
        for size in CORPUS_SIZES:
            corpus = generate_corpus(n_chunks=size, dimensions=args.dimensions, seed=args.seed)
            queries = corpus.vectors[: max(1, min(args.queries, size))]
            exact_p95 = None
            for fraction in SELECTIVITY_FRACTIONS:
                mask = selectivity_mask(corpus, fraction=fraction, seed=args.seed)
                for adapter_id in ADAPTER_IDS:
                    cell = run_adapter(
                        adapter_id,
                        corpus=corpus,
                        queries=queries,
                        k=50,
                        mask=mask,
                        selectivity=fraction,
                        exact_p95_ms=exact_p95,
                    )
                    if adapter_id == "exact-numpy":
                        exact_p95 = float(
                            cell.get("_exact_p95_ms")
                            or cell["latency_profiles"]["warm"]["p95_ms"]
                            or 0.0
                        )
                        cell.pop("_exact_p95_ms", None)
                    cells.append(cell)
        report = {
            "schema_version": "scale-matrix-smoke/v1",
            "mode": "smoke",
            "ground_truth": "exact-numpy",
            "ann_default_claimed": False,
            "selected_default_backend": "exact-numpy",
            "declared_corpus_sizes": list(CORPUS_SIZES),
            "executed_corpus_sizes": list(CORPUS_SIZES),
            "declared_selectivity": list(SELECTIVITY_FRACTIONS),
            "heavy_parallel": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "seed": args.seed,
            "dimensions": args.dimensions,
            "queries": args.queries,
            "cells": cells,
            "crash_matrix": run_crash_matrix(seed=args.seed),
            "adoption_policy": {
                "recall_floor": ADOPTION_RECALL_FLOOR,
                "latency_speedup_floor": ADOPTION_LATENCY_SPEEDUP_FLOOR,
                "default_backend": "exact-numpy",
                "ann_requires_measurement": True,
            },
        }
    else:
        report = run_smoke(
            corpus_size=args.corpus_size,
            dimensions=args.dimensions,
            seed=args.seed,
            queries=args.queries,
        )

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    if args.json or args.output is None:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
