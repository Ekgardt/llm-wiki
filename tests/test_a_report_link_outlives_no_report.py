"""A step artifact is kept as long as the report that names it.

Artifacts were held to the report count and lasted about two nights while the
reports that point at them lasted 30 days. See
docs/research/2026-09-25-a-report-link-outlives-no-report.md.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import maintenance_helpers


def test_a_month_of_artifacts_is_kept(tmp_path: Path, monkeypatch) -> None:
    artifacts = tmp_path / "maintenance"
    artifacts.mkdir()
    monkeypatch.setattr(maintenance_helpers, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(maintenance_helpers, "ARTIFACT_DIR", artifacts)
    now = time.time()
    for index in range(29 * 30):
        path = artifacts / f"step-{index:04d}.out.log"
        path.write_text("x", encoding="utf-8")
        moment = now - index * 2800
        os.utime(path, (moment, moment))

    maintenance_helpers.prune_maintenance_output()

    assert len(list(artifacts.glob("*.log"))) == 29 * 30
