"""Optional LLM-judge pass over LongMemEval results (MEM-10).

LongMemEval's official metric is an LLM judge; the deterministic metrics in
`longmemeval_score.py` under-credit answers that state a correct alias or
paraphrase. This pass reads the results JSONL written by
`run_longmemeval.py`, asks the configured provider one short grading question
per answered row, and writes a judged JSONL next to it (resumable).

Deviations from the paper, stated plainly: one generic grading prompt is used
for every category instead of the paper's per-type GPT-4o prompts, and the
judge is the same local claude provider that produced the answers — a
same-family judge, which published audits warn can be lenient. Both facts are
carried into the research note next to the numbers.

    uv run python benchmark/longmemeval_judge.py --results <results.jsonl>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402

REPO = BENCHMARK_DIR.parent
JUDGE_SYSTEM_PROMPT = (
    "You grade question-answering outputs. Decide whether the model answer "
    "states the same fact as the gold answer: aliases, paraphrases, extra "
    "correct context, and different formats of the same value all count as "
    "correct; a missing, different, or contradicting fact counts as wrong. "
    "Reply with exactly one word: yes or no."
)

# One category is not graded on facts, and grading it on facts is why it read
# 0.0000 with zero spread across three runs of 200 on 2026-09-01. LongMemEval's
# preference questions evaluate personalised generation against a rubric: the
# gold is a description of what a good answer would take into account, not a
# value to match. Token-overlap metrics are reported as inapplicable there for
# the same reason.
# See `docs/research/2026-09-01-a-category-graded-by-the-wrong-question.md`.
RUBRIC_CATEGORY = "single-session-preference"

RUBRIC_SYSTEM_PROMPT = (
    "You grade personalised answers against a rubric. The gold text describes "
    "what the user would prefer an answer to take into account; it is not a "
    "fact to match. Decide whether the model answer respects that preference: "
    "it counts as correct when it takes those things into account, in its own "
    "words and in any order, even without naming every one of them. An empty "
    "answer, a refusal, or advice that ignores what the gold describes counts "
    "as wrong. Reply with exactly one word: yes or no."
)


def system_prompt_for(row: dict) -> str:
    """The fact prompt, except where the category is graded against a rubric."""
    if str(row.get("category")) == RUBRIC_CATEGORY:
        return RUBRIC_SYSTEM_PROMPT
    return JUDGE_SYSTEM_PROMPT


def _rubric_prompt(row: dict) -> str:
    return (
        f"Question: {row.get('question', '')}\n"
        f"What the user would prefer: {row.get('gold', '')}\n"
        f"Model answer: {row.get('hypothesis', '')}\n"
        "Does the model answer respect that preference? Answer yes or no."
    )


def _fact_prompt(row: dict) -> str:
    return (
        f"Question: {row.get('question', '')}\n"
        f"Gold answer: {row.get('gold', '')}\n"
        f"Model answer: {row.get('hypothesis', '')}\n"
        "Does the model answer state the same fact as the gold answer? "
        "Answer yes or no."
    )


def judge_prompt(row: dict) -> str:
    if str(row.get("category")) == RUBRIC_CATEGORY:
        return _rubric_prompt(row)
    return _fact_prompt(row)


def _verdict_of(text: str | None) -> bool | None:
    if not text:
        return None
    word = text.strip().split()[0].strip(".,!").casefold()
    if word in {"yes", "no"}:
        return word == "yes"
    return None


def needs_judging(row: dict) -> bool:
    """Only answered non-abstention rows need a judge; the rest are settled."""
    if row.get("is_abstention") or row.get("error"):
        return False
    return row.get("status") == "answered" and bool(row.get("hypothesis"))


def _judged_row(row: dict, call) -> dict:
    if not needs_judging(row):
        return {**row, "judge_correct": None, "judge_seconds": None}
    started = time.monotonic()
    raw = call(judge_prompt(row), system_prompt_for(row), 10)
    return {
        **row,
        "judge_correct": _verdict_of(raw),
        "judge_raw": (raw or "").strip()[:80],
        "judge_seconds": round(time.monotonic() - started, 2),
    }


def _provider_call():
    """The shared provider client, called from outside this repository.

    `claude -p` loads the working directory's `CLAUDE.md`; this vault's
    `@`-imports about 300 KB of operating instructions, which turns a one-word
    grading call into an agent turn about something else. Measured 2026-08-28,
    same prompt one minute apart: 175.42s and a wrong-topic answer from this
    repository, 12.59s and the right word from a neutral directory. The runner
    fixes this by chdir-ing into its throwaway vault; the judge has no vault,
    so it uses a bare temporary directory.
    """
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from llm_client import call_llm

    os.chdir(tempfile.mkdtemp(prefix="longmemeval-judge-"))
    return call_llm


def _loaded_rows(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _judged_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(row.get("question_id")) for row in _loaded_rows(path)}


def _row_verdict(row: dict) -> float:
    """The judge's word where it spoke; the deterministic score elsewhere."""
    verdict = row.get("judge_correct")
    if verdict is None:
        verdict = bool(longmemeval_score.score_question(row).get("correct"))
    return float(verdict)


def _accuracy_row(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "judge_accuracy": None}
    return {"n": len(values), "judge_accuracy": round(sum(values) / len(values), 4)}


def _gradable(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not longmemeval_score.is_provider_failure(row)]


def _judge_accuracy(rows: list[dict]) -> dict:
    """Judge-or-deterministic accuracy per category, abstention unchanged.

    Rows the provider never answered are dropped, not graded: there is no
    hypothesis to compare, so counting them would report the throughput of
    this machine's single provider as the memory system's recall.
    """
    verdicts: dict[str, list[float]] = {}
    for row in _gradable(rows):
        verdicts.setdefault(str(row.get("category")), []).append(_row_verdict(row))
    report = {name: _accuracy_row(values) for name, values in sorted(verdicts.items())}
    report["overall"] = _accuracy_row([value for values in verdicts.values() for value in values])
    return report


def _resolved(given: str | None, results_path: Path, suffix: str) -> Path:
    """An absolute output path, fixed before the provider call moves the cwd.

    `_provider_call` chdirs out of this repository so `claude -p` does not
    inherit its `CLAUDE.md`; a relative output path resolved after that would
    land in the temporary directory.
    """
    if given:
        return Path(given).resolve()
    return results_path.with_suffix(suffix)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", default=None, help="judged JSONL (default: <results>.judged.jsonl)")
    parser.add_argument("--report", default=None, help="judge report JSON")
    return parser.parse_args()


def _judge_pending(rows: list[dict], out_path: Path, call) -> None:
    done = _judged_ids(out_path)
    pending = [row for row in rows if str(row.get("question_id")) not in done]
    print(f"rows={len(rows)} judged={len(done)} pending={len(pending)}")
    with out_path.open("a", encoding="utf-8") as stream:
        for index, row in enumerate(pending, start=1):
            judged = _judged_row(row, call)
            stream.write(json.dumps(judged, ensure_ascii=False) + "\n")
            stream.flush()
            print(
                f"[{index}/{len(pending)}] {judged.get('question_id')} "
                f"judge={judged.get('judge_correct')}",
                flush=True,
            )


def main() -> int:
    args = parse_args()
    results_path = Path(args.results).resolve()
    out_path = _resolved(args.out, results_path, ".judged.jsonl")
    report_path = _resolved(args.report, results_path, ".judge-report.json")
    _judge_pending(_loaded_rows(results_path), out_path, _provider_call())
    report = _judge_accuracy(_loaded_rows(out_path))
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {report_path}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
