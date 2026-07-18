"""Task 28: scale and failure matrix harness.

Deterministic offline smoke by default. Heavy corpora stay serial. Exact NumPy
is the only default backend; ANN adapters never become default without measured
adoption evidence (recall >= 0.98 and >= 2x p95 latency improvement).
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import platform
import subprocess
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
PRODUCT_P95_TARGET_MS = 100.0
MATERIAL_EXCEED_RATIO = 1.20
SMOKE_REPORT_SCHEMA = Path(__file__).resolve().parent / "scale-matrix-smoke-report-v1.schema.json"
FULL_REPORT_SCHEMA = Path(__file__).resolve().parent / "scale-matrix-full-report-v1.schema.json"
METRIC_KEYS = (
    "latency_ms",
    "rss_bytes",
    "disk_bytes",
    "build_ms",
    "update_ms",
    "delete_ms",
    "startup_ms",
    "concurrent_reader_p95_ms",
    "recall_at_10",
    "recall_at_50",
    "batch_throughput_qps",
)


def _require_positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1")
    return value


def _require_fraction(name: str, value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < value <= 1.0
    ):
        raise ValueError(f"{name} must be a finite number in (0, 1]")
    return float(value)


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
    _require_positive_int("n_chunks", n_chunks)
    _require_positive_int("dimensions", dimensions)
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

    _require_fraction("fraction", fraction)
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
    if matrix.shape[0] < 1 or matrix.shape[1] < 1:
        raise ValueError("vectors must have positive rows and dimensions")
    _require_positive_int("k", k)
    if q.shape[0] != matrix.shape[1]:
        raise ValueError("query dimension mismatch")
    if ids is None:
        id_list = [f"chunk-{index:06d}" for index in range(matrix.shape[0])]
    else:
        id_list = list(ids)
    if len(id_list) != matrix.shape[0] or len(set(id_list)) != len(id_list):
        raise ValueError("ids must be unique and match vectors")
    if not np.isfinite(matrix).all() or not np.isfinite(q).all():
        raise ValueError("vectors and query must be finite")
    active = np.ones(matrix.shape[0], dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    if active.ndim != 1 or active.shape[0] != matrix.shape[0]:
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
    # Full ordering is required: argpartition can choose arbitrary members of a
    # tie that straddles the top-k boundary.
    ordered = sorted(
        ((-float(scores[i]), id_list[int(i)], float(scores[i])) for i in np.flatnonzero(active)),
        key=lambda item: (item[0], item[1]),
    )[:take]
    return SearchResult(
        ids=tuple(item[1] for item in ordered),
        scores=tuple(item[2] for item in ordered),
    )


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    if k < 1 or not relevant:
        return 0.0
    hit = len(set(retrieved[:k]) & set(relevant[:k]))
    return hit / float(min(k, len(relevant)))


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if not all(math.isfinite(value) for value in ordered):
        raise ValueError("percentile samples must be finite")
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
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
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
    values = {
        "recall_at_10": recall_at_10,
        "recall_at_50": recall_at_50,
        "exact_p95_ms": exact_p95_ms,
        "candidate_p95_ms": candidate_p95_ms,
    }
    valid: dict[str, float] = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            reasons.append(f"{name} is not a measured number")
            continue
        number = float(value)
        if not math.isfinite(number):
            reasons.append(f"{name} is nonfinite")
            continue
        valid[name] = number
    for name in ("recall_at_10", "recall_at_50"):
        value = valid.get(name)
        if value is not None and not 0.0 <= value <= 1.0:
            reasons.append(f"{name} must be in [0, 1]")
        elif value is not None and value < ADOPTION_RECALL_FLOOR:
            reasons.append(f"{name} {value:.4f} < {ADOPTION_RECALL_FLOOR}")
    for name in ("exact_p95_ms", "candidate_p95_ms"):
        if name in valid and valid[name] <= 0:
            reasons.append(f"{name} must be positive")
    exact = valid.get("exact_p95_ms")
    candidate = valid.get("candidate_p95_ms")
    material_threshold = PRODUCT_P95_TARGET_MS * MATERIAL_EXCEED_RATIO
    if exact is not None and exact <= material_threshold:
        reasons.append(
            f"exact p95 does not materially exceed product p95 target "
            f"({exact:.3f}ms <= {material_threshold:.3f}ms)"
        )
    if candidate is not None and candidate > PRODUCT_P95_TARGET_MS:
        reasons.append(
            f"candidate p95 misses product p95 target "
            f"({candidate:.3f}ms > {PRODUCT_P95_TARGET_MS:.3f}ms)"
        )
    speedup = None
    if exact is not None and candidate is not None and exact > 0 and candidate > 0:
        speedup = exact / candidate
        if not math.isfinite(speedup):
            reasons.append("latency speedup is nonfinite")
            speedup = None
        elif speedup < ADOPTION_LATENCY_SPEEDUP_FLOOR:
            reasons.append(f"latency speedup {speedup:.3f}x < {ADOPTION_LATENCY_SPEEDUP_FLOOR}x")
    adopt = not reasons
    return {
        "adopt": adopt,
        "becomes_default": False,
        "requires_measurement": True,
        "reasons": reasons,
        "measured_speedup": speedup,
        "product_p95_target_ms": PRODUCT_P95_TARGET_MS,
        "material_exceed_ratio": MATERIAL_EXCEED_RATIO,
    }


def _metric_provenance(
    metrics: dict[str, int | float | None],
    *,
    source: str,
    unavailable: dict[str, str] | None = None,
) -> dict[str, dict[str, str | None]]:
    unavailable = unavailable or {}
    provenance: dict[str, dict[str, str | None]] = {}
    for name in METRIC_KEYS:
        value = metrics.get(name)
        if value is None:
            provenance[name] = {
                "status": "unavailable",
                "source": None,
                "reason": unavailable.get(name, "not_measured"),
            }
        else:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"metric {name} must be finite or null")
            provenance[name] = {"status": "measured", "source": source, "reason": None}
    return provenance


def _finalize_cell(
    cell: dict[str, Any], *, corpus: ScaleCorpus, mask: Any | None
) -> dict[str, Any]:
    selected = len(_masked_ids(corpus, mask))
    cell["filter_methodology"] = {
        "indexed_corpus_size": len(corpus.chunk_ids),
        "selected_corpus_size": selected,
        "application": "query_time",
        "equivalent_predicate": "selected = true",
        "implementation": (
            "lancedb-prefilter"
            if cell["adapter"].startswith("lancedb")
            else "numpy-mask"
            if cell["adapter"] == "exact-numpy"
            else "postfilter"
        ),
    }
    metrics = cell["metrics"]
    cell.setdefault("metric_provenance", _metric_provenance(metrics, source=cell["adapter"]))
    cell.setdefault(
        "index",
        {
            "requested": "exact" if cell["adapter"] == "exact-numpy" else "optional",
            "status": "flat" if cell["adapter"] == "exact-numpy" else "unavailable",
            "type": "exact-numpy" if cell["adapter"] == "exact-numpy" else None,
            "verified_by": "implementation" if cell["adapter"] == "exact-numpy" else None,
            "reason": None if cell["adapter"] == "exact-numpy" else "adapter_did_not_report_index",
        },
    )
    return cell


def _public_cell(raw: dict[str, Any]) -> dict[str, Any]:
    cell = dict(raw)
    for key in ("_exact_p95_ms", "adopted", "is_default"):
        cell.pop(key, None)
    return cell


def _time_search(
    fn,
    *,
    repeats: int,
) -> tuple[list[float], Any]:
    _require_positive_int("repeats", repeats)
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
        return exact_numpy_search(matrix, q, k=k, mask=mask, ids=corpus.chunk_ids)

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

    recalls10 = [recall_at_k(ret, truth, 10) for ret, truth in zip(retrieved_sets, truth_sets)]
    recalls50 = [recall_at_k(ret, truth, 50) for ret, truth in zip(retrieved_sets, truth_sets)]
    warm_p95 = _percentile(warm_samples, 0.95) or 0.0
    batch_qps = (len(warm_samples) / (sum(warm_samples) / 1000.0)) if warm_samples else None
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

    return _finalize_cell(
        {
            "adapter": "exact-numpy",
            "corpus_size": len(corpus.chunk_ids),
            "selectivity": selectivity,
            "status": "ok",
            "reason": None,
            "metrics": {
                "latency_ms": _percentile(warm_samples, 0.5),
                "rss_bytes": _rss_bytes(),
                "disk_bytes": None,
                "build_ms": build_ms,
                "update_ms": None,
                "delete_ms": None,
                "startup_ms": None,
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
        },
        corpus=corpus,
        mask=mask,
    )


def _unavailable_cell(
    adapter_id: str,
    *,
    corpus: ScaleCorpus,
    mask: Any | None,
    selectivity: float,
    status: str,
    reason: str,
) -> dict[str, Any]:
    metrics = {name: None for name in METRIC_KEYS}
    cell = {
        "adapter": adapter_id,
        "corpus_size": len(corpus.chunk_ids),
        "selectivity": selectivity,
        "status": status,
        "reason": reason,
        "is_default": False,
        "adopted": False,
        "metrics": metrics,
        "metric_provenance": _metric_provenance(
            metrics,
            source=adapter_id,
            unavailable={name: reason for name in METRIC_KEYS},
        ),
        "latency_profiles": {
            "cold": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
            "warm": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
        },
        "adoption": {
            "adopt": False,
            "becomes_default": False,
            "requires_measurement": True,
            "reasons": [reason],
        },
        "index": {
            "requested": "ann" if adapter_id == "lancedb-ann" else "optional",
            "status": "unavailable",
            "type": None,
            "verified_by": None,
            "reason": reason,
        },
    }
    return _finalize_cell(cell, corpus=corpus, mask=mask)


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
    _require_positive_int("k", k)
    if len(queries) < 1:
        raise ValueError("queries must be non-empty")
    _require_fraction("selectivity", selectivity)
    if adapter_id != "exact-numpy" and not _adapter_available(adapter_id):
        return _unavailable_cell(
            adapter_id,
            corpus=corpus,
            mask=mask,
            selectivity=selectivity,
            status="skipped",
            reason="dependency_unavailable",
        )

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
        return _unavailable_cell(
            adapter_id,
            corpus=corpus,
            mask=mask,
            selectivity=selectivity,
            status="failed",
            reason=f"adapter_error:{type(exc).__name__}: {exc}",
        )

    cell = _finalize_cell(cell, corpus=corpus, mask=mask)
    cand_p95 = cell["latency_profiles"]["warm"]["p95_ms"]
    baseline = exact_p95_ms if exact_p95_ms is not None else cand_p95
    adoption = evaluate_adoption_gate(
        recall_at_10=cell["metrics"]["recall_at_10"],
        recall_at_50=cell["metrics"]["recall_at_50"],
        exact_p95_ms=baseline,
        candidate_p95_ms=cand_p95,
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
    if active.ndim != 1 or active.shape[0] != len(corpus.chunk_ids):
        raise ValueError("mask length must match corpus")
    return [int(i) for i in np.flatnonzero(active)]


def _truth_for_queries(corpus: ScaleCorpus, queries: Any, k: int, mask: Any | None):
    return [
        exact_numpy_search(corpus.vectors, q, k=k, mask=mask, ids=corpus.chunk_ids).ids
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
    active = set(_masked_ids(corpus, mask))
    keys = np.arange(len(corpus.chunk_ids), dtype=np.int64)
    vectors = np.ascontiguousarray(corpus.vectors, dtype=np.float32)
    index.add(keys, vectors)
    connectivity = getattr(index, "connectivity", None)
    metric = getattr(index, "metric", None)
    indexed_size = getattr(index, "size", None)
    if (
        isinstance(connectivity, bool)
        or not isinstance(connectivity, int)
        or connectivity < 1
        or isinstance(indexed_size, bool)
        or not isinstance(indexed_size, int)
        or indexed_size != len(corpus.chunk_ids)
        or "cos" not in str(metric).lower()
    ):
        raise RuntimeError(
            "unverified USEARCH HNSW cosine index: "
            f"connectivity={connectivity!r}, size={indexed_size!r}, metric={metric!r}"
        )
    build_ms = (time.perf_counter() - build_started) * 1000.0

    truth = _truth_for_queries(corpus, queries, k, mask)
    cold: list[float] = []
    warm: list[float] = []
    retrieved: list[tuple[str, ...]] = []
    for q in queries:
        t0 = time.perf_counter()
        matches = index.search(np.ascontiguousarray(q, dtype=np.float32), len(corpus.chunk_ids))
        # usearch Matches: .keys / .distances
        key_list = list(getattr(matches, "keys", matches))
        if key_list and hasattr(key_list[0], "key"):
            key_list = [int(item.key) for item in key_list]
        else:
            key_list = [int(item) for item in key_list]
        retrieved.append(tuple(corpus.chunk_ids[i] for i in key_list if i in active)[:k])
        cold.append((time.perf_counter() - t0) * 1000.0)
    for q in queries:
        for _ in range(3):
            t0 = time.perf_counter()
            matches = index.search(np.ascontiguousarray(q, dtype=np.float32), len(corpus.chunk_ids))
            key_list = list(getattr(matches, "keys", matches))
            if key_list and hasattr(key_list[0], "key"):
                key_list = [int(item.key) for item in key_list]
            else:
                key_list = [int(item) for item in key_list]
            _ = tuple(item for item in key_list if item in active)[:k]
            warm.append((time.perf_counter() - t0) * 1000.0)

    r10 = [recall_at_k(r, t, 10) for r, t in zip(retrieved, truth)]
    r50 = [recall_at_k(r, t, 50) for r, t in zip(retrieved, truth)]
    metrics = {
        "latency_ms": _percentile(warm, 0.5),
        "rss_bytes": _rss_bytes(),
        "disk_bytes": None,
        "build_ms": build_ms,
        "update_ms": None,
        "delete_ms": None,
        "startup_ms": None,
        "concurrent_reader_p95_ms": None,
        "recall_at_10": float(sum(r10) / len(r10)) if r10 else None,
        "recall_at_50": float(sum(r50) / len(r50)) if r50 else None,
        "batch_throughput_qps": (len(warm) / (sum(warm) / 1000.0)) if warm else None,
    }
    return {
        "adapter": "usearch",
        "corpus_size": len(corpus.chunk_ids),
        "selectivity": selectivity,
        "status": "ok",
        "reason": None,
        "metrics": metrics,
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
        "metric_provenance": _metric_provenance(metrics, source="usearch_runtime"),
        "index": {
            "requested": "ann",
            "status": "ann",
            "type": "HNSW",
            "verified_by": "usearch.Index.connectivity/size",
            "reason": None,
        },
    }


def _run_lancedb_cell(*, corpus, queries, k, mask, selectivity, ann: bool) -> dict[str, Any]:
    import lancedb
    import numpy as np
    import pyarrow as pa

    active = set(_masked_ids(corpus, mask))
    build_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="scale-lance-") as tmp:
        db = lancedb.connect(tmp)
        rows = {
            "id": list(corpus.chunk_ids),
            "vector": [vector.tolist() for vector in corpus.vectors],
            "selected": [index in active for index in range(len(corpus.chunk_ids))],
        }
        table = db.create_table("chunks", pa.table(rows))
        index_metadata = {
            "requested": "ann" if ann else "flat",
            "status": "flat",
            "type": "flat-scan",
            "verified_by": "no_vector_index_requested",
            "reason": None,
        }
        if ann:
            try:
                partitions = max(1, min(16, len(corpus.chunk_ids) // 4 or 1))
                try:
                    from lancedb.index import IvfPq
                except ImportError:
                    # LanceDB 0.20 supports only the legacy index builder.
                    table.create_index(
                        index_type="IVF_PQ",
                        metric="cosine",
                        num_partitions=partitions,
                    )
                else:
                    table.create_index(
                        "vector",
                        config=IvfPq(
                            distance_type="cosine",
                            num_partitions=partitions,
                        ),
                    )
                indices = list(table.list_indices())
                if not indices:
                    raise RuntimeError("LanceDB reported no vector index after creation")
                index_name = None
                index_type = None
                stats = None
                for descriptor in indices:
                    candidate_name = getattr(descriptor, "name", None)
                    candidate_stats = (
                        table.index_stats(candidate_name) if candidate_name is not None else None
                    )
                    candidate_type = getattr(descriptor, "index_type", None)
                    if candidate_type is None and candidate_stats is not None:
                        candidate_type = getattr(candidate_stats, "index_type", None)
                    normalized = str(candidate_type or "").upper()
                    if any(token in normalized for token in ("IVF", "HNSW")):
                        index_name = candidate_name
                        index_type = candidate_type
                        stats = candidate_stats
                        break
                if index_name is None or stats is None:
                    raise RuntimeError("LanceDB reported no verifiable ANN vector index")
                indexed_rows = getattr(stats, "num_indexed_rows", None)
                unindexed_rows = getattr(stats, "num_unindexed_rows", None)
                if indexed_rows != len(corpus.chunk_ids) or unindexed_rows not in {0, None}:
                    raise RuntimeError(
                        f"incomplete LanceDB ANN index: indexed={indexed_rows!r}, "
                        f"unindexed={unindexed_rows!r}"
                    )
                index_metadata = {
                    "requested": "ann",
                    "status": "ann",
                    "type": str(index_type),
                    "verified_by": "list_indices/index_stats indexed rows",
                    "reason": None,
                }
            except Exception as exc:
                return _unavailable_cell(
                    "lancedb-ann",
                    corpus=corpus,
                    mask=mask,
                    selectivity=selectivity,
                    status="skipped",
                    reason=f"ann_index_unavailable:{type(exc).__name__}: {exc}",
                )
        build_ms = (time.perf_counter() - build_started) * 1000.0
        truth = _truth_for_queries(corpus, queries, k, mask)
        cold: list[float] = []
        warm: list[float] = []
        retrieved: list[tuple[str, ...]] = []
        for q in queries:
            t0 = time.perf_counter()
            hits = (
                table.search(np.asarray(q, dtype=np.float32))
                .where("selected = true", prefilter=True)
                .limit(k)
                .to_list()
            )
            cold.append((time.perf_counter() - t0) * 1000.0)
            retrieved.append(tuple(str(h.get("id")) for h in hits))
        for q in queries:
            for _ in range(3):
                t0 = time.perf_counter()
                (
                    table.search(np.asarray(q, dtype=np.float32))
                    .where("selected = true", prefilter=True)
                    .limit(k)
                    .to_list()
                )
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
                "update_ms": None,
                "delete_ms": None,
                "startup_ms": None,
                "concurrent_reader_p95_ms": None,
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
            "index": index_metadata,
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
    for i in range(len(corpus.chunk_ids)):
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
            (qblob, len(corpus.chunk_ids)),
        ).fetchall()
        retrieved.append(
            tuple(str(row[0]) for row in rows if int(str(row[0]).rsplit("-", 1)[1]) in active)[:k]
        )
        cold.append((time.perf_counter() - t0) * 1000.0)
    for q in queries:
        qblob = np.ascontiguousarray(q, dtype=np.float32).tobytes()
        for _ in range(3):
            t0 = time.perf_counter()
            rows = con.execute(
                "SELECT id FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (qblob, len(corpus.chunk_ids)),
            ).fetchall()
            _ = tuple(row[0] for row in rows if int(str(row[0]).rsplit("-", 1)[1]) in active)[:k]
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
            "disk_bytes": None,
            "build_ms": build_ms,
            "update_ms": None,
            "delete_ms": None,
            "startup_ms": None,
            "concurrent_reader_p95_ms": None,
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
        "index": {
            "requested": "flat",
            "status": "flat",
            "type": "sqlite-vec-vec0",
            "verified_by": "virtual_table_creation",
            "reason": None,
        },
    }


def _fsync_directory(path: Path) -> tuple[bool, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EINVAL, errno.ENOTSUP, errno.EPERM}:
            return False, f"directory_fsync_unsupported:{exc.errno}"
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EBADF, errno.EINVAL, errno.ENOTSUP, errno.EPERM}:
                return False, f"directory_fsync_unsupported:{exc.errno}"
            raise
    finally:
        os.close(descriptor)
    return True, None


def _crash_worker(point: str, root: Path) -> None:
    staging = root / "staging"
    active = root / "active"
    staging.mkdir()
    payload = staging / "vectors.bin"
    with payload.open("wb") as handle:
        handle.write(b"scale-matrix-crash-payload")
        handle.flush()
        if point == "before_fsync":
            os._exit(91)
        os.fsync(handle.fileno())
    staging_synced, reason = _fsync_directory(staging)
    if not staging_synced:
        sys.stderr.write(reason or "staging_directory_fsync_unavailable")
        sys.stderr.flush()
        os._exit(81)
    if point == "before_activation":
        os._exit(92)
    os.replace(staging, active)
    root_synced, reason = _fsync_directory(root)
    if not root_synced:
        sys.stderr.write(reason or "activation_directory_fsync_unavailable")
        sys.stderr.flush()
        os._exit(82)
    os._exit(93)


def unavailable_crash_matrix(reason: str) -> dict[str, dict[str, Any]]:
    return {
        point: {
            "injected": False,
            "worker_returncode": None,
            "recovered_cleanly": False,
            "partial_activation": False,
            "status": "unavailable",
            "platform": sys.platform,
            "filesystem": "not_measured",
            "file_fsync": "unavailable",
            "directory_fsync": "unavailable",
            "recovery_directory_fsync": "unavailable",
            "detail": reason,
        }
        for point in CRASH_POINTS
    }


def _filesystem_description(path: Path) -> str:
    if os.name == "nt":
        try:
            import ctypes

            name = ctypes.create_unicode_buffer(64)
            root = Path(path.anchor or path.resolve().anchor)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                str(root), None, 0, None, None, None, name, len(name)
            )
            if ok and name.value:
                return name.value
        except (AttributeError, OSError):
            pass
    stats = os.statvfs(path) if hasattr(os, "statvfs") else None
    detail = f"block_size={stats.f_bsize}" if stats is not None else "type_unavailable"
    return f"local_temp:{detail}"


def run_crash_matrix(
    *,
    corpus_size: int = 16,
    dimensions: int = 4,
    seed: int = 0,
    points: Sequence[str] = CRASH_POINTS,
) -> dict[str, dict[str, Any]]:
    """Terminate a subprocess at each activation point, then inspect recovery."""
    del seed
    _require_positive_int("crash corpus_size", corpus_size)
    _require_positive_int("crash dimensions", dimensions)
    outcomes: dict[str, dict[str, Any]] = {}
    for point in points:
        if point not in CRASH_POINTS:
            raise ValueError(f"unknown crash point: {point}")
        with tempfile.TemporaryDirectory(prefix="scale-crash-") as tmp:
            root = Path(tmp)
            base = {
                "injected": False,
                "worker_returncode": None,
                "recovered_cleanly": False,
                "partial_activation": False,
                "status": "failed",
                "platform": sys.platform,
                "filesystem": _filesystem_description(root),
                "file_fsync": "unavailable",
                "directory_fsync": "unavailable",
                "recovery_directory_fsync": "not_reached",
                "detail": None,
            }
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--crash-worker",
                        point,
                        str(root),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                base["injected"] = result.returncode in {91, 92, 93}
                base["worker_returncode"] = result.returncode
                expected_returncode = {
                    "before_fsync": 91,
                    "before_activation": 92,
                    "after_activation": 93,
                }[point]
                if result.returncode == expected_returncode:
                    base["file_fsync"] = "not_reached" if point == "before_fsync" else "performed"
                    base["directory_fsync"] = (
                        "not_reached"
                        if point == "before_fsync"
                        else "performed"
                    )
                elif result.returncode in {81, 82}:
                    base["file_fsync"] = "performed"
                    base["directory_fsync"] = "unavailable"
                active = root / "active"
                staging = root / "staging"
                expected_active = point == "after_activation"
                payload = active / "vectors.bin"
                active_valid = (
                    active.is_dir() and payload.read_bytes() == b"scale-matrix-crash-payload"
                    if expected_active and payload.is_file()
                    else not active.exists()
                )
                partial = active.exists() and not active_valid
                if staging.exists():
                    import shutil

                    shutil.rmtree(staging)
                    recovered_synced, recovery_reason = _fsync_directory(root)
                    base["recovery_directory_fsync"] = (
                        "performed" if recovered_synced else "unavailable"
                    )
                else:
                    recovery_reason = None
                recovered = bool(base["injected"] and active_valid and not partial)
                base["recovered_cleanly"] = recovered
                base["partial_activation"] = partial
                base["status"] = (
                    "passed"
                    if recovered
                    and base["directory_fsync"] != "unavailable"
                    and base["recovery_directory_fsync"] != "unavailable"
                    else "unavailable"
                    if recovered or result.returncode in {81, 82}
                    else "failed"
                )
                base["detail"] = (
                    recovery_reason
                    or result.stderr.strip()
                    or "subprocess_terminated_and_recovered"
                )
            except Exception as exc:
                base["detail"] = f"{type(exc).__name__}: {exc}"
            outcomes[point] = base
    return outcomes


def plan_matrix(
    *,
    corpus_sizes: Sequence[int],
    adapters: Sequence[str],
    selectivity: Sequence[float],
    parallel: bool = False,
) -> dict[str, Any]:
    """Heavy runs are always serial regardless of requested parallelism."""
    if not corpus_sizes or any(
        isinstance(size, bool) or not isinstance(size, int) or size < 1 for size in corpus_sizes
    ):
        raise ValueError("corpus_sizes must be non-empty and positive")
    if not adapters or any(adapter not in ADAPTER_IDS for adapter in adapters):
        raise ValueError("adapters must be non-empty and known")
    if not selectivity:
        raise ValueError("selectivity must be non-empty and in (0, 1]")
    for fraction in selectivity:
        _require_fraction("selectivity", fraction)
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

    _require_positive_int("corpus_size", corpus_size)
    _require_positive_int("dimensions", dimensions)
    _require_positive_int("queries", queries)
    chosen_adapters = tuple(
        adapters or ("exact-numpy", "sqlite-vec", "usearch", "lancedb-flat", "lancedb-ann")
    )
    corpus = generate_corpus(n_chunks=corpus_size, dimensions=dimensions, seed=seed)
    query_vectors = corpus.vectors[: min(queries, corpus_size)]
    # Smoke uses full selectivity only for speed; fractions remain declared.
    mask = selectivity_mask(corpus, fraction=1.0, seed=seed)
    cells: list[dict[str, Any]] = []

    exact_cell = run_adapter(
        "exact-numpy",
        corpus=corpus,
        queries=query_vectors,
        k=min(50, corpus_size),
        mode="exact",
        mask=mask,
        selectivity=1.0,
    )
    exact_p95 = float(
        exact_cell.get("_exact_p95_ms") or exact_cell["latency_profiles"]["warm"]["p95_ms"] or 0.0
    )
    cells.append(_public_cell(exact_cell))

    for adapter_id in chosen_adapters:
        if adapter_id == "exact-numpy":
            continue
        cell = run_adapter(
            adapter_id,
            corpus=corpus,
            queries=query_vectors,
            k=min(50, corpus_size),
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
    report = build_report(
        mode="smoke",
        executed_corpus_sizes=[corpus_size],
        dimensions=dimensions,
        queries=int(min(queries, corpus_size)),
        seed=seed,
        cells=cells,
        crash_matrix=crash,
    )
    # Keep numpy out of unused warning in constrained environments.
    _ = np
    return report


def build_report(
    *,
    mode: str,
    executed_corpus_sizes: Sequence[int],
    dimensions: int,
    queries: int,
    seed: int,
    cells: Sequence[dict[str, Any]],
    crash_matrix: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if mode not in {"smoke", "full"}:
        raise ValueError("mode must be smoke or full")
    _require_positive_int("dimensions", dimensions)
    _require_positive_int("queries", queries)
    if not executed_corpus_sizes or any(
        isinstance(size, bool) or not isinstance(size, int) or size < 1
        for size in executed_corpus_sizes
    ):
        raise ValueError("executed_corpus_sizes must be non-empty and positive")
    return {
        "schema_version": f"scale-matrix-{mode}/v1",
        "mode": mode,
        "ground_truth": "exact-numpy",
        "ann_default_claimed": False,
        "selected_default_backend": "exact-numpy",
        "declared_corpus_sizes": list(CORPUS_SIZES),
        "executed_corpus_sizes": list(executed_corpus_sizes),
        "declared_selectivity": list(SELECTIVITY_FRACTIONS),
        "heavy_parallel": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "dimensions": dimensions,
        "queries": queries,
        "cells": [_public_cell(cell) for cell in cells],
        "crash_matrix": crash_matrix,
        "provenance": {
            "platform": sys.platform,
            "python": platform.python_version(),
            "harness": "benchmark/run_scale_matrix.py",
            "measurement_status": "measured" if cells else "unavailable",
        },
        "adoption_policy": {
            "recall_floor": ADOPTION_RECALL_FLOOR,
            "latency_speedup_floor": ADOPTION_LATENCY_SPEEDUP_FLOOR,
            "product_p95_target_ms": PRODUCT_P95_TARGET_MS,
            "material_exceed_ratio": MATERIAL_EXCEED_RATIO,
            "default_backend": "exact-numpy",
            "ann_requires_measurement": True,
        },
    }


def _assert_finite_json(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"nonfinite report value at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_finite_json(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_finite_json(child, f"{path}[{index}]")


def _validate_metric_provenance(report: dict[str, Any]) -> None:
    for cell_index, cell in enumerate(report.get("cells", [])):
        metrics = cell.get("metrics", {})
        provenance = cell.get("metric_provenance", {})
        for metric in METRIC_KEYS:
            value = metrics.get(metric)
            metric_provenance = provenance.get(metric, {})
            status = metric_provenance.get("status")
            if (value is None) != (status == "unavailable"):
                raise ValueError(f"metric provenance mismatch at cells[{cell_index}].{metric}")
            if status == "measured" and (
                not metric_provenance.get("source") or metric_provenance.get("reason") is not None
            ):
                raise ValueError(f"metric provenance mismatch at cells[{cell_index}].{metric}")
            if status == "unavailable" and (
                metric_provenance.get("source") is not None
                or not metric_provenance.get("reason")
            ):
                raise ValueError(f"metric provenance mismatch at cells[{cell_index}].{metric}")


def _validate_report_semantics(report: dict[str, Any], *, mode: str) -> None:
    cells = report.get("cells", [])
    observed = [
        (cell.get("corpus_size"), cell.get("selectivity"), cell.get("adapter"))
        for cell in cells
    ]
    if mode == "full":
        expected = [
            (size, fraction, adapter)
            for size in CORPUS_SIZES
            for fraction in SELECTIVITY_FRACTIONS
            for adapter in ADAPTER_IDS
        ]
        if sorted(observed, key=str) != sorted(expected, key=str):
            raise ValueError("full matrix cells must contain each declared combination exactly once")
    else:
        executed_sizes = report.get("executed_corpus_sizes", [])
        smoke_adapters = [adapter for _size, _fraction, adapter in observed]
        if (
            len(executed_sizes) != 1
            or len(smoke_adapters) != len(set(smoke_adapters))
            or smoke_adapters.count("exact-numpy") != 1
            or any(size != executed_sizes[0] or fraction != 1.0 for size, fraction, _ in observed)
        ):
            raise ValueError("smoke matrix cells must contain one cell per adapter at full selectivity")
    for cell_index, cell in enumerate(cells):
        methodology = cell.get("filter_methodology", {})
        if methodology.get("indexed_corpus_size") != cell.get("corpus_size"):
            raise ValueError(f"filter methodology mismatch at cells[{cell_index}]")
        adapter = cell.get("adapter")
        expected_implementation = (
            "lancedb-prefilter"
            if isinstance(adapter, str) and adapter.startswith("lancedb")
            else "numpy-mask"
            if adapter == "exact-numpy"
            else "postfilter"
        )
        if methodology.get("implementation") != expected_implementation:
            raise ValueError(f"filter methodology mismatch at cells[{cell_index}]")
        size = cell.get("corpus_size")
        fraction = cell.get("selectivity")
        if isinstance(size, int) and isinstance(fraction, (int, float)):
            expected_selected = max(1, round(size * fraction))
            if methodology.get("selected_corpus_size") != expected_selected:
                raise ValueError(f"filter methodology mismatch at cells[{cell_index}]")


def write_report_atomic(report: dict[str, Any], output: Path, *, mode: str) -> None:
    from reliable_memory import validate_schema

    schema = (
        SMOKE_REPORT_SCHEMA if mode == "smoke" else FULL_REPORT_SCHEMA if mode == "full" else None
    )
    if schema is None:
        raise ValueError("mode must be smoke or full")
    _assert_finite_json(report)
    _validate_metric_provenance(report)
    _validate_report_semantics(report, mode=mode)
    validate_schema(report, schema)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        _fsync_directory(output.parent)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "--crash-worker":
        if len(raw_argv) != 3 or raw_argv[1] not in CRASH_POINTS:
            return 2
        _crash_worker(raw_argv[1], Path(raw_argv[2]))
        return 1
    parser = argparse.ArgumentParser(description="Scale and failure matrix harness (Task 28)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true", help="Deterministic offline smoke only")
    parser.add_argument(
        "--json", action="store_true", help="Write JSON report to stdout or --output"
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--corpus-size", type=int, default=32)
    parser.add_argument("--dimensions", type=int, default=8)
    parser.add_argument("--queries", type=int, default=4)
    mode.add_argument(
        "--full",
        action="store_true",
        help="Run declared heavy sizes serially (slow; not for ordinary CI)",
    )
    args = parser.parse_args(raw_argv)

    for name in ("corpus_size", "dimensions", "queries"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be >= 1")

    if args.full and args.queries > min(CORPUS_SIZES):
        parser.error(f"--queries must be <= {min(CORPUS_SIZES)} for --full")

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
        report = build_report(
            mode="full",
            executed_corpus_sizes=CORPUS_SIZES,
            dimensions=args.dimensions,
            queries=args.queries,
            seed=args.seed,
            cells=cells,
            crash_matrix=run_crash_matrix(seed=args.seed),
        )
    else:
        report = run_smoke(
            corpus_size=args.corpus_size,
            dimensions=args.dimensions,
            seed=args.seed,
            queries=args.queries,
        )

    _assert_finite_json(report)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        write_report_atomic(report, args.output, mode=report["mode"])
    if args.json or args.output is None:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
