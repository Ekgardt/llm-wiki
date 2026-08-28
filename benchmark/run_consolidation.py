"""MEM-13: does this vault's consolidation earn its cost? A paired measurement.

Runs each question twice on one vault — once over the raw captured sessions,
once after `episode_consolidation` has appended its durable-item entries — and
reports the difference as a paired comparison (`consolidation_score.py`). The
per-question work is done by `consolidation_vault.py`, one process per question,
for the same reason MEM-10 does it that way: the product resolves its vault
roots at import time.

    uv run python benchmark/run_consolidation.py --sample 24
    uv run python benchmark/run_consolidation.py --sample 12 --type temporal-reasoning

The default slice is `multi-session`, the category MEM-10 scored worst (0.083,
n=12) and the one consolidation is supposed to serve: several sessions about one
thing should become one answer.

Two per-arm JSONL files are written next to the paired stream in the exact row
shape `longmemeval_judge.py` and `longmemeval_score.aggregate` read, so each arm
can be judged and reported with no new scoring code.
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

import consolidation_score  # noqa: E402
import longmemeval_data  # noqa: E402

WORKER = BENCHMARK_DIR / "consolidation_vault.py"
RESULTS_DIR = longmemeval_data.DATASET_DIR
# One question runs the whole pipeline twice plus a nightly consolidation of
# every haystack day; MEM-10's single-arm ceiling of 2400 s is not enough.
QUESTION_TIMEOUT_SECONDS = 5400


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=24, help="questions from the slice")
    parser.add_argument("--type", default="multi-session", help="question_type slice, or 'all'")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--provider", default="claude")
    parser.add_argument("--provider-timeout", type=int, default=240)
    parser.add_argument("--results", default=None, help="paired JSONL stream (resumable)")
    parser.add_argument("--report", default=None, help="paired report JSON")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--keep-vaults", action="store_true")
    parser.add_argument("--list-only", action="store_true", help="print the sample and exit")
    return parser.parse_args()


def slice_of(data: list[dict], question_type: str) -> list[dict]:
    if question_type == "all":
        return list(data)
    return [row for row in data if str(row.get("question_type")) == question_type]


def sample_questions(data: list[dict], args: argparse.Namespace) -> list[dict]:
    """A deterministic draw from one question type; the same flags name the same ids."""
    return longmemeval_data.stratified_sample(
        slice_of(data, args.type), args.sample, args.seed
    )


def _run_tag(args: argparse.Namespace) -> str:
    return f"{args.type}-n{args.sample}-seed{args.seed}"


def _default_path(args: argparse.Namespace, given: str | None, stem: str) -> Path:
    if given:
        return Path(given)
    return RESULTS_DIR / f"{stem}-{_run_tag(args)}"


def _results_path(args: argparse.Namespace) -> Path:
    return _default_path(args, args.results, "consolidation").with_suffix(".jsonl")


def _report_path(args: argparse.Namespace) -> Path:
    return _default_path(args, args.report, "consolidation-report").with_suffix(".json")


def _existing_results(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return {str(row.get("question_id")): row for row in rows}


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


def _harness_failure(question: dict, detail: str) -> dict:
    failed = {"status": "error", "hypothesis": "", "error_kind": "harness_failure"}
    return {
        "question_id": str(question.get("question_id")),
        "question_type": str(question.get("question_type")),
        "category": longmemeval_data.category_of(question),
        "is_abstention": longmemeval_data.is_abstention(question),
        "gold": str(question.get("answer", "")),
        "error": detail[:500],
        "baseline": dict(failed),
        "consolidated": dict(failed),
    }


def _completed_worker(question_file: Path, out_file: Path, args: argparse.Namespace):
    return subprocess.run(
        _worker_command(question_file, out_file, args),
        env=_worker_environment(args),
        cwd=str(BENCHMARK_DIR.parent),
        capture_output=True,
        text=True,
        timeout=QUESTION_TIMEOUT_SECONDS,
    )


def _run_worker(question: dict, staging: Path, args: argparse.Namespace) -> dict:
    """One question in its own process; a stale result is never read as fresh.

    Same rule MEM-10 had to learn: staging survives across runs, so the result
    file is deleted before the worker starts and its presence afterwards is the
    proof that this call wrote it.
    """
    question_id = str(question["question_id"])
    question_file = staging / f"{question_id}.question.json"
    out_file = staging / f"{question_id}.result.json"
    question_file.write_text(json.dumps(question, ensure_ascii=False), encoding="utf-8")
    out_file.unlink(missing_ok=True)
    try:
        completed = _completed_worker(question_file, out_file, args)
    except subprocess.TimeoutExpired:
        return _harness_failure(question, f"worker exceeded {QUESTION_TIMEOUT_SECONDS}s")
    if not out_file.exists():
        return _harness_failure(
            question, f"worker rc={completed.returncode}: {completed.stderr[-400:]}"
        )
    row = json.loads(out_file.read_text(encoding="utf-8"))
    row["worker_exit"] = completed.returncode
    return row


def _progress(row: dict, finished: int, total: int, elapsed: float) -> str:
    arms = " ".join(
        f"{arm}={(row.get(arm) or {}).get('status')}" for arm in consolidation_score.ARMS
    )
    cost = row.get("consolidation") or {}
    return (
        f"[{finished}/{total}] {row.get('question_id')} {arms} "
        f"items={cost.get('items')} calls={cost.get('provider_calls')} "
        f"({elapsed:.0f}s elapsed)"
    )


def _execute(pending: list[dict], staging: Path, results_path: Path, args) -> None:
    started = time.monotonic()
    finished = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(_run_worker, question, staging, args) for question in pending
        ]
        with results_path.open("a", encoding="utf-8") as stream:
            for future in as_completed(futures):
                row = future.result()
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                finished += 1
                elapsed = time.monotonic() - started
                print(_progress(row, finished, len(pending), elapsed), flush=True)


def write_arm_streams(paired_rows: list[dict], results_path: Path) -> list[Path]:
    """One JSONL per arm in the MEM-10 row shape, for the judge and the scorer."""
    written = []
    for arm in consolidation_score.ARMS:
        path = results_path.with_name(f"{results_path.stem}-{arm}.jsonl")
        rows = [consolidation_score.arm_row(paired, arm) for paired in paired_rows]
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        path.write_text(body, encoding="utf-8")
        written.append(path)
    return written


def _print_summary(report: dict) -> None:
    difference = report["difference"]
    for arm in consolidation_score.ARMS:
        row = report[arm]
        print(
            f"{arm} n={row['n']} accuracy={row['accuracy']} f1={row['f1']} "
            f"answered={row['answered']} insufficient={row['insufficient_evidence']} "
            f"tokens={row['mean_est_prompt_tokens']} chunks={row['mean_chunks']}"
        )
    print(
        f"delta={difference['accuracy_delta']} "
        f"baseline_only={difference['baseline_only_correct']} "
        f"consolidated_only={difference['consolidated_only_correct']} "
        f"mcnemar_p={difference['mcnemar_exact_p']}"
    )
    print(f"consolidation cost: {json.dumps(report['consolidation_cost'])}")


def _list_sample(sample: list[dict]) -> int:
    for question in sample:
        print(question["question_id"], question["question_type"])
    return 0


def _sampled_rows(results_path: Path, sample: list[dict]) -> list[dict]:
    sample_ids = {str(question["question_id"]) for question in sample}
    return [
        row
        for row in _existing_results(results_path).values()
        if str(row.get("question_id")) in sample_ids
    ]


def _is_measured(row: dict) -> bool:
    """A row the stand actually produced, as opposed to one it died on.

    A harness failure is not a measurement, so a later run must be free to try
    the question again. Measured 2026-08-28: three concurrent workers each load
    a ~1.1 GiB embedding model and the first question came back `rc=-9` — the
    OOM killer — which, stored as a completed row, would have silently excluded
    that question from every resumed run.
    """
    arms = [consolidation_score.arm_row(row, arm) for arm in consolidation_score.ARMS]
    kinds = {arm.get("error_kind") for arm in arms}
    return kinds != {"harness_failure"}


def _measured_results(results_path: Path) -> dict[str, dict]:
    rows = _existing_results(results_path)
    return {key: row for key, row in rows.items() if _is_measured(row)}


def _run_pending(results_path: Path, sample: list[dict], args: argparse.Namespace) -> None:
    done = _measured_results(results_path)
    pending = [row for row in sample if str(row["question_id"]) not in done]
    print(f"sample={len(sample)} done={len(done)} pending={len(pending)}")
    staging = results_path.parent / f"staging-{_run_tag(args)}"
    staging.mkdir(parents=True, exist_ok=True)
    _execute(pending, staging, results_path, args)


def _publish(results_path: Path, sample: list[dict], args: argparse.Namespace) -> dict:
    rows = _sampled_rows(results_path, sample)
    report = consolidation_score.paired_report(rows)
    _report_path(args).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for path in write_arm_streams(rows, results_path):
        print(f"arm stream: {path}")
    print(f"report: {_report_path(args)}")
    return report


def main() -> int:
    args = parse_args()
    sample = sample_questions(longmemeval_data.load_dataset(), args)
    if args.list_only:
        return _list_sample(sample)
    results_path = _results_path(args)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    _run_pending(results_path, sample, args)
    _print_summary(_publish(results_path, sample, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
