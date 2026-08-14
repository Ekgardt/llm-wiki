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
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must be a UTC timestamp")
    parsed = parsed.astimezone(timezone.utc)
    return parsed, parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


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
    name = raw.get("name")
    if not isinstance(name, str):
        raise ValueError("job name must use the timing::<class>::<identity> format")
    matched = JOB_NAME_PATTERN.fullmatch(name)
    if matched is None:
        raise ValueError(f"job name has an unknown timing prefix: {name}")
    timeout_class, identity = matched.groups()
    if timeout_class not in TIMEOUT_CEILINGS:
        raise ValueError(f"job name uses an unknown timeout class: {timeout_class}")
    if raw.get("status") != "completed" or raw.get("conclusion") != "success":
        raise ValueError(f"every timing job must be completed and successful: {name}")
    started, started_text = _utc_timestamp(raw.get("startedAt"), "job start")
    completed, completed_text = _utc_timestamp(raw.get("completedAt"), "job completion")
    runtime = (completed - started).total_seconds()
    if not math.isfinite(runtime) or runtime < 0:
        raise ValueError(f"job completion precedes start: {name}")
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
    xml_files = sorted(artifact_root.rglob("*.xml")) if artifact_root.is_dir() else []
    if len(xml_files) != 1:
        raise ValueError(f"full job requires exactly one JUnit artifact XML: {artifact_name}")
    try:
        root = ET.parse(xml_files[0]).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"invalid JUnit artifact: {artifact_name}") from exc
    if _suite_seconds(root) > job_runtime:
        raise ValueError(f"JUnit suite time exceeds job runtime: {artifact_name}")

    records = []
    node_ids: set[str] = set()
    for case in root.iter("testcase"):
        classname = case.get("classname", "").strip()
        name = case.get("name", "").strip()
        if not name:
            raise ValueError(f"JUnit testcase has no name: {artifact_name}")
        node_id = f"{classname}::{name}" if classname else name
        if node_id in node_ids:
            raise ValueError(f"JUnit artifact contains a duplicate node ID: {node_id}")
        node_ids.add(node_id)
        duration = _finite_nonnegative(case.get("time"), "JUnit testcase time")
        records.append(
            {
                "workflow_run_id": workflow_run_id,
                "run_attempt": run_attempt,
                "job_id": job_id,
                "artifact_name": artifact_name,
                "node_id": node_id,
                "duration_seconds": duration,
            }
        )
    if not records:
        raise ValueError(f"JUnit artifact contains no testcase records: {artifact_name}")
    return records


def compile_report(
    run_jsons: Sequence[Path],
    junit_roots: Sequence[Path],
    *,
    head_sha: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Compile five exact-head workflow attempts into one validated report."""
    if len(run_jsons) != len(junit_roots):
        raise ValueError("run JSON and JUnit roots must be paired")
    if len(run_jsons) != 5:
        raise ValueError("exactly five workflow attempts are required")
    if HEAD_SHA_PATTERN.fullmatch(head_sha) is None:
        raise ValueError("head SHA must be lowercase full 40-hex")

    workflow_attempts = []
    jobs = []
    tests = []
    class_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    run_ids: set[int] = set()
    attempts: set[int] = set()
    job_ids: set[int] = set()
    artifact_names: set[str] = set()

    for run_path, junit_root in zip(run_jsons, junit_roots, strict=True):
        payload = _load_run(Path(run_path))
        run_id = _positive_integer(payload.get("databaseId"), "workflow run ID")
        attempt = _positive_integer(payload.get("attempt"), "workflow run attempt")
        if attempt not in {1, 2, 3, 4, 5}:
            raise ValueError("workflow attempts must be contiguous from 1 through 5")
        if attempt in attempts:
            raise ValueError(f"duplicate workflow run attempt: {attempt}")
        attempts.add(attempt)
        run_ids.add(run_id)
        if payload.get("headSha") != head_sha:
            raise ValueError("every workflow attempt must match the requested head SHA")
        if payload.get("conclusion") != "success":
            raise ValueError("every workflow attempt must be successful")
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValueError("every workflow attempt must contain timing jobs")
        workflow_attempts.append(
            {
                "workflow_run_id": run_id,
                "run_attempt": attempt,
                "head_sha": head_sha,
                "conclusion": "success",
            }
        )
        for raw_job in raw_jobs:
            job, timeout_class, identity = _parse_job(
                raw_job,
                workflow_run_id=run_id,
                run_attempt=attempt,
            )
            job_id = job["job_id"]
            if job_id in job_ids:
                raise ValueError(f"duplicate job ID: {job_id}")
            job_ids.add(job_id)
            jobs.append(job)
            class_samples[timeout_class].append(
                {
                    "workflow_run_id": run_id,
                    "run_attempt": attempt,
                    "job_id": job_id,
                    "runtime_seconds": job["runtime_seconds"],
                }
            )
            if timeout_class in FULL_CLASSES:
                artifact_name = (
                    f"pytest-timings-{timeout_class}-{identity}-attempt-{attempt}"
                )
                if artifact_name in artifact_names:
                    raise ValueError(f"duplicate JUnit artifact identity: {artifact_name}")
                artifact_names.add(artifact_name)
                tests.extend(
                    _junit_records(
                        Path(junit_root) / artifact_name,
                        workflow_run_id=run_id,
                        run_attempt=attempt,
                        job_id=job_id,
                        artifact_name=artifact_name,
                        job_runtime=job["runtime_seconds"],
                    )
                )

    if len(run_ids) != 1:
        raise ValueError("timing evidence must come from exactly one workflow run")
    if attempts != {1, 2, 3, 4, 5}:
        raise ValueError("workflow attempts must be contiguous from 1 through 5")

    classes = {}
    for timeout_class, ceiling in TIMEOUT_CEILINGS.items():
        samples = sorted(
            class_samples.get(timeout_class, []),
            key=lambda sample: (sample["run_attempt"], sample["job_id"]),
        )
        if len(samples) < 5:
            raise ValueError(f"{timeout_class} requires at least five timing samples")
        p95 = nearest_rank_p95([sample["runtime_seconds"] for sample in samples])
        if p95 > ceiling * 0.8:
            raise ValueError(f"{timeout_class} p95 lacks 20 percent timeout headroom")
        classes[timeout_class] = {
            "ceiling_seconds": ceiling,
            "p95_seconds": p95,
            "samples": samples,
        }

    generated = generated_at or datetime.now(timezone.utc).replace(microsecond=0)
    if generated.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    report = {
        "schema_version": "ci-timeout-evidence/v1",
        "head_sha": head_sha,
        "generated_at": generated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workflow_attempts": sorted(workflow_attempts, key=lambda item: item["run_attempt"]),
        "jobs": sorted(jobs, key=lambda item: (item["run_attempt"], item["job_id"])),
        "classes": classes,
        "tests": sorted(
            tests,
            key=lambda item: (item["run_attempt"], item["job_id"], item["node_id"]),
        ),
    }
    validate_schema(report, SCHEMA_PATH)
    return report


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
