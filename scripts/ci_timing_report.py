#!/usr/bin/env python3
"""Compile retained CI timeout evidence from GitHub job and JUnit data."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reliable_memory import validate_schema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "benchmark" / "ci-timeout-evidence-v1.schema.json"
HEAD_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
JOB_NAME_PATTERN = re.compile(r"^timing::([a-z_]+)::([^:]+)$")
TIMEOUT_CEILINGS = {
    "focused": 900,
    "clean": 1200,
    "installer": 1200,
    "linux_full": 2700,
    "windows_full": 3600,
    "macos_full": 2700,
}
FULL_CLASSES = frozenset({"linux_full", "windows_full", "macos_full"})


def nearest_rank_p95(values: list[float]) -> float:
    """Return the inclusive nearest-rank p95 for finite nonnegative samples."""
    if not values:
        raise ValueError("at least one timing sample is required")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("timing samples must be finite nonnegative numbers")
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _utc_timestamp(value: object, label: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a UTC timestamp")
    parsed = _parsed_utc(value, label).astimezone(timezone.utc)
    return parsed, parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parsed_utc(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_with_numeric_offset(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must be a UTC timestamp")
    return parsed


def _with_numeric_offset(value: str) -> str:
    if value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


def _finite_nonnegative(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite nonnegative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite nonnegative number")
    return number


def _load_run(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read run JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"run JSON root must be an object: {path}")
    return payload


def _parse_job(
    raw: object,
    *,
    workflow_run_id: int,
    run_attempt: int,
) -> tuple[dict[str, Any], str, str]:
    if not isinstance(raw, dict):
        raise ValueError("workflow jobs must be objects")
    job_id = _positive_integer(raw.get("databaseId"), "job ID")
    name, timeout_class, identity = _job_identity(raw.get("name"))
    if raw.get("status") != "completed" or raw.get("conclusion") != "success":
        raise ValueError(f"every timing job must be completed and successful: {name}")
    started_text, completed_text, runtime = _job_window(raw, name)
    return (
        {
            "workflow_run_id": workflow_run_id,
            "run_attempt": run_attempt,
            "job_id": job_id,
            "timeout_class": timeout_class,
            "name": name,
            "conclusion": "success",
            "started_at": started_text,
            "completed_at": completed_text,
            "runtime_seconds": runtime,
        },
        timeout_class,
        identity,
    )


def _job_identity(name: object) -> tuple[str, str, str]:
    """(name, timeout class, identity) of a `timing::<class>::<identity>` job name."""
    if not isinstance(name, str):
        raise ValueError("job name must use the timing::<class>::<identity> format")
    matched = JOB_NAME_PATTERN.fullmatch(name)
    if matched is None:
        raise ValueError(f"job name has an unknown timing prefix: {name}")
    timeout_class, identity = matched.groups()
    _require_known_class(timeout_class)
    return name, timeout_class, identity


def _require_known_class(timeout_class: str) -> None:
    if timeout_class not in TIMEOUT_CEILINGS:
        raise ValueError(f"job name uses an unknown timeout class: {timeout_class}")


def _job_window(raw: dict, name: str) -> tuple[str, str, float]:
    started, started_text = _utc_timestamp(raw.get("startedAt"), "job start")
    completed, completed_text = _utc_timestamp(raw.get("completedAt"), "job completion")
    runtime = (completed - started).total_seconds()
    if not math.isfinite(runtime) or runtime < 0:
        raise ValueError(f"job completion precedes start: {name}")
    return started_text, completed_text, runtime


def _suite_seconds(root: ET.Element) -> float:
    suites = [root] if root.tag == "testsuite" else list(root.findall("./testsuite"))
    if not suites:
        raise ValueError("JUnit artifact must contain a testsuite")
    return sum(_finite_nonnegative(suite.get("time"), "JUnit suite time") for suite in suites)


def _junit_records(
    artifact_root: Path,
    *,
    workflow_run_id: int,
    run_attempt: int,
    job_id: int,
    artifact_name: str,
    job_runtime: float,
) -> list[dict[str, Any]]:
    root = _junit_root(artifact_root, artifact_name)
    if _suite_seconds(root) > job_runtime:
        raise ValueError(f"JUnit suite time exceeds job runtime: {artifact_name}")
    base = {
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
        "job_id": job_id,
        "artifact_name": artifact_name,
    }
    records = _testcase_records(root, artifact_name, base)
    if not records:
        raise ValueError(f"JUnit artifact contains no testcase records: {artifact_name}")
    return records


def _junit_root(artifact_root: Path, artifact_name: str) -> ET.Element:
    xml_files = sorted(artifact_root.rglob("*.xml")) if artifact_root.is_dir() else []
    if len(xml_files) != 1:
        raise ValueError(f"full job requires exactly one JUnit artifact XML: {artifact_name}")
    try:
        return ET.parse(xml_files[0]).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"invalid JUnit artifact: {artifact_name}") from exc


def _testcase_records(root: ET.Element, artifact_name: str, base: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    node_ids: set[str] = set()
    for case in root.iter("testcase"):
        node_id = _testcase_node_id(case, artifact_name)
        if node_id in node_ids:
            raise ValueError(f"JUnit artifact contains a duplicate node ID: {node_id}")
        node_ids.add(node_id)
        duration = _finite_nonnegative(case.get("time"), "JUnit testcase time")
        records.append({**base, "node_id": node_id, "duration_seconds": duration})
    return records


def _testcase_node_id(case: ET.Element, artifact_name: str) -> str:
    classname = case.get("classname", "").strip()
    name = case.get("name", "").strip()
    if not name:
        raise ValueError(f"JUnit testcase has no name: {artifact_name}")
    return f"{classname}::{name}" if classname else name


def compile_report(
    run_jsons: Sequence[Path],
    junit_roots: Sequence[Path],
    *,
    head_sha: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Compile five exact-head workflow attempts into one validated report."""
    _require_five_pairs(run_jsons, junit_roots)
    if HEAD_SHA_PATTERN.fullmatch(head_sha) is None:
        raise ValueError("head SHA must be lowercase full 40-hex")
    evidence = _TimingEvidence(head_sha)
    for run_path, junit_root in zip(run_jsons, junit_roots, strict=True):
        evidence.add_attempt(_load_run(Path(run_path)), Path(junit_root))
    evidence.require_one_complete_run()
    report = evidence.report(generated_at)
    validate_schema(report, SCHEMA_PATH)
    return report


