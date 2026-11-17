"""Worker process trees, peak RSS, resource measurements and the orchestration gates computed
from them."""
from __future__ import annotations

import contextlib
import ctypes
import math
import os
import signal
import statistics
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from retrieval_v2_contract import THRESHOLDS


def _worker_popen_options(env: dict[str, str] | None) -> dict:
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        options["start_new_session"] = True
    return options


def _kill_worker_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    else:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.kill()
        process.communicate(timeout=5)


def run_process_tree(
    command: Sequence[str], *, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("worker deadline must be positive and finite")
    process = subprocess.Popen(list(command), **_worker_popen_options(env))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_worker_tree(process)
        raise TimeoutError("real benchmark worker deadline exceeded") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _process_memory_counters_type(wintypes):
    class ProcessMemoryCounters(ctypes.Structure):
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

    return ProcessMemoryCounters


def _windows_peak_rss() -> tuple[int | None, str]:
    try:
        from ctypes import wintypes

        counters = _process_memory_counters_type(wintypes)()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if ok:
            return int(counters.PeakWorkingSetSize), "measured-windows-working-set"
    except (AttributeError, OSError, ValueError):
        pass
    return None, "unavailable"


def _posix_peak_rss() -> tuple[int | None, str]:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        multiplier = 1 if sys.platform == "darwin" else 1024
        return int(usage * multiplier), "measured-posix-ru-maxrss"
    except (ImportError, OSError, ValueError):
        return None, "unavailable"


def process_peak_rss() -> tuple[int | None, str]:
    if os.name == "nt":
        return _windows_peak_rss()
    return _posix_peak_rss()


_MEASURED_RESOURCE_FIELDS = (
    "latency_p50_ms",
    "latency_p95_ms",
    "cold_first_query_latency_ms",
    "warm_latency_p50_ms",
    "warm_latency_p95_ms",
    "build_time_ms",
    "indexing_throughput_chunks_per_second",
    "peak_rss_bytes",
    "index_size_bytes",
)


def _nearest_rank_p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return values[max(0, math.ceil(0.95 * len(values)) - 1)]


def _median_or_none(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def _first_or_none(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return values[0]


def _indexing_throughput(chunk_count: int, indexing_duration_ms: float) -> float | None:
    if chunk_count > 0 and indexing_duration_ms > 0:
        return chunk_count / (indexing_duration_ms / 1000.0)
    return None


def _measurement_status(value: object) -> str:
    if value is not None:
        return "measured"
    return "unavailable"


def resource_measurements(
    latencies_ms: Sequence[float],
    *,
    build_time_ms: float,
    chunk_count: int,
    index_size_bytes: int,
    peak_rss_bytes: int | None,
    peak_rss_status: str,
    indexing_duration_ms: float | None = None,
) -> dict:
    sorted_latencies = sorted(latencies_ms)
    warm_latencies = sorted(latencies_ms[1:])
    if indexing_duration_ms is None:
        indexing_duration_ms = build_time_ms
    measurements = {
        "latency_p50_ms": _median_or_none(sorted_latencies),
        "latency_p95_ms": _nearest_rank_p95(sorted_latencies),
        "cold_first_query_latency_ms": _first_or_none(latencies_ms),
        "warm_latency_p50_ms": _median_or_none(warm_latencies),
        "warm_latency_p95_ms": _nearest_rank_p95(warm_latencies),
        "peak_rss_bytes": peak_rss_bytes,
        "peak_rss_status": peak_rss_status,
        "build_time_ms": build_time_ms,
        "indexing_throughput_chunks_per_second": _indexing_throughput(chunk_count, indexing_duration_ms),
        "index_size_bytes": index_size_bytes,
    }
    measurements["measurement_status"] = {
        name: _measurement_status(measurements[name]) for name in _MEASURED_RESOURCE_FIELDS
    }
    return measurements


RETRIEVAL_GATE_METRICS = ("parent_recall_at_10", "all_required_evidence_recall_at_20", "ndcg_at_10", "mrr_at_10")


def _false_answer_gate(value: float | None) -> bool:
    return value is not None and value <= THRESHOLDS["no_answer_false_answer_rate"]


def _language_passes(metrics: dict) -> bool:
    gap = THRESHOLDS["max_language_gate_gap"]
    retrieval_passes = all(metrics[name] >= THRESHOLDS[name] - gap for name in RETRIEVAL_GATE_METRICS)
    return retrieval_passes and _false_answer_gate(metrics["no_answer_false_answer_rate"])


def _orchestration_passed(metric_gates: dict, language_results: dict, qa_contract_passed: bool) -> bool:
    return all(metric_gates.values()) and all(language_results.values()) and qa_contract_passed


def gate_results(overall: dict, slices: dict[str, dict], *, qa_contract_passed: bool = True) -> dict:
    metric_gates = {name: overall[name] >= THRESHOLDS[name] for name in RETRIEVAL_GATE_METRICS}
    metric_gates["no_answer_false_answer_rate"] = _false_answer_gate(overall["no_answer_false_answer_rate"])
    language_results = {language: _language_passes(slices[language]) for language in ("EN", "RU", "ZH")}
    return {
        "release_evidence": False,
        "interpretation": "orchestration-only",
        "metric_results": metric_gates,
        "language_results": language_results,
        "qa_contract_passed": qa_contract_passed,
        "passed_for_orchestration": _orchestration_passed(metric_gates, language_results, qa_contract_passed),
    }
