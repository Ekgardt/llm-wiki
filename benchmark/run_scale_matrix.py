"""Task 28: scale and failure matrix harness.

Deterministic offline smoke by default. Heavy corpora stay serial. Exact NumPy
is the only default backend; ANN adapters never become default without measured
adoption evidence (recall >= 0.98 and >= 2x p95 latency improvement).
"""

from __future__ import annotations

import argparse
import errno
import importlib
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


def _clustered_unit_vectors(rng: Any, n_chunks: int, dimensions: int) -> Any:
    """Random unit vectors; every third one sits in one of a few tight clusters."""
    import numpy as np

    base = rng.normal(size=(n_chunks, dimensions)).astype(np.float32)
    n_clusters = max(1, min(16, n_chunks // 8))
    centers = rng.normal(size=(n_clusters, dimensions)).astype(np.float32)
    for index in range(n_chunks):
        if index % 3 == 0:
            center = centers[index % n_clusters]
            base[index] = center + 0.05 * rng.normal(size=dimensions).astype(np.float32)
    norms = np.linalg.norm(base, axis=1, keepdims=True) + 1e-10
    return (base / norms).astype(np.float32)


def _status_of(index: int) -> str:
    if index % 17 == 0:
        return "superseded"
    return "active"


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
    vectors = _clustered_unit_vectors(rng, n_chunks, dimensions)
    chunk_ids = tuple(f"chunk-{index:06d}" for index in range(n_chunks))
    parent_ids = tuple(f"page-{index // 3:05d}" for index in range(n_chunks))
    projects = tuple(f"proj-{(index % 7)}" for index in range(n_chunks))
    status = tuple(_status_of(index) for index in range(n_chunks))
    return ScaleCorpus(
        chunk_ids=chunk_ids,
        parent_ids=parent_ids,
        projects=projects,
        status=status,
        vectors=vectors,
        filters={"project": projects, "status": status, "parent_id": parent_ids},
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


def _validated_matrix(vectors: Any) -> Any:
    import numpy as np

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("vectors must be 2-D")
    if matrix.shape[0] < 1 or matrix.shape[1] < 1:
        raise ValueError("vectors must have positive rows and dimensions")
    return matrix


def _validated_query(query: Any, matrix: Any) -> Any:
    import numpy as np

    q = np.asarray(query, dtype=np.float32).reshape(-1)
    if q.shape[0] != matrix.shape[1]:
        raise ValueError("query dimension mismatch")
    return q


def _validated_ids(ids: Sequence[str] | None, rows: int) -> list[str]:
    id_list = [f"chunk-{index:06d}" for index in range(rows)] if ids is None else list(ids)
    if len(id_list) != rows or len(set(id_list)) != len(id_list):
        raise ValueError("ids must be unique and match vectors")
    return id_list


def _validated_mask(mask: Any | None, matrix: Any) -> Any:
    import numpy as np

    active = np.ones(matrix.shape[0], dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    if active.ndim != 1 or active.shape[0] != matrix.shape[0]:
        raise ValueError("mask length mismatch")
    return active


def _ordered_top(scores: Any, active: Any, id_list: list[str], take: int) -> SearchResult:
    """The top `take` by score then id: a full ordering, because argpartition
    can choose arbitrary members of a tie that straddles the boundary."""
    import numpy as np

    ordered = sorted(
        ((-float(scores[i]), id_list[int(i)], float(scores[i])) for i in np.flatnonzero(active)),
        key=lambda item: (item[0], item[1]),
    )[:take]
    return SearchResult(
        ids=tuple(item[1] for item in ordered),
        scores=tuple(item[2] for item in ordered),
    )


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

    matrix = _validated_matrix(vectors)
    _require_positive_int("k", k)
    q = _validated_query(query, matrix)
    id_list = _validated_ids(ids, matrix.shape[0])
    if not np.isfinite(matrix).all() or not np.isfinite(q).all():
        raise ValueError("vectors and query must be finite")
    active = _validated_mask(mask, matrix)
    if not bool(active.any()):
        return SearchResult(ids=(), scores=())
    qn = q / (np.linalg.norm(q) + 1e-10)
    mn = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)
    scores = np.where(active, mn @ qn, -np.inf)
    take = min(k, int(active.sum()))
    if take <= 0:
        return SearchResult(ids=(), scores=())
    return _ordered_top(scores, active, id_list, take)


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    if k < 1 or not relevant:
        return 0.0
    hit = len(set(retrieved[:k]) & set(relevant[:k]))
    return hit / float(min(k, len(relevant)))


def _ordered_finite(values: Sequence[float]) -> list[float]:
    ordered = sorted(float(v) for v in values)
    if not all(math.isfinite(value) for value in ordered):
        raise ValueError("percentile samples must be finite")
    return ordered


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = _ordered_finite(values)
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


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


_ADAPTER_MODULES = {
    "exact-numpy": ("numpy",),
    "sqlite-vec": ("sqlite_vec",),
    "usearch": ("usearch", "usearch.index"),
    "lancedb-flat": ("lancedb",),
    "lancedb-ann": ("lancedb",),
}


def _adapter_available(adapter_id: str) -> bool:
    modules = _ADAPTER_MODULES.get(adapter_id)
    if modules is None:
        return False
    return any(_importable(name) for name in modules)


def _measured_number(name: str, value: object) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{name} is not a measured number"
    number = float(value)
    if not math.isfinite(number):
        return None, f"{name} is nonfinite"
    return number, None


def _valid_gate_values(values: dict[str, object]) -> tuple[dict[str, float], list[str]]:
    valid: dict[str, float] = {}
    reasons: list[str] = []
    for name, value in values.items():
        number, reason = _measured_number(name, value)
        if reason is not None:
            reasons.append(reason)
            continue
        valid[name] = number
    return valid, reasons


def _recall_reason(name: str, value: float | None) -> str | None:
    if value is None:
        return None
    if not 0.0 <= value <= 1.0:
        return f"{name} must be in [0, 1]"
    if value < ADOPTION_RECALL_FLOOR:
        return f"{name} {value:.4f} < {ADOPTION_RECALL_FLOOR}"
    return None


def _positive_reasons(valid: dict[str, float]) -> list[str]:
    reasons = []
    for name in ("exact_p95_ms", "candidate_p95_ms"):
        if name in valid and valid[name] <= 0:
            reasons.append(f"{name} must be positive")
    return reasons


def _target_reasons(exact: float | None, candidate: float | None) -> list[str]:
    reasons = []
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
    return reasons


def _both_positive(exact: float | None, candidate: float | None) -> bool:
    if exact is None or candidate is None:
        return False
    return exact > 0 and candidate > 0


def _speedup(exact: float | None, candidate: float | None) -> tuple[float | None, str | None]:
    if not _both_positive(exact, candidate):
        return None, None
    speedup = exact / candidate
    if not math.isfinite(speedup):
        return None, "latency speedup is nonfinite"
    if speedup < ADOPTION_LATENCY_SPEEDUP_FLOOR:
        return speedup, f"latency speedup {speedup:.3f}x < {ADOPTION_LATENCY_SPEEDUP_FLOOR}x"
    return speedup, None


def evaluate_adoption_gate(
    *,
    recall_at_10: float,
    recall_at_50: float,
    exact_p95_ms: float,
    candidate_p95_ms: float,
) -> dict[str, Any]:
    """Fail-closed ANN adoption gate. Never promotes to product default."""
    values = {
        "recall_at_10": recall_at_10,
        "recall_at_50": recall_at_50,
        "exact_p95_ms": exact_p95_ms,
        "candidate_p95_ms": candidate_p95_ms,
    }
    valid, reasons = _valid_gate_values(values)
    for name in ("recall_at_10", "recall_at_50"):
        reason = _recall_reason(name, valid.get(name))
        if reason is not None:
            reasons.append(reason)
    reasons.extend(_positive_reasons(valid))
    exact = valid.get("exact_p95_ms")
    candidate = valid.get("candidate_p95_ms")
    reasons.extend(_target_reasons(exact, candidate))
    speedup, speedup_reason = _speedup(exact, candidate)
    if speedup_reason is not None:
        reasons.append(speedup_reason)
    return {
        "adopt": not reasons,
        "becomes_default": False,
        "requires_measurement": True,
        "reasons": reasons,
        "measured_speedup": speedup,
        "product_p95_target_ms": PRODUCT_P95_TARGET_MS,
        "material_exceed_ratio": MATERIAL_EXCEED_RATIO,
    }


_EXACT_RECALL_UNAVAILABLE = {
    "recall_at_10": "ground_truth_backend",
    "recall_at_50": "ground_truth_backend",
}


def _require_finite_metric(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"metric {name} must be finite or null")


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
            continue
        _require_finite_metric(name, value)
        provenance[name] = {"status": "measured", "source": source, "reason": None}
    return provenance


def _filter_implementation(adapter: object) -> str:
    if isinstance(adapter, str) and adapter.startswith("lancedb"):
        return "lancedb-prefilter"
    if adapter == "exact-numpy":
        return "numpy-mask"
    return "postfilter"


def _default_index_metadata(adapter: str) -> dict[str, str | None]:
    if adapter == "exact-numpy":
        return {
            "requested": "exact",
            "status": "flat",
            "type": "exact-numpy",
            "verified_by": "implementation",
            "reason": None,
        }
    return {
        "requested": "optional",
        "status": "unavailable",
        "type": None,
        "verified_by": None,
        "reason": "adapter_did_not_report_index",
    }


def _finalize_cell(
    cell: dict[str, Any], *, corpus: ScaleCorpus, mask: Any | None
) -> dict[str, Any]:
    adapter = cell["adapter"]
    cell["filter_methodology"] = {
        "indexed_corpus_size": len(corpus.chunk_ids),
        "selected_corpus_size": len(_masked_ids(corpus, mask)),
        "application": "query_time",
        "equivalent_predicate": "selected = true",
        "implementation": _filter_implementation(adapter),
    }
    cell.setdefault("metric_provenance", _metric_provenance(cell["metrics"], source=adapter))
    cell.setdefault("index", _default_index_metadata(adapter))
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


def _timed_pass(queries: Any, one_query: Any, *, repeats: int) -> list[float]:
    """One pass over the queries; every sample of every query, in order."""
    samples: list[float] = []
    for q in queries:
        taken, _result = _time_search(lambda qq=q: one_query(qq), repeats=repeats)
        samples.extend(taken)
    return samples


def _concurrent_reader_samples(matrix: Any, queries: Any, k: int, mask: Any, ids: Any) -> list[float]:
    """Two readers over the immutable matrix at once; their per-query latencies."""

    def reader_job():
        local = []
        for q in queries:
            t0 = time.perf_counter()
            exact_numpy_search(matrix, q, k=min(5, k), mask=mask, ids=ids)
            local.append((time.perf_counter() - t0) * 1000.0)
        return local

    samples: list[float] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(reader_job) for _ in range(2)]:
            samples.extend(future.result())
    return samples


def _batch_qps(warm_samples: list[float]) -> float | None:
    if not warm_samples:
        return None
    return len(warm_samples) / (sum(warm_samples) / 1000.0)


def _latency_profile(samples: list[float]) -> dict[str, float | None]:
    return {
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
    }


def _exact_metrics(
    *, build_ms: float, warm_samples: list[float], concurrent_samples: list[float]
) -> dict[str, Any]:
    return {
        "latency_ms": _percentile(warm_samples, 0.5),
        "rss_bytes": _rss_bytes(),
        "disk_bytes": None,
        "build_ms": build_ms,
        "update_ms": None,
        "delete_ms": None,
        "startup_ms": None,
        "concurrent_reader_p95_ms": _percentile(concurrent_samples, 0.95),
        # The exact backend is the ground truth the other cells are graded
        # against; graded against itself it read 1.0 by construction (audit
        # M11), so it reports no recall and says why.
        "recall_at_10": None,
        "recall_at_50": None,
        "batch_throughput_qps": _batch_qps(warm_samples),
    }


# Exact backend is always the default; adoption of itself is N/A as ANN.
_EXACT_ADOPTION = {
    "adopt": False,
    "becomes_default": False,
    "requires_measurement": True,
    "reasons": ["exact-numpy is the default ground-truth backend"],
}


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

    cold_samples = _timed_pass(queries, one_query, repeats=1)
    warm_samples = _timed_pass(queries, one_query, repeats=3)
    concurrent_samples = _concurrent_reader_samples(matrix, queries, k, mask, corpus.chunk_ids)
    metrics = _exact_metrics(
        build_ms=build_ms, warm_samples=warm_samples, concurrent_samples=concurrent_samples
    )
    return _finalize_cell(
        {
            "adapter": "exact-numpy",
            "corpus_size": len(corpus.chunk_ids),
            "selectivity": selectivity,
            "status": "ok",
            "reason": None,
            "metrics": metrics,
            "metric_provenance": _metric_provenance(
                metrics, source="exact-numpy", unavailable=_EXACT_RECALL_UNAVAILABLE
            ),
            "latency_profiles": {
                "cold": _latency_profile(cold_samples),
                "warm": _latency_profile(warm_samples),
            },
            "adoption": dict(_EXACT_ADOPTION, reasons=list(_EXACT_ADOPTION["reasons"])),
            "_exact_p95_ms": _percentile(warm_samples, 0.95) or 0.0,
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


def _require_adapter_arguments(adapter_id: str, queries: Any, k: int, selectivity: float) -> None:
    if adapter_id not in ADAPTER_IDS:
        raise ValueError(f"unknown adapter: {adapter_id}")
    _require_positive_int("k", k)
    if len(queries) < 1:
        raise ValueError("queries must be non-empty")
    _require_fraction("selectivity", selectivity)


def _run_optional_cell(adapter_id: str, **cell_arguments: Any) -> dict[str, Any]:
    if adapter_id == "usearch":
        return _run_usearch_cell(**cell_arguments)
    if adapter_id.startswith("lancedb"):
        return _run_lancedb_cell(**cell_arguments, ann=adapter_id.endswith("ann"))
    if adapter_id == "sqlite-vec":
        return _run_sqlite_vec_cell(**cell_arguments)
    raise RuntimeError(f"adapter not implemented: {adapter_id}")


def _adoption_for(cell: dict[str, Any], exact_p95_ms: float | None) -> dict[str, Any]:
    cand_p95 = cell["latency_profiles"]["warm"]["p95_ms"]
    baseline = cand_p95 if exact_p95_ms is None else exact_p95_ms
    adoption = evaluate_adoption_gate(
        recall_at_10=cell["metrics"]["recall_at_10"],
        recall_at_50=cell["metrics"]["recall_at_50"],
        exact_p95_ms=baseline,
        candidate_p95_ms=cand_p95,
    )
    reasons = ["measured_gate_passed_but_not_product_default"] if adoption["adopt"] else adoption["reasons"]
    return {
        "adopt": adoption["adopt"],
        "becomes_default": False,
        "requires_measurement": True,
        "reasons": reasons,
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
    _require_adapter_arguments(adapter_id, queries, k, selectivity)
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
        cell = _run_optional_cell(
            adapter_id, corpus=corpus, queries=queries, k=k, mask=mask, selectivity=selectivity
        )
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
    cell["adoption"] = _adoption_for(cell, exact_p95_ms)
    cell["is_default"] = False
    cell["adopted"] = cell["adoption"]["adopt"]
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


def _mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _recall_pair(retrieved: list[tuple[str, ...]], truth: Any) -> tuple[float | None, float | None]:
    r10 = [recall_at_k(r, t, 10) for r, t in zip(retrieved, truth)]
    r50 = [recall_at_k(r, t, 50) for r, t in zip(retrieved, truth)]
    return _mean_or_none(r10), _mean_or_none(r50)


def _ann_metrics(
    *,
    warm: list[float],
    build_ms: float,
    disk_bytes: int | None,
    recall10: float | None,
    recall50: float | None,
) -> dict[str, Any]:
    return {
        "latency_ms": _percentile(warm, 0.5),
        "rss_bytes": _rss_bytes(),
        "disk_bytes": disk_bytes,
        "build_ms": build_ms,
        "update_ms": None,
        "delete_ms": None,
        "startup_ms": None,
        "concurrent_reader_p95_ms": None,
        "recall_at_10": recall10,
        "recall_at_50": recall50,
        "batch_throughput_qps": _batch_qps(warm),
    }


def _measurement_only_adoption() -> dict[str, Any]:
    return {"adopt": False, "becomes_default": False, "requires_measurement": True, "reasons": []}


def _adapter_cell(
    adapter: str,
    corpus: ScaleCorpus,
    selectivity: float,
    metrics: dict[str, Any],
    cold: list[float],
    warm: list[float],
    index: dict[str, Any],
) -> dict[str, Any]:
    return {
        "adapter": adapter,
        "corpus_size": len(corpus.chunk_ids),
        "selectivity": selectivity,
        "status": "ok",
        "reason": None,
        "metrics": metrics,
        "latency_profiles": {"cold": _latency_profile(cold), "warm": _latency_profile(warm)},
        "adoption": _measurement_only_adoption(),
        "index": index,
    }


def _is_positive_int(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 1


def _is_exact_int(value: object, expected: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value == expected


def _require_verified_usearch_index(index: Any, expected_size: int) -> None:
    connectivity = getattr(index, "connectivity", None)
    metric = getattr(index, "metric", None)
    indexed_size = getattr(index, "size", None)
    if (
        not _is_positive_int(connectivity)
        or not _is_exact_int(indexed_size, expected_size)
        or "cos" not in str(metric).lower()
    ):
        raise RuntimeError(
            "unverified USEARCH HNSW cosine index: "
            f"connectivity={connectivity!r}, size={indexed_size!r}, metric={metric!r}"
        )


def _usearch_index(corpus: ScaleCorpus) -> Any:
    import numpy as np

    try:
        from usearch.index import Index
    except ImportError:
        from usearch import Index  # type: ignore

    index = Index(ndim=int(corpus.vectors.shape[1]), metric="cos")
    keys = np.arange(len(corpus.chunk_ids), dtype=np.int64)
    index.add(keys, np.ascontiguousarray(corpus.vectors, dtype=np.float32))
    _require_verified_usearch_index(index, len(corpus.chunk_ids))
    return index


def _usearch_keys(matches: Any) -> list[int]:
    # usearch Matches: .keys / .distances
    key_list = list(getattr(matches, "keys", matches))
    if key_list and hasattr(key_list[0], "key"):
        return [int(item.key) for item in key_list]
    return [int(item) for item in key_list]


def _usearch_search(index: Any, q: Any, count: int) -> list[int]:
    import numpy as np

    return _usearch_keys(index.search(np.ascontiguousarray(q, dtype=np.float32), count))


def _usearch_cold_pass(
    index: Any, corpus: ScaleCorpus, queries: Any, active: set[int], k: int
) -> tuple[list[float], list[tuple[str, ...]]]:
    cold: list[float] = []
    retrieved: list[tuple[str, ...]] = []
    for q in queries:
        t0 = time.perf_counter()
        keys = _usearch_search(index, q, len(corpus.chunk_ids))
        retrieved.append(tuple(corpus.chunk_ids[i] for i in keys if i in active)[:k])
        cold.append((time.perf_counter() - t0) * 1000.0)
    return cold, retrieved


def _usearch_warm_pass(
    index: Any, corpus: ScaleCorpus, queries: Any, active: set[int], k: int
) -> list[float]:
    warm: list[float] = []
    for q in queries:
        for _ in range(3):
            t0 = time.perf_counter()
            keys = _usearch_search(index, q, len(corpus.chunk_ids))
            _ = tuple(item for item in keys if item in active)[:k]
            warm.append((time.perf_counter() - t0) * 1000.0)
    return warm


def _run_usearch_cell(*, corpus, queries, k, mask, selectivity) -> dict[str, Any]:
    active = set(_masked_ids(corpus, mask))
    build_started = time.perf_counter()
    index = _usearch_index(corpus)
    build_ms = (time.perf_counter() - build_started) * 1000.0
    truth = _truth_for_queries(corpus, queries, k, mask)
    cold, retrieved = _usearch_cold_pass(index, corpus, queries, active, k)
    warm = _usearch_warm_pass(index, corpus, queries, active, k)
    recall10, recall50 = _recall_pair(retrieved, truth)
    metrics = _ann_metrics(
        warm=warm, build_ms=build_ms, disk_bytes=None, recall10=recall10, recall50=recall50
    )
    cell = _adapter_cell(
        "usearch",
        corpus,
        selectivity,
        metrics,
        cold,
        warm,
        {
            "requested": "ann",
            "status": "ann",
            "type": "HNSW",
            "verified_by": "usearch.Index.connectivity/size",
            "reason": None,
        },
    )
    cell["metric_provenance"] = _metric_provenance(metrics, source="usearch_runtime")
    return cell


def _lancedb_table(db: Any, corpus: ScaleCorpus, active: set[int]) -> Any:
    import pyarrow as pa

    rows = {
        "id": list(corpus.chunk_ids),
        "vector": [vector.tolist() for vector in corpus.vectors],
        "selected": [index in active for index in range(len(corpus.chunk_ids))],
    }
    return db.create_table("chunks", pa.table(rows))


def _create_lancedb_index(table: Any, partitions: int) -> None:
    try:
        from lancedb.index import IvfPq
    except ImportError:
        # LanceDB 0.20 supports only the legacy index builder.
        table.create_index(index_type="IVF_PQ", metric="cosine", num_partitions=partitions)
        return
    table.create_index("vector", config=IvfPq(distance_type="cosine", num_partitions=partitions))


def _index_descriptor_type(table: Any, descriptor: Any) -> tuple[Any, Any, Any]:
    """(name, stats, index type) of one LanceDB index descriptor."""
    candidate_name = getattr(descriptor, "name", None)
    candidate_stats = table.index_stats(candidate_name) if candidate_name is not None else None
    candidate_type = getattr(descriptor, "index_type", None)
    if candidate_type is None and candidate_stats is not None:
        candidate_type = getattr(candidate_stats, "index_type", None)
    return candidate_name, candidate_stats, candidate_type


def _ann_index_descriptor(table: Any, indices: list) -> tuple[Any, Any, Any] | None:
    for descriptor in indices:
        name, stats, index_type = _index_descriptor_type(table, descriptor)
        normalized = str(index_type or "").upper()
        if any(token in normalized for token in ("IVF", "HNSW")):
            return name, stats, index_type
    return None


def _ann_index_found(found: tuple[Any, Any, Any] | None) -> bool:
    if found is None:
        return False
    return found[0] is not None and found[1] is not None


def _require_complete_ann_index(stats: Any, corpus_size: int) -> None:
    indexed_rows = getattr(stats, "num_indexed_rows", None)
    unindexed_rows = getattr(stats, "num_unindexed_rows", None)
    if indexed_rows != corpus_size or unindexed_rows not in {0, None}:
        raise RuntimeError(
            f"incomplete LanceDB ANN index: indexed={indexed_rows!r}, "
            f"unindexed={unindexed_rows!r}"
        )


def _verified_ann_metadata(table: Any, corpus_size: int) -> dict[str, Any]:
    indices = list(table.list_indices())
    if not indices:
        raise RuntimeError("LanceDB reported no vector index after creation")
    found = _ann_index_descriptor(table, indices)
    if not _ann_index_found(found):
        raise RuntimeError("LanceDB reported no verifiable ANN vector index")
    _require_complete_ann_index(found[1], corpus_size)
    return {
        "requested": "ann",
        "status": "ann",
        "type": str(found[2]),
        "verified_by": "list_indices/index_stats indexed rows",
        "reason": None,
    }


def _flat_index_metadata(ann: bool) -> dict[str, Any]:
    return {
        "requested": "ann" if ann else "flat",
        "status": "flat",
        "type": "flat-scan",
        "verified_by": "no_vector_index_requested",
        "reason": None,
    }


def _lancedb_index_metadata(table: Any, corpus_size: int, ann: bool) -> dict[str, Any]:
    if not ann:
        return _flat_index_metadata(False)
    partitions = max(1, min(16, corpus_size // 4 or 1))
    _create_lancedb_index(table, partitions)
    return _verified_ann_metadata(table, corpus_size)


def _lancedb_hits(table: Any, q: Any, k: int) -> list:
    import numpy as np

    return (
        table.search(np.asarray(q, dtype=np.float32))
        .where("selected = true", prefilter=True)
        .limit(k)
        .to_list()
    )


def _lancedb_cold_pass(table: Any, queries: Any, k: int) -> tuple[list[float], list[tuple[str, ...]]]:
    cold: list[float] = []
    retrieved: list[tuple[str, ...]] = []
    for q in queries:
        t0 = time.perf_counter()
        hits = _lancedb_hits(table, q, k)
        cold.append((time.perf_counter() - t0) * 1000.0)
        retrieved.append(tuple(str(h.get("id")) for h in hits))
    return cold, retrieved


def _lancedb_warm_pass(table: Any, queries: Any, k: int) -> list[float]:
    warm: list[float] = []
    for q in queries:
        for _ in range(3):
            t0 = time.perf_counter()
            _lancedb_hits(table, q, k)
            warm.append((time.perf_counter() - t0) * 1000.0)
    return warm


def _directory_bytes(path: str) -> int:
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


def _lancedb_measured_cell(
    table: Any,
    tmp: str,
    corpus: ScaleCorpus,
    queries: Any,
    k: int,
    mask: Any,
    selectivity: float,
    ann: bool,
    build_ms: float,
    index_metadata: dict[str, Any],
) -> dict[str, Any]:
    truth = _truth_for_queries(corpus, queries, k, mask)
    cold, retrieved = _lancedb_cold_pass(table, queries, k)
    warm = _lancedb_warm_pass(table, queries, k)
    recall10, recall50 = _recall_pair(retrieved, truth)
    metrics = _ann_metrics(
        warm=warm,
        build_ms=build_ms,
        disk_bytes=int(_directory_bytes(tmp)),
        recall10=recall10,
        recall50=recall50,
    )
    adapter = "lancedb-ann" if ann else "lancedb-flat"
    return _adapter_cell(adapter, corpus, selectivity, metrics, cold, warm, index_metadata)


def _run_lancedb_cell(*, corpus, queries, k, mask, selectivity, ann: bool) -> dict[str, Any]:
    import lancedb

    active = set(_masked_ids(corpus, mask))
    build_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="scale-lance-") as tmp:
        table = _lancedb_table(lancedb.connect(tmp), corpus, active)
        try:
            index_metadata = _lancedb_index_metadata(table, len(corpus.chunk_ids), ann)
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
        return _lancedb_measured_cell(
            table, tmp, corpus, queries, k, mask, selectivity, ann, build_ms, index_metadata
        )


def _sqlite_vec_connection(corpus: ScaleCorpus) -> Any:
    import sqlite3

    import numpy as np
    import sqlite_vec

    dims = int(corpus.vectors.shape[1])
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
    return con


def _query_blob(q: Any) -> bytes:
    import numpy as np

    return np.ascontiguousarray(q, dtype=np.float32).tobytes()


def _sqlite_vec_rows(con: Any, qblob: bytes, count: int) -> list:
    return con.execute(
        "SELECT id FROM vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (qblob, count),
    ).fetchall()


def _chunk_ordinal(row_id: object) -> int:
    return int(str(row_id).rsplit("-", 1)[1])


def _sqlite_vec_cold_pass(
    con: Any, corpus: ScaleCorpus, queries: Any, active: list[int], k: int
) -> tuple[list[float], list[tuple[str, ...]]]:
    cold: list[float] = []
    retrieved: list[tuple[str, ...]] = []
    for q in queries:
        qblob = _query_blob(q)
        t0 = time.perf_counter()
        rows = _sqlite_vec_rows(con, qblob, len(corpus.chunk_ids))
        retrieved.append(tuple(str(row[0]) for row in rows if _chunk_ordinal(row[0]) in active)[:k])
        cold.append((time.perf_counter() - t0) * 1000.0)
    return cold, retrieved


def _sqlite_vec_warm_pass(
    con: Any, corpus: ScaleCorpus, queries: Any, active: list[int], k: int
) -> list[float]:
    warm: list[float] = []
    for q in queries:
        qblob = _query_blob(q)
        for _ in range(3):
            t0 = time.perf_counter()
            rows = _sqlite_vec_rows(con, qblob, len(corpus.chunk_ids))
            _ = tuple(row[0] for row in rows if _chunk_ordinal(row[0]) in active)[:k]
            warm.append((time.perf_counter() - t0) * 1000.0)
    return warm


def _run_sqlite_vec_cell(*, corpus, queries, k, mask, selectivity) -> dict[str, Any]:
    active = _masked_ids(corpus, mask)
    build_started = time.perf_counter()
    con = _sqlite_vec_connection(corpus)
    build_ms = (time.perf_counter() - build_started) * 1000.0
    truth = _truth_for_queries(corpus, queries, k, mask)
    cold, retrieved = _sqlite_vec_cold_pass(con, corpus, queries, active, k)
    warm = _sqlite_vec_warm_pass(con, corpus, queries, active, k)
    con.close()
    recall10, recall50 = _recall_pair(retrieved, truth)
    metrics = _ann_metrics(
        warm=warm, build_ms=build_ms, disk_bytes=None, recall10=recall10, recall50=recall50
    )
    return _adapter_cell(
        "sqlite-vec",
        corpus,
        selectivity,
        metrics,
        cold,
        warm,
        {
            "requested": "flat",
            "status": "flat",
            "type": "sqlite-vec-vec0",
            "verified_by": "virtual_table_creation",
            "reason": None,
        },
    )


_FSYNC_UNSUPPORTED_ERRNOS = frozenset({errno.EACCES, errno.EINVAL, errno.ENOTSUP, errno.EPERM})


def _unsupported_fsync(exc: OSError, extra: frozenset[int] = frozenset()) -> tuple[bool, str] | None:
    """The (False, reason) outcome for an errno that means "not supported here"."""
    if exc.errno in _FSYNC_UNSUPPORTED_ERRNOS or exc.errno in extra:
        return False, f"directory_fsync_unsupported:{exc.errno}"
    return None


def _fsync_descriptor(descriptor: int) -> tuple[bool, str | None]:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        unsupported = _unsupported_fsync(exc, frozenset({errno.EBADF}))
        if unsupported is None:
            raise
        return unsupported
    return True, None


def _fsync_directory(path: Path) -> tuple[bool, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        unsupported = _unsupported_fsync(exc)
        if unsupported is None:
            raise
        return unsupported
    try:
        return _fsync_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _exit_unless_synced(synced: bool, reason: str | None, fallback: str, code: int) -> None:
    if synced:
        return
    sys.stderr.write(reason or fallback)
    sys.stderr.flush()
    os._exit(code)


def _write_crash_payload(payload: Path, point: str) -> None:
    with payload.open("wb") as handle:
        handle.write(b"scale-matrix-crash-payload")
        handle.flush()
        if point == "before_fsync":
            os._exit(91)
        os.fsync(handle.fileno())


def _crash_worker(point: str, root: Path) -> None:
    staging = root / "staging"
    active = root / "active"
    staging.mkdir()
    _write_crash_payload(staging / "vectors.bin", point)
    staging_synced, reason = _fsync_directory(staging)
    _exit_unless_synced(staging_synced, reason, "staging_directory_fsync_unavailable", 81)
    if point == "before_activation":
        os._exit(92)
    os.replace(staging, active)
    root_synced, reason = _fsync_directory(root)
    _exit_unless_synced(root_synced, reason, "activation_directory_fsync_unavailable", 82)
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


def _windows_volume_name(path: Path) -> str | None:
    try:
        import ctypes

        name = ctypes.create_unicode_buffer(64)
        root = Path(path.anchor or path.resolve().anchor)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            str(root), None, 0, None, None, None, name, len(name)
        )
    except (AttributeError, OSError):
        return None
    if ok and name.value:
        return name.value
    return None


def _filesystem_description(path: Path) -> str:
    if os.name == "nt":
        name = _windows_volume_name(path)
        if name is not None:
            return name
    stats = os.statvfs(path) if hasattr(os, "statvfs") else None
    detail = f"block_size={stats.f_bsize}" if stats is not None else "type_unavailable"
    return f"local_temp:{detail}"


_EXPECTED_CRASH_RETURNCODES = {
    "before_fsync": 91,
    "before_activation": 92,
    "after_activation": 93,
}


def _crash_base(root: Path) -> dict[str, Any]:
    return {
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


def _run_crash_worker(point: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--crash-worker", point, str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _note_fsync_outcome(base: dict[str, Any], point: str, returncode: int) -> None:
    if returncode == _EXPECTED_CRASH_RETURNCODES[point]:
        reached = "not_reached" if point == "before_fsync" else "performed"
        base["file_fsync"] = reached
        base["directory_fsync"] = reached
        return
    if returncode in {81, 82}:
        base["file_fsync"] = "performed"
        base["directory_fsync"] = "unavailable"


def _active_is_valid(active: Path, payload: Path, expected_active: bool) -> bool:
    if expected_active and payload.is_file():
        return active.is_dir() and payload.read_bytes() == b"scale-matrix-crash-payload"
    return not active.exists()


def _recover_staging(base: dict[str, Any], root: Path, staging: Path) -> str | None:
    """Remove a leftover staging directory; the reason when the root sync was unavailable."""
    if not staging.exists():
        return None
    import shutil

    shutil.rmtree(staging)
    recovered_synced, recovery_reason = _fsync_directory(root)
    base["recovery_directory_fsync"] = "performed" if recovered_synced else "unavailable"
    return recovery_reason


def _fsyncs_available(base: dict[str, Any]) -> bool:
    return base["directory_fsync"] != "unavailable" and base["recovery_directory_fsync"] != "unavailable"


def _crash_status(base: dict[str, Any], recovered: bool, returncode: int) -> str:
    if recovered and _fsyncs_available(base):
        return "passed"
    if recovered or returncode in {81, 82}:
        return "unavailable"
    return "failed"


def _crash_detail(recovery_reason: str | None, stderr: str) -> str:
    return recovery_reason or stderr.strip() or "subprocess_terminated_and_recovered"


def _inspect_crash_outcome(
    base: dict[str, Any], point: str, root: Path, result: subprocess.CompletedProcess
) -> None:
    base["injected"] = result.returncode in {91, 92, 93}
    base["worker_returncode"] = result.returncode
    _note_fsync_outcome(base, point, result.returncode)
    active = root / "active"
    payload = active / "vectors.bin"
    active_valid = _active_is_valid(active, payload, point == "after_activation")
    partial = active.exists() and not active_valid
    recovery_reason = _recover_staging(base, root, root / "staging")
    recovered = bool(base["injected"] and active_valid and not partial)
    base["recovered_cleanly"] = recovered
    base["partial_activation"] = partial
    base["status"] = _crash_status(base, recovered, result.returncode)
    base["detail"] = _crash_detail(recovery_reason, result.stderr)


def _crash_point_outcome(point: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="scale-crash-") as tmp:
        root = Path(tmp)
        base = _crash_base(root)
        try:
            _inspect_crash_outcome(base, point, root, _run_crash_worker(point, root))
        except Exception as exc:
            base["detail"] = f"{type(exc).__name__}: {exc}"
        return base


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
        outcomes[point] = _crash_point_outcome(point)
    return outcomes


def _valid_sizes(sizes: Sequence[int]) -> bool:
    return bool(sizes) and all(_is_positive_int(size) for size in sizes)


def _valid_adapters(adapters: Sequence[str]) -> bool:
    return bool(adapters) and all(adapter in ADAPTER_IDS for adapter in adapters)


def _require_plan_inputs(
    corpus_sizes: Sequence[int], adapters: Sequence[str], selectivity: Sequence[float]
) -> None:
    if not _valid_sizes(corpus_sizes):
        raise ValueError("corpus_sizes must be non-empty and positive")
    if not _valid_adapters(adapters):
        raise ValueError("adapters must be non-empty and known")
    if not selectivity:
        raise ValueError("selectivity must be non-empty and in (0, 1]")
    for fraction in selectivity:
        _require_fraction("selectivity", fraction)


def _execution(parallel: bool, heavy: bool) -> str:
    if parallel and not heavy:
        return "parallel"
    return "serial"


def _plan(
    corpus_sizes: Sequence[int],
    adapters: Sequence[str],
    selectivity: Sequence[float],
    *,
    parallel_allowed: bool,
    execution: str,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "parallel_allowed": parallel_allowed,
        "execution": execution,
        "reason": reason,
        "corpus_sizes": list(corpus_sizes),
        "adapters": list(adapters),
        "selectivity": list(selectivity),
    }


def plan_matrix(
    *,
    corpus_sizes: Sequence[int],
    adapters: Sequence[str],
    selectivity: Sequence[float],
    parallel: bool = False,
) -> dict[str, Any]:
    """Heavy runs are always serial regardless of requested parallelism."""
    _require_plan_inputs(corpus_sizes, adapters, selectivity)
    heavy = any(size >= 1000 for size in corpus_sizes)
    if heavy and parallel:
        return _plan(
            corpus_sizes,
            adapters,
            selectivity,
            parallel_allowed=False,
            execution="serial",
            reason="heavy_runs_must_be_serial",
        )
    return _plan(
        corpus_sizes,
        adapters,
        selectivity,
        parallel_allowed=bool(parallel) and not heavy,
        execution=_execution(parallel, heavy),
        reason=None,
    )


def _exact_p95_of(exact_cell: dict[str, Any]) -> float:
    """The warm p95 the ANN cells are measured against, from the exact cell."""
    measured = exact_cell.get("_exact_p95_ms")
    if measured:
        return float(measured)
    return float(exact_cell["latency_profiles"]["warm"]["p95_ms"] or 0.0)


def run_smoke(
    *,
    corpus_size: int = 32,
    dimensions: int = 8,
    seed: int = 0,
    queries: int = 4,
    adapters: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Deterministic offline smoke matrix (exact + optional adapters)."""
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
    exact_p95 = _exact_p95_of(exact_cell)
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
    return report


def _require_report_inputs(
    mode: str, executed_corpus_sizes: Sequence[int], dimensions: int, queries: int
) -> None:
    if mode not in {"smoke", "full"}:
        raise ValueError("mode must be smoke or full")
    _require_positive_int("dimensions", dimensions)
    _require_positive_int("queries", queries)
    if not _valid_sizes(executed_corpus_sizes):
        raise ValueError("executed_corpus_sizes must be non-empty and positive")


def _measurement_status(cells: Sequence[dict[str, Any]]) -> str:
    if cells:
        return "measured"
    return "unavailable"


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
    _require_report_inputs(mode, executed_corpus_sizes, dimensions, queries)
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
            "measurement_status": _measurement_status(cells),
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


def _json_children(value: Any, path: str) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [(f"{path}.{key}", child) for key, child in value.items()]
    if isinstance(value, list):
        return [(f"{path}[{index}]", child) for index, child in enumerate(value)]
    return []


def _assert_finite_json(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"nonfinite report value at {path}")
    for child_path, child in _json_children(value, path):
        _assert_finite_json(child, child_path)


def _measured_consistent(metric_provenance: dict[str, Any]) -> bool:
    return bool(metric_provenance.get("source")) and metric_provenance.get("reason") is None


def _unavailable_consistent(metric_provenance: dict[str, Any]) -> bool:
    return metric_provenance.get("source") is None and bool(metric_provenance.get("reason"))


def _provenance_consistent(value: Any, metric_provenance: dict[str, Any]) -> bool:
    status = metric_provenance.get("status")
    if (value is None) != (status == "unavailable"):
        return False
    if status == "measured":
        return _measured_consistent(metric_provenance)
    if status == "unavailable":
        return _unavailable_consistent(metric_provenance)
    return True


def _validate_cell_provenance(cell_index: int, cell: dict[str, Any]) -> None:
    metrics = cell.get("metrics", {})
    provenance = cell.get("metric_provenance", {})
    for metric in METRIC_KEYS:
        if not _provenance_consistent(metrics.get(metric), provenance.get(metric, {})):
            raise ValueError(f"metric provenance mismatch at cells[{cell_index}].{metric}")


def _validate_metric_provenance(report: dict[str, Any]) -> None:
    for cell_index, cell in enumerate(report.get("cells", [])):
        _validate_cell_provenance(cell_index, cell)


def _observed_cells(cells: Sequence[dict[str, Any]]) -> list[tuple[Any, Any, Any]]:
    return [(cell.get("corpus_size"), cell.get("selectivity"), cell.get("adapter")) for cell in cells]


def _require_full_matrix(observed: list[tuple[Any, Any, Any]]) -> None:
    expected = [
        (size, fraction, adapter)
        for size in CORPUS_SIZES
        for fraction in SELECTIVITY_FRACTIONS
        for adapter in ADAPTER_IDS
    ]
    if sorted(observed, key=str) != sorted(expected, key=str):
        raise ValueError("full matrix cells must contain each declared combination exactly once")


def _one_cell_per_adapter(smoke_adapters: list[Any]) -> bool:
    return len(smoke_adapters) == len(set(smoke_adapters)) and smoke_adapters.count("exact-numpy") == 1


def _all_at_full_selectivity(observed: list[tuple[Any, Any, Any]], size: Any) -> bool:
    return not any(cell_size != size or fraction != 1.0 for cell_size, fraction, _ in observed)


def _smoke_cells_are_valid(observed: list[tuple[Any, Any, Any]], executed_sizes: list[Any]) -> bool:
    if len(executed_sizes) != 1:
        return False
    if not _one_cell_per_adapter([adapter for _size, _fraction, adapter in observed]):
        return False
    return _all_at_full_selectivity(observed, executed_sizes[0])


def _require_smoke_matrix(observed: list[tuple[Any, Any, Any]], report: dict[str, Any]) -> None:
    if not _smoke_cells_are_valid(observed, report.get("executed_corpus_sizes", [])):
        raise ValueError("smoke matrix cells must contain one cell per adapter at full selectivity")


def _require_selected_size(cell_index: int, cell: dict[str, Any], methodology: dict[str, Any]) -> None:
    size = cell.get("corpus_size")
    fraction = cell.get("selectivity")
    if not isinstance(size, int) or not isinstance(fraction, (int, float)):
        return
    if methodology.get("selected_corpus_size") != max(1, round(size * fraction)):
        raise ValueError(f"filter methodology mismatch at cells[{cell_index}]")


def _require_cell_methodology(cell_index: int, cell: dict[str, Any]) -> None:
    methodology = cell.get("filter_methodology", {})
    if methodology.get("indexed_corpus_size") != cell.get("corpus_size"):
        raise ValueError(f"filter methodology mismatch at cells[{cell_index}]")
    if methodology.get("implementation") != _filter_implementation(cell.get("adapter")):
        raise ValueError(f"filter methodology mismatch at cells[{cell_index}]")
    _require_selected_size(cell_index, cell, methodology)


def _validate_report_semantics(report: dict[str, Any], *, mode: str) -> None:
    cells = report.get("cells", [])
    observed = _observed_cells(cells)
    if mode == "full":
        _require_full_matrix(observed)
    else:
        _require_smoke_matrix(observed, report)
    for cell_index, cell in enumerate(cells):
        _require_cell_methodology(cell_index, cell)


def _report_schema(mode: str) -> Any:
    if mode == "smoke":
        return SMOKE_REPORT_SCHEMA
    if mode == "full":
        return FULL_REPORT_SCHEMA
    return None


def write_report_atomic(report: dict[str, Any], output: Path, *, mode: str) -> None:
    from reliable_memory import validate_schema

    schema = _report_schema(mode)
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


def _crash_worker_entry(raw_argv: list[str]) -> int | None:
    """Exit code of the crash-worker mode, or None when this is not that mode."""
    if not raw_argv or raw_argv[0] != "--crash-worker":
        return None
    if len(raw_argv) != 3 or raw_argv[1] not in CRASH_POINTS:
        return 2
    _crash_worker(raw_argv[1], Path(raw_argv[2]))
    return 1


def _build_parser() -> argparse.ArgumentParser:
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
    return parser


def _require_argument_bounds(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name in ("corpus_size", "dimensions", "queries"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be >= 1")
    if args.full and args.queries > min(CORPUS_SIZES):
        parser.error(f"--queries must be <= {min(CORPUS_SIZES)} for --full")


def _exact_p95_after(adapter_id: str, cell: dict[str, Any], exact_p95: float | None) -> float | None:
    """The exact cell hands its warm p95 to the ANN cells that follow it."""
    if adapter_id != "exact-numpy":
        return exact_p95
    value = _exact_p95_of(cell)
    cell.pop("_exact_p95_ms", None)
    return value


def _size_cells(size: int, args: argparse.Namespace) -> list[dict[str, Any]]:
    corpus = generate_corpus(n_chunks=size, dimensions=args.dimensions, seed=args.seed)
    queries = corpus.vectors[: max(1, min(args.queries, size))]
    cells: list[dict[str, Any]] = []
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
            exact_p95 = _exact_p95_after(adapter_id, cell, exact_p95)
            cells.append(cell)
    return cells


def _full_report(args: argparse.Namespace) -> dict[str, Any]:
    # Serial heavy path — never parallel.
    plan = plan_matrix(
        corpus_sizes=CORPUS_SIZES,
        adapters=ADAPTER_IDS,
        selectivity=SELECTIVITY_FRACTIONS,
        parallel=True,
    )
    assert plan["execution"] == "serial"
    cells = [cell for size in CORPUS_SIZES for cell in _size_cells(size, args)]
    return build_report(
        mode="full",
        executed_corpus_sizes=CORPUS_SIZES,
        dimensions=args.dimensions,
        queries=args.queries,
        seed=args.seed,
        cells=cells,
        crash_matrix=run_crash_matrix(seed=args.seed),
    )


def _emit_report(report: dict[str, Any], args: argparse.Namespace) -> None:
    _assert_finite_json(report)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        write_report_atomic(report, args.output, mode=report["mode"])
    if args.json or args.output is None:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    worker_exit = _crash_worker_entry(raw_argv)
    if worker_exit is not None:
        return worker_exit
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    _require_argument_bounds(parser, args)
    if args.full:
        report = _full_report(args)
    else:
        report = run_smoke(
            corpus_size=args.corpus_size,
            dimensions=args.dimensions,
            seed=args.seed,
            queries=args.queries,
        )
    _emit_report(report, args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
