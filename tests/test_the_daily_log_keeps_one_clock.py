"""The daily log keeps one clock, and a claim names the instant its block meant.

Audit 2026-09-27 C-5: the headings were written on two clocks and every claim
labelled a local wall-clock reading `Z`. Research:
docs/research/2026-09-27-the-daily-log-keeps-one-clock.md
"""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from tests.test_claims import raw_claim, source_bytes

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Clock-reading writers. Breadcrumbs use the verified occurrence instant instead;
# test_breadcrumb_decision and test_breadcrumb_worker cover delayed delivery.
DAILY_CLOCK_READERS = (
    ("mcp_server.py", "_log_decision"),
    ("session_end_project_tag.py", "_tag_project_payload"),
    ("memory_queue.py", "_manual_flush"),
    ("flush_memory.py", "_capture_now"),
    ("daily_log_append.py", "append_daily"),
    ("episode_consolidation.py", "consolidate_day"),
    ("episode_consolidation.py", "_today_or"),
    ("episode_consolidation.py", "_default_day"),
)


@pytest.fixture
def kolkata():
    """A machine five and a half hours east of UTC."""
    if not hasattr(time, "tzset"):
        pytest.skip("the process zone cannot be changed on this platform")
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    yield
    os.environ.pop("TZ", None)
    os.environ.update({} if before is None else {"TZ": before})
    time.tzset()


@pytest.fixture
def pipeline(tmp_path: Path):
    from claims import ClaimPipeline
    from evidence_resolver import EvidenceResolver

    daily = tmp_path / "knowledge/daily/2026-01-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source_bytes())
    return ClaimPipeline(EvidenceResolver(tmp_path))


def _normalized(pipeline) -> dict:
    block = pipeline.split_blocks(source_bytes())[0]
    return pipeline.normalize(pipeline.verify_literal(pipeline.extract(block, raw_claim())[0])).record


def test_a_claim_names_the_utc_instant_of_its_local_block(kolkata, pipeline) -> None:
    from claims import validate_claim_record

    record = _normalized(pipeline)
    assert record["observed_at"] == "2026-01-01T21:34:05Z"
    validate_claim_record(record)


def test_a_ledger_written_before_keeps_validating(kolkata, pipeline) -> None:
    from claims import validate_claim_record

    record = {**_normalized(pipeline), "observed_at": "2026-01-02T03:04:05Z"}
    validate_claim_record(record)


def test_an_instant_no_real_offset_explains_is_refused(pipeline) -> None:
    from claims import validate_claim_record

    record = {**_normalized(pipeline), "observed_at": "2026-01-02T03:04:06Z"}
    with pytest.raises(ValueError, match="observation"):
        validate_claim_record(record)


def test_the_compile_names_the_same_instant(kolkata) -> None:
    from compile_memory import block_instant

    assert block_instant("2026-01-02", "03:04:05") == "2026-01-01T21:34:05Z"


def _is_named(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.FunctionDef) and node.name == name


def _function(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in ast.walk(tree) if _is_named(node, name))


def _clock_calls(path: Path, name: str) -> set[str]:
    calls = [node for node in ast.walk(_function(path, name)) if isinstance(node, ast.Call)]
    return {ast.unparse(call.func) for call in calls}


@pytest.mark.parametrize(("module", "function"), DAILY_CLOCK_READERS)
def test_every_daily_writer_reads_the_one_clock(module: str, function: str) -> None:
    calls = _clock_calls(SCRIPTS / module, function)
    assert "local_now" in calls
    assert not calls & {"datetime.now", "_utc_now", "datetime.utcnow", "date.today"}
