from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from ci_timing_report import compile_report, main, nearest_rank_p95
from reliable_memory import SchemaValidationError, validate_schema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "benchmark" / "ci-timeout-evidence-v1.schema.json"
HEAD_SHA = "a" * 40
RUN_ID = 8675309
CLASSES = {
    "focused": (900, "lint"),
    "clean": (1200, "production-py3.10"),
    "installer": (1200, "linux"),
    "linux_full": (2700, "py3.10"),
    "windows_full": (3600, "py3.10"),
    "macos_full": (2700, "py3.10"),
}
FULL_CLASSES = {"linux_full", "windows_full", "macos_full"}


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_attempt(
    root: Path,
    attempt: int,
    *,
    run_id: int = RUN_ID,
    head_sha: str = HEAD_SHA,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    started = datetime(2026, 8, attempt, tzinfo=timezone.utc)
    jobs = []
    junit_root = root / f"junit-{attempt}"
    junit_root.mkdir()
    for offset, (timeout_class, (_, identity)) in enumerate(CLASSES.items(), start=1):
        runtime = float(90 + attempt + offset)
        job_id = attempt * 100 + offset
        jobs.append(
            {
                "databaseId": job_id,
                "name": f"timing::{timeout_class}::{identity}",
                "status": "completed",
                "conclusion": "success",
                "startedAt": _timestamp(started),
                "completedAt": _timestamp(started + timedelta(seconds=runtime)),
            }
        )
        if timeout_class in FULL_CLASSES:
            artifact = f"pytest-timings-{timeout_class}-{identity}-attempt-{attempt}"
            artifact_root = junit_root / artifact
            artifact_root.mkdir()
            (artifact_root / "junit.xml").write_text(
                "<testsuites time=\"10\"><testsuite name=\"pytest\" time=\"10\">"
                "<testcase classname=\"tests.test_example\" name=\"test_ok\" time=\"10\"/>"
                "</testsuite></testsuites>\n",
                encoding="utf-8",
            )
    payload = {
        "databaseId": run_id,
        "attempt": attempt,
        "conclusion": "success",
        "headSha": head_sha,
        "jobs": jobs,
    }
    run_json = root / f"run-{attempt}.json"
    run_json.write_text(json.dumps(payload), encoding="utf-8")
    return run_json, junit_root


def _evidence_inputs(tmp_path: Path) -> tuple[list[Path], list[Path]]:
    pairs = [_write_attempt(tmp_path, attempt) for attempt in range(1, 6)]
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def _rewrite(path: Path, update) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    update(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_nearest_rank_p95_is_inclusive_and_rejects_nonfinite_values() -> None:
    assert nearest_rank_p95([1.0, 3.0, 2.0, 5.0, 4.0]) == 5.0
    with pytest.raises(ValueError, match="sample"):
        nearest_rank_p95([])
    for value in (math.nan, math.inf, -math.inf, -1.0):
        with pytest.raises(ValueError, match="finite nonnegative"):
            nearest_rank_p95([value])


def test_compile_report_emits_closed_schema_valid_evidence(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    report = compile_report(
        run_jsons,
        junit_roots,
        head_sha=HEAD_SHA,
        generated_at=datetime(2026, 8, 12, 12, 34, 56, tzinfo=timezone.utc),
    )

    validate_schema(report, SCHEMA)
    assert report["schema_version"] == "ci-timeout-evidence/v1"
    assert report["head_sha"] == HEAD_SHA
    assert report["generated_at"] == "2026-08-12T12:34:56Z"
    assert [item["run_attempt"] for item in report["workflow_attempts"]] == [1, 2, 3, 4, 5]
    assert set(report["classes"]) == set(CLASSES)
    assert len(report["jobs"]) == 30
    assert len(report["tests"]) == 15
    assert len({item["artifact_name"] for item in report["tests"]}) == 15
    assert report["classes"]["focused"]["p95_seconds"] == 96.0
    broken = copy.deepcopy(report)
    broken["unexpected"] = True
    with pytest.raises(SchemaValidationError, match="unknown properties"):
        validate_schema(broken, SCHEMA)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(attempt=6), "attempts"),
        (lambda payload: payload.update(databaseId=RUN_ID + 1), "workflow run"),
        (lambda payload: payload.update(headSha="b" * 40), "head SHA"),
        (lambda payload: payload.update(conclusion="failure"), "successful"),
        (lambda payload: payload["jobs"][0].update(conclusion="cancelled"), "successful"),
        (lambda payload: payload["jobs"][0].update(name="timing::unknown::job"), "class"),
    ],
)
def test_compile_report_rejects_invalid_attempt_or_job_identity(
    tmp_path: Path, mutation: Any, message: str
) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    _rewrite(run_jsons[-1], mutation)
    with pytest.raises(ValueError, match=message):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)


def test_compile_report_requires_exactly_five_paired_attempts(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    with pytest.raises(ValueError, match="exactly five"):
        compile_report(run_jsons[:-1], junit_roots[:-1], head_sha=HEAD_SHA)
    with pytest.raises(ValueError, match="paired"):
        compile_report(run_jsons, junit_roots[:-1], head_sha=HEAD_SHA)


def test_compile_report_rejects_duplicate_attempt_and_missing_class(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    _rewrite(run_jsons[-1], lambda payload: payload.update(attempt=4))
    with pytest.raises(ValueError, match="duplicate.*attempt"):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)

    run_jsons, junit_roots = _evidence_inputs(tmp_path / "second")
    for path in run_jsons:
        _rewrite(
            path,
            lambda payload: payload["jobs"].__setitem__(
                slice(None),
                [job for job in payload["jobs"] if "::installer::" not in job["name"]],
            ),
        )
    with pytest.raises(ValueError, match="installer.*samples"):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)


def test_compile_report_requires_one_junit_artifact_per_full_job(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    next(junit_roots[0].glob("**/*.xml")).unlink()
    with pytest.raises(ValueError, match="JUnit artifact"):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)


def test_compile_report_rejects_nonfinite_or_impossible_junit_time(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    xml_path = next(junit_roots[0].glob("**/*.xml"))
    original = xml_path.read_text(encoding="utf-8")
    xml_path.write_text(original.replace('time="10"', 'time="NaN"'), encoding="utf-8")
    with pytest.raises(ValueError, match="finite nonnegative"):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)

    xml_path.write_text(original.replace('time="10"', 'time="1000"'), encoding="utf-8")
    with pytest.raises(ValueError, match="job runtime"):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)


def test_compile_report_requires_twenty_percent_timeout_headroom(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)

    def slow_focused(payload: dict[str, object]) -> None:
        job = payload["jobs"][0]
        started = datetime.fromisoformat(job["startedAt"].replace("Z", "+00:00"))
        job["completedAt"] = _timestamp(started + timedelta(seconds=721))

    for path in run_jsons:
        _rewrite(path, slow_focused)
    with pytest.raises(ValueError, match="focused.*headroom"):
        compile_report(run_jsons, junit_roots, head_sha=HEAD_SHA)


def test_cli_writes_strict_json_report(tmp_path: Path) -> None:
    run_jsons, junit_roots = _evidence_inputs(tmp_path)
    output = tmp_path / "report.json"
    arguments = []
    for run_json, junit_root in zip(run_jsons, junit_roots, strict=True):
        arguments.extend(("--run-json", str(run_json), "--junit-root", str(junit_root)))
    arguments.extend(("--head-sha", HEAD_SHA, "--output", str(output)))

    assert main(arguments) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    validate_schema(report, SCHEMA)
