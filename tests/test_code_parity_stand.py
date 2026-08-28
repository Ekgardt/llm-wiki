"""The code-parity stand grades mechanically and its task list stays valid."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import run_code_parity as stand  # noqa: E402


def _tasks() -> list[dict]:
    return stand.load_tasks(stand.TASKS_PATH)["tasks"]


def test_task_ids_are_unique_and_every_side_is_specified():
    tasks = _tasks()
    ids = [task["id"] for task in tasks]
    assert len(set(ids)) == len(ids)
    for side in stand.SIDES:
        assert all(task[side] for task in tasks), side


def test_the_best_llm_wiki_side_runs_the_same_runner_as_the_configured_one():
    assert stand._RUNNERS["llm_wiki_best"] is stand._RUNNERS["llm_wiki"]


def _dead_code_tasks() -> list[dict]:
    return [task for task in _tasks() if task["id"] in ("T06", "T07")]


def _assert_empty_frontier_is_required(task: dict) -> None:
    calls = task["llm_wiki_best"]
    must = stand._side_terms(task, calls, "must")
    assert '"nodes": []' in must, task["id"]
    assert stand._side_terms(task, calls, "must_not") == task["gold"]["must_not"]
    echo = '{{"nodes": [{{"name": "{}"}}]}}'.format(task["gold"]["must"][0])
    assert stand._grade_text(echo, must, []) == "partial"


def test_the_dead_code_override_demands_an_empty_frontier_not_a_name_echo():
    """T06/T07 via query mode: echoing the symbol must not earn the grade."""
    tasks = _dead_code_tasks()
    assert len(tasks) == 2
    for task in tasks:
        _assert_empty_frontier_is_required(task)


def test_every_task_carries_hand_established_gold():
    for task in _tasks():
        assert task["gold"]["citations"], task["id"]
        assert task["gold"]["must"], task["id"]


def test_the_task_kinds_cover_the_contracted_families():
    kinds = {task["kind"] for task in _tasks()}
    expected = {
        "who-calls",
        "what-does-x-call",
        "find-symbol-definition",
        "find-dead-code",
        "impact-of-changing-x",
        "architecture-of-module",
        "multi-hop",
        "architecture-summary",
        "community-naming",
    }
    assert expected <= kinds


def test_grading_matches_on_word_boundaries():
    assert stand._grade_text("calls retrieval.py stuff", ["retrieve"], []) == "wrong"
    assert stand._grade_text("then retrieve ran", ["retrieve"], []) == "correct"


def test_grading_accepts_any_alternate_of_a_must_entry():
    must = [["_fused_candidates", "2845"]]
    assert stand._grade_text("call at line 2845", must, []) == "correct"
    assert stand._grade_text("_fused_candidates calls it", must, []) == "correct"
    assert stand._grade_text("line 28451", must, []) == "wrong"


def test_a_must_not_hit_grades_wrong_even_with_all_musts_present():
    grade = stand._grade_text("dead: _flush_started fuse_rrf", ["_flush_started"], ["fuse_rrf"])
    assert grade == "wrong"


def test_partial_grade_when_some_musts_are_missing():
    grade = stand._grade_text("only _write_one here", ["_write_one", "_keep_session_record"], [])
    assert grade == "partial"


def test_a_timeout_or_error_side_grades_wrong():
    assert stand._grade_side({"status": "timeout", "text": ""}, ["x"], []) == "wrong"
    assert stand._grade_side({"status": "error", "text": "x"}, ["x"], []) == "wrong"


def test_a_tool_error_answer_is_graded_on_its_text():
    outcome = {"status": "tool_error", "text": '{"error": "operation_failed"}'}
    assert stand._grade_side(outcome, ["_fused_candidates"], []) == "wrong"


def test_side_level_must_overrides_gold_only_when_present():
    task = {"gold": {"must": ["gold_term"]}}
    calls = [{"tool": "trace_path", "must": ["callers_total: 0"]}]
    assert stand._side_terms(task, calls, "must") == ["callers_total: 0"]
    assert stand._side_terms(task, [{"tool": "x"}], "must") == ["gold_term"]


def test_cbm_text_prefers_structured_content_and_falls_back():
    structured = {"structuredContent": {"text": "structured"}}
    fallback = {"content": [{"type": "text", "text": "plain"}]}
    assert stand._cbm_text(structured) == "structured"
    assert stand._cbm_text(fallback) == "plain"


def test_llm_wiki_error_payloads_are_flagged_as_tool_errors():
    assert stand._llm_wiki_status({"error": "operation_failed"}) == "tool_error"
    assert stand._llm_wiki_status({"status": "error", "mode": "symbol"}) == "tool_error"
    assert stand._llm_wiki_status({"architecture": {}}) == "answered"


def test_run_side_sums_time_and_answers_if_any_call_answered():
    results = iter(
        [
            {"status": "tool_error", "seconds": 1.0, "text": "no"},
            {"status": "answered", "seconds": 2.0, "text": "yes"},
        ]
    )
    outcome = stand.run_side([{"a": 1}, {"b": 2}], lambda call: next(results))
    assert outcome["status"] == "answered"
    assert outcome["seconds"] == 3.0
    assert outcome["text"] == "no\nyes"


def test_the_child_protocol_round_trips_a_real_tool_error(tmp_path):
    payload = {"tool": "get_architecture", "arguments": {"directory": str(tmp_path)}}
    proc = subprocess.run(
        [sys.executable, str(BENCHMARK / "run_code_parity.py"), "--child", json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert "data" in parsed
    assert parsed["seconds"] >= 0
