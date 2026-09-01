"""MEM-10: the vault's first honest number on LongMemEval.

Runs the `longmemeval_s` questions against the product's real pipeline: one
disposable adopted vault per question (see `longmemeval_vault.py`), capture's
own write path for every haystack session, one immutable generation, then
`retrieve_via_search_memory` + `grounded_qa` with the configured provider.

    uv run python benchmark/run_longmemeval.py --sample 50   # stratified sample
    uv run python benchmark/run_longmemeval.py --full        # all 500, overnight

Results stream to a JSONL file (resumable: already-answered question ids are
skipped), and the aggregate report is deterministic text metrics — see
`longmemeval_score.py` for what the numbers mean and what they must not be
compared against.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_data  # noqa: E402
import longmemeval_score  # noqa: E402

WORKER = BENCHMARK_DIR / "longmemeval_vault.py"
RESULTS_DIR = longmemeval_data.DATASET_DIR
QUESTION_TIMEOUT_SECONDS = 2400


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=50, help="stratified sample size")
    parser.add_argument("--full", action="store_true", help="run all 500 questions")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--provider-timeout", type=int, default=240)
    parser.add_argument("--results", default=None, help="JSONL stream (resumable)")
    parser.add_argument("--report", default=None, help="aggregate report JSON")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--keep-vaults", action="store_true")
    parser.add_argument("--list-only", action="store_true", help="print the sample and exit")
    return parser.parse_args()


def _run_tag(args: argparse.Namespace) -> str:
    if args.full:
        return "full"
    return f"n{args.sample}-seed{args.seed}"


def _results_path(args: argparse.Namespace) -> Path:
    if args.results:
        return Path(args.results)
    return RESULTS_DIR / f"results-{_run_tag(args)}.jsonl"


def _report_path(args: argparse.Namespace) -> Path:
    if args.report:
        return Path(args.report)
    return RESULTS_DIR / f"report-{_run_tag(args)}.json"


def _existing_results(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[str(row.get("question_id"))] = row
    return rows


def _sampled(args: argparse.Namespace, data: list[dict]) -> list[dict]:
    if args.full:
        return data
    return longmemeval_data.stratified_sample(data, args.sample, args.seed)


def _worker_command(question_file: Path, out_file: Path, args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(WORKER),
        "--question",
        str(question_file),
        "--out",
        str(out_file),
    ]
    if args.workdir:
        command.extend(["--workdir", args.workdir])
    if args.keep_vaults:
        command.append("--keep-vault")
    return command


def _worker_environment(args: argparse.Namespace) -> dict[str, str]:
    import os

    environment = os.environ.copy()
    environment.pop("LLM_WIKI_ROOT", None)
    environment.pop("LLM_WIKI_STATE_ROOT", None)
    environment["MEMORY_LLM_PROVIDER"] = args.provider
    environment["MEMORY_LLM_TIMEOUT_S"] = str(args.provider_timeout)
    return environment


def _subprocess_failure(question: dict, detail: str) -> dict:
    return {
        "question_id": str(question.get("question_id")),
        "question_type": str(question.get("question_type")),
        "category": longmemeval_data.category_of(question),
        "is_abstention": longmemeval_data.is_abstention(question),
        "gold": str(question.get("answer", "")),
        "status": "error",
        "hypothesis": "",
        "error": detail[:500],
        "error_kind": "harness_failure",
    }


def _run_worker(question: dict, staging: Path, args: argparse.Namespace) -> dict:
    """One question in its own process; a stale result is never read as fresh.

    Staging is keyed by question id and survives across runs, so the previous
    run's `<id>.result.json` is still on disk when this one starts. Reading it
    back without proof of authorship reported a 2026-08-28 01:22 result as a
    2026-08-28 11:35 one — byte-identical provider timings for a worker that
    had just died. The file is removed before the worker starts and a nonzero
    exit is a failure even when a file is present, so a result can only come
    from the process this call actually ran.
    """
    question_id = str(question["question_id"])
    question_file = staging / f"{question_id}.question.json"
    out_file = staging / f"{question_id}.result.json"
    question_file.write_text(json.dumps(question, ensure_ascii=False), encoding="utf-8")
    out_file.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            _worker_command(question_file, out_file, args),
            env=_worker_environment(args),
            cwd=str(BENCHMARK_DIR.parent),
            capture_output=True,
            text=True,
            timeout=QUESTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _subprocess_failure(question, f"worker exceeded {QUESTION_TIMEOUT_SECONDS}s")
    if not out_file.exists():
        detail = f"worker rc={completed.returncode}: {completed.stderr[-400:]}"
        return _subprocess_failure(question, detail)
    return _worker_result(out_file, completed.returncode)


def _worker_result(out_file: Path, returncode: int) -> dict:
    """The row the worker wrote, carrying a dirty exit rather than hiding it.

    The worker writes its result and returns 0; the process can still abort
    during interpreter shutdown — `terminate called without an active
    exception`, SIGABRT, measured as rc=-6 on 2026-08-28 — from a native
    thread left by the embedding model. That happens after the measurement is
    complete, so discarding the row would throw away a good answer for a dirty
    teardown. The file was deleted before this worker started, so its presence
    proves this run wrote it; the exit code rides along as evidence.
    """
    row = json.loads(out_file.read_text(encoding="utf-8"))
    row["worker_exit"] = returncode
    return row


def _with_judgements(rows: list[dict], results_path: Path) -> list[dict]:
    """Fold in any judged rows sitting beside the results, keyed by question id.

    The report's `accuracy` is `contains_answer`, a substring test that cannot
    match a free-text answer and never matches a preference gold — measured
    2026-09-01, `single-session-preference` read 0.0000 by containment and 0.25
    by the judge on the same rows. Every figure the backlog compares against is
    a judge score, so the report has to carry one when it exists.
    """
    judged_path = results_path.with_suffix(".judged.jsonl")
    if not judged_path.is_file():
        return rows
    verdicts = _judged_verdicts(judged_path)
    return [
        {**row, "judge_correct": verdicts.get(str(row.get("question_id")))}
        if str(row.get("question_id")) in verdicts
        else row
        for row in rows
    ]


def _judged_verdicts(path: Path) -> dict[str, object]:
    verdicts: dict[str, object] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row.get("judge_correct"), bool):
            verdicts[str(row.get("question_id"))] = row["judge_correct"]
    return verdicts


def _print_summary(report: dict) -> None:
    columns = (
        "category n scored accuracy judge em f1 prov_fail est_tokens total_tokens "
        "retrieve_s answer_s"
    )
    print(columns)
    for name, row in report.items():
        print(
            f"{name} {row['n']} {row['scored']} {row['accuracy']} {row['em']} "
            f"{row.get('judge_accuracy')} "
            f"{row['f1']} {row['provider_failures']} {row['mean_est_prompt_tokens']} "
            f"{row.get('mean_est_total_prompt_tokens')} "
            f"{row['mean_retrieve_seconds']} {row['mean_answer_seconds']}"
        )


def _pending_questions(sample: list[dict], done: dict[str, dict]) -> list[dict]:
    return [
        question for question in sample if str(question["question_id"]) not in done
    ]


def _list_sample(sample: list[dict]) -> None:
    for question in sample:
        print(question["question_id"], longmemeval_data.category_of(question))


def _execute(pending: list[dict], staging: Path, results_path: Path, args) -> None:
    started = time.monotonic()
    finished = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(_run_worker, question, staging, args): question
            for question in pending
        }
        with results_path.open("a", encoding="utf-8") as stream:
            for future in as_completed(futures):
                row = future.result()
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                finished += 1
                elapsed = time.monotonic() - started
                print(
                    f"[{finished}/{len(pending)}] {row.get('question_id')} "
                    f"{row.get('category')} status={row.get('status')} "
                    f"({elapsed:.0f}s elapsed)",
                    flush=True,
                )


def main() -> int:
    args = parse_args()
    data = longmemeval_data.load_dataset()
    sample = _sampled(args, data)
    if args.list_only:
        _list_sample(sample)
        return 0
    results_path = _results_path(args)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    done = _existing_results(results_path)
    pending = _pending_questions(sample, done)
    print(f"sample={len(sample)} done={len(done)} pending={len(pending)}")
    staging = results_path.parent / f"staging-{_run_tag(args)}"
    staging.mkdir(parents=True, exist_ok=True)
    _execute(pending, staging, results_path, args)
    rows = list(_existing_results(results_path).values())
    sample_ids = {str(question["question_id"]) for question in sample}
    scoped = [row for row in rows if str(row.get("question_id")) in sample_ids]
    scoped = _with_judgements(scoped, results_path)
    report = longmemeval_score.aggregate(scoped)
    _report_path(args).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report: {_report_path(args)}")
    _print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