def _require_five_pairs(run_jsons: Sequence[Path], junit_roots: Sequence[Path]) -> None:
    if len(run_jsons) != len(junit_roots):
        raise ValueError("run JSON and JUnit roots must be paired")
    if len(run_jsons) != 5:
        raise ValueError("exactly five workflow attempts are required")


def _require_head_and_success(payload: dict[str, Any], head_sha: str) -> None:
    if payload.get("headSha") != head_sha:
        raise ValueError("every workflow attempt must match the requested head SHA")
    if payload.get("conclusion") != "success":
        raise ValueError("every workflow attempt must be successful")


def _generated_at(generated_at: datetime | None) -> datetime:
    generated = generated_at or datetime.now(timezone.utc).replace(microsecond=0)
    if generated.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    return generated


class _TimingEvidence:
    """Every attempt, job, sample and testcase of one workflow run, checked as it arrives."""

    def __init__(self, head_sha: str) -> None:
        self.head_sha = head_sha
        self.workflow_attempts: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.tests: list[dict[str, Any]] = []
        self.class_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.run_ids: set[int] = set()
        self.attempts: set[int] = set()
        self.job_ids: set[int] = set()
        self.artifact_names: set[str] = set()

    def add_attempt(self, payload: dict[str, Any], junit_root: Path) -> None:
        run_id = _positive_integer(payload.get("databaseId"), "workflow run ID")
        attempt = _positive_integer(payload.get("attempt"), "workflow run attempt")
        self._claim_attempt(attempt)
        self.run_ids.add(run_id)
        raw_jobs = self._timing_jobs(payload)
        self.workflow_attempts.append(
            {
                "workflow_run_id": run_id,
                "run_attempt": attempt,
                "head_sha": self.head_sha,
                "conclusion": "success",
            }
        )
        for raw_job in raw_jobs:
            self._add_job(raw_job, run_id, attempt, junit_root)

    def _claim_attempt(self, attempt: int) -> None:
        if attempt not in {1, 2, 3, 4, 5}:
            raise ValueError("workflow attempts must be contiguous from 1 through 5")
        if attempt in self.attempts:
            raise ValueError(f"duplicate workflow run attempt: {attempt}")
        self.attempts.add(attempt)

    def _timing_jobs(self, payload: dict[str, Any]) -> list:
        _require_head_and_success(payload, self.head_sha)
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValueError("every workflow attempt must contain timing jobs")
        return raw_jobs

    def _add_job(self, raw_job: object, run_id: int, attempt: int, junit_root: Path) -> None:
        job, timeout_class, identity = _parse_job(
            raw_job,
            workflow_run_id=run_id,
            run_attempt=attempt,
        )
        self._claim_job_id(job["job_id"])
        self.jobs.append(job)
        self.class_samples[timeout_class].append(
            {
                "workflow_run_id": run_id,
                "run_attempt": attempt,
                "job_id": job["job_id"],
                "runtime_seconds": job["runtime_seconds"],
            }
        )
        if timeout_class in FULL_CLASSES:
            self._add_junit(job, timeout_class, identity, junit_root)

    def _claim_job_id(self, job_id: int) -> None:
        if job_id in self.job_ids:
            raise ValueError(f"duplicate job ID: {job_id}")
        self.job_ids.add(job_id)

    def _add_junit(self, job: dict[str, Any], timeout_class: str, identity: str, junit_root: Path) -> None:
        artifact_name = f"pytest-timings-{timeout_class}-{identity}-attempt-{job['run_attempt']}"
        if artifact_name in self.artifact_names:
            raise ValueError(f"duplicate JUnit artifact identity: {artifact_name}")
        self.artifact_names.add(artifact_name)
        self.tests.extend(
            _junit_records(
                junit_root / artifact_name,
                workflow_run_id=job["workflow_run_id"],
                run_attempt=job["run_attempt"],
                job_id=job["job_id"],
                artifact_name=artifact_name,
                job_runtime=job["runtime_seconds"],
            )
        )

    def require_one_complete_run(self) -> None:
        if len(self.run_ids) != 1:
            raise ValueError("timing evidence must come from exactly one workflow run")
        if self.attempts != {1, 2, 3, 4, 5}:
            raise ValueError("workflow attempts must be contiguous from 1 through 5")

    def _class_summary(self, timeout_class: str, ceiling: float) -> dict[str, Any]:
        samples = sorted(
            self.class_samples.get(timeout_class, []),
            key=lambda sample: (sample["run_attempt"], sample["job_id"]),
        )
        if len(samples) < 5:
            raise ValueError(f"{timeout_class} requires at least five timing samples")
        p95 = nearest_rank_p95([sample["runtime_seconds"] for sample in samples])
        if p95 > ceiling * 0.8:
            raise ValueError(f"{timeout_class} p95 lacks 20 percent timeout headroom")
        return {"ceiling_seconds": ceiling, "p95_seconds": p95, "samples": samples}

    def report(self, generated_at: datetime | None) -> dict[str, Any]:
        classes = {
            timeout_class: self._class_summary(timeout_class, ceiling)
            for timeout_class, ceiling in TIMEOUT_CEILINGS.items()
        }
        generated = _generated_at(generated_at)
        return {
            "schema_version": "ci-timeout-evidence/v1",
            "head_sha": self.head_sha,
            "generated_at": generated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "workflow_attempts": sorted(self.workflow_attempts, key=lambda item: item["run_attempt"]),
            "jobs": sorted(self.jobs, key=lambda item: (item["run_attempt"], item["job_id"])),
            "classes": classes,
            "tests": sorted(
                self.tests,
                key=lambda item: (item["run_attempt"], item["job_id"], item["node_id"]),
            ),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", action="append", type=Path, required=True)
    parser.add_argument("--junit-root", action="append", type=Path, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = compile_report(
        args.run_json,
        args.junit_root,
        head_sha=args.head_sha,
    )
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
