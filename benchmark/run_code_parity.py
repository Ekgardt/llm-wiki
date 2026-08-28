"""Code-intelligence parity stand: llm-wiki surfaces vs codebase-memory-mcp.

CODE-07 (docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md §12). One task list,
two sides, the same hand-established gold:

* llm-wiki answers through its own product surfaces — the ``get_architecture``
  modes and ``find_dead_code`` exactly as ``mcp_server`` dispatches them.
  Each call runs in a child process because the live extraction path does
  not honor the operation deadline (measured 491.6 s for a 60 s deadline on
  2026-08-28); the parent kills the child at budget + grace and records a
  timeout instead of waiting.
* ``llm_wiki_best`` is the same product through the surface that actually
  serves each question — mostly the ``query`` mode added 2026-08-28, plus
  ``snippet``.  It exists because the first pairing (2026-08-28) measured
  ``llm_wiki`` at 0/13: ten tasks died on one shared defect, the hard-coded
  ``max_rows=10_000`` in ``scripts/code_graph.py`` against a graph holding
  35,313 CALLS edges and 19,153 function/method nodes.  Reporting only the
  broken surface would understate the product; reporting only the working
  one would hide the defect.  Both columns ship.
* codebase-memory-mcp answers through its own CLI
  (``codebase-memory-mcp cli --json <tool> <json>``), which runs the same
  tools its MCP server exposes.

Honesty notes, load-bearing:

* Wall time includes each side's real process startup (Python import of
  ``mcp_server`` on one side, the cbm binary handshake on the other).
  Both sides pay their own true cost; neither is amortized.
* ``tokens`` is ``len(answer_text) // 4`` — an approximation, not a
  tokenizer.
* Grading is mechanical: word-boundary matching of gold terms in the
  answer text.  ``correct`` = every must term present and no must_not
  term; ``partial`` = some but not all; ``wrong`` = none, or a must_not
  hit, or no answer.  A side that timed out or errored grades ``wrong``.
* ``wrong_but_confident`` counts answers delivered without any error or
  timeout marker that still graded wrong — the safety dimension.
* ``operator_attention_events`` counts calls that did not answer at all
  (timeout / error / tool_error) — each one forces the operator or agent
  to fall back to manual search.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = Path(__file__).resolve().parent / "code-parity-v1.json"
CBM_BINARY = Path.home() / ".local" / "bin" / "codebase-memory-mcp"
CBM_PROJECT = "home-user-llm-wiki"
CALL_BUDGET_SECONDS = 60.0
KILL_GRACE_SECONDS = 10.0
ANSWER_EXCERPT_CHARS = 4000
SIDES = ("llm_wiki", "llm_wiki_best", "cbm")


def load_tasks(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def approx_tokens(text: str) -> int:
    """len/4 approximation, documented as such in the module docstring."""
    return len(text) // 4


def _boundary_pattern(term: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
    )


def _found(entry: str | list, text: str) -> bool:
    alternates = entry if isinstance(entry, list) else [entry]
    return any(_boundary_pattern(term).search(text) for term in alternates)


def _hit_count(must: list, text: str) -> int:
    return sum(1 for entry in must if _found(entry, text))


def _grade_text(text: str, must: list, must_not: list) -> str:
    if any(_found(entry, text) for entry in must_not):
        return "wrong"
    hits = _hit_count(must, text)
    if hits == len(must):
        return "correct"
    return "partial" if hits else "wrong"


def _grade_side(outcome: dict, must: list, must_not: list) -> str:
    if outcome["status"] in ("timeout", "error"):
        return "wrong"
    return _grade_text(outcome["text"], must, must_not)


def _side_terms(task: dict, calls: list[dict], key: str) -> list:
    for call in calls:
        if key in call:
            return call[key]
    return task["gold"][key]


def _timeout_row(seconds: float) -> dict:
    return {"status": "timeout", "seconds": seconds, "text": ""}


def _timed_subprocess(cmd: list[str], outcome) -> dict:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CALL_BUDGET_SECONDS + KILL_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _timeout_row(round(time.monotonic() - start, 3))
    return outcome(proc, round(time.monotonic() - start, 3))


def _error_row(seconds: float, detail: str) -> dict:
    return {"status": "error", "seconds": seconds, "text": detail[-500:]}


def _llm_wiki_status(data: object) -> str:
    if not isinstance(data, dict):
        return "answered"
    flagged = data.get("error") or data.get("status") in ("error", "timeout")
    return "tool_error" if flagged else "answered"


def _llm_wiki_outcome(proc: subprocess.CompletedProcess, seconds: float) -> dict:
    if proc.returncode != 0:
        return _error_row(seconds, proc.stderr)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _error_row(seconds, "unparseable child output: " + proc.stdout)
    text = json.dumps(payload["data"], ensure_ascii=False, default=str)
    return {
        "status": _llm_wiki_status(payload["data"]),
        "seconds": seconds,
        "text": text,
    }


def run_llm_wiki_call(call: dict, directory: str) -> dict:
    payload = {
        "tool": call["tool"],
        "arguments": {"directory": directory, **call["arguments"]},
    }
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        json.dumps(payload),
    ]
    return _timed_subprocess(cmd, _llm_wiki_outcome)


def _cbm_text(document: dict) -> str:
    structured = document.get("structuredContent") or {}
    text = structured.get("text")
    if isinstance(text, str):
        return text
    content = document.get("content") or [{}]
    return str(content[0].get("text", ""))


def _cbm_outcome(proc: subprocess.CompletedProcess, seconds: float) -> dict:
    if proc.returncode != 0:
        return _error_row(seconds, proc.stderr)
    try:
        document = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _error_row(seconds, "unparseable cbm output: " + proc.stdout)
    status = "tool_error" if document.get("isError") else "answered"
    return {"status": status, "seconds": seconds, "text": _cbm_text(document)}


def run_cbm_call(call: dict, directory: str) -> dict:
    del directory  # cbm addresses the repository by project name
    arguments = {"project": CBM_PROJECT, **call["arguments"]}
    cmd = [str(CBM_BINARY), "cli", "--json", call["tool"], json.dumps(arguments)]
    return _timed_subprocess(cmd, _cbm_outcome)


_RUNNERS = {
    "llm_wiki": run_llm_wiki_call,
    "llm_wiki_best": run_llm_wiki_call,
    "cbm": run_cbm_call,
}


def _side_status(statuses: list[str]) -> str:
    return "answered" if "answered" in statuses else statuses[0]


def _values(results: list[dict], key: str) -> list:
    return [result[key] for result in results]


def run_side(calls: list[dict], runner) -> dict:
    results = [runner(call) for call in calls]
    statuses = _values(results, "status")
    return {
        "status": _side_status(statuses),
        "seconds": round(sum(_values(results, "seconds")), 3),
        "text": "\n".join(_values(results, "text")),
        "call_statuses": statuses,
    }


def _scored_side(task: dict, side: str, directory: str) -> dict:
    calls = task[side]
    runner = _RUNNERS[side]
    outcome = run_side(calls, lambda call: runner(call, directory))
    must = _side_terms(task, calls, "must")
    must_not = _side_terms(task, calls, "must_not")
    return {
        "status": outcome["status"],
        "call_statuses": outcome["call_statuses"],
        "grade": _grade_side(outcome, must, must_not),
        "seconds": outcome["seconds"],
        "tokens": approx_tokens(outcome["text"]),
        "answer_excerpt": outcome["text"][:ANSWER_EXCERPT_CHARS],
    }


def score_task(task: dict, directory: str, sides: tuple[str, ...]) -> dict:
    row = {"id": task["id"], "kind": task["kind"], "question": task["question"]}
    for side in sides:
        row[side] = _scored_side(task, side, directory)
        _print_progress(task, side, row[side])
    return row


def _print_progress(task: dict, side: str, scored: dict) -> None:
    print(
        f"{task['id']} {side:8s} {scored['status']:10s} "
        f"{scored['grade']:7s} {scored['seconds']:8.2f}s "
        f"{scored['tokens']:6d} tok",
        file=sys.stderr,
        flush=True,
    )


def _is_confident_wrong(outcome: dict) -> bool:
    return outcome["status"] == "answered" and outcome["grade"] == "wrong"


def _is_unanswered(outcome: dict) -> bool:
    return outcome["status"] != "answered"


def _count_if(outcomes: list[dict], predicate) -> int:
    return sum(1 for outcome in outcomes if predicate(outcome))


def _grade_counts(outcomes: list[dict]) -> dict:
    grades = Counter(_values(outcomes, "grade"))
    return {
        "correct": grades.get("correct", 0),
        "partial": grades.get("partial", 0),
        "wrong": grades.get("wrong", 0),
    }


def _side_summary(rows: list[dict], side: str) -> dict:
    outcomes = _values(rows, side)
    summary = _grade_counts(outcomes)
    summary.update({
        "statuses": dict(Counter(_values(outcomes, "status"))),
        "total_seconds": round(sum(_values(outcomes, "seconds")), 2),
        "total_tokens": sum(_values(outcomes, "tokens")),
        "wrong_but_confident": _count_if(outcomes, _is_confident_wrong),
        "operator_attention_events": _count_if(outcomes, _is_unanswered),
    })
    return summary


def summarize(rows: list[dict], sides: tuple[str, ...]) -> dict:
    return {side: _side_summary(rows, side) for side in sides}


def run_stand(tasks_path: Path, directory: str, sides: tuple[str, ...]) -> dict:
    document = load_tasks(tasks_path)
    rows = [score_task(task, directory, sides) for task in document["tasks"]]
    return {
        "version": document["version"],
        "directory": directory,
        "budget_seconds": CALL_BUDGET_SECONDS,
        "tasks": rows,
        "summary": summarize(rows, sides),
    }


def _child_result(payload: dict) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    import mcp_server

    start = time.monotonic()
    data, _ = mcp_server._tool_call_data(
        payload["tool"], payload["arguments"], start + CALL_BUDGET_SECONDS
    )
    return {"seconds": round(time.monotonic() - start, 3), "data": data}


def _run_child(argument: str) -> int:
    result = _child_result(json.loads(argument))
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", type=Path, default=TASKS_PATH)
    parser.add_argument("--directory", default=str(ROOT))
    parser.add_argument("--sides", nargs="+", choices=SIDES, default=list(SIDES))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--child", default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _write_report(report: dict, out: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if out is not None:
        out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.child is not None:
        return _run_child(args.child)
    report = run_stand(args.tasks, args.directory, tuple(args.sides))
    _write_report(report, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
