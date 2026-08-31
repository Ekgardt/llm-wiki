"""MEM-15 — does memory lift the answer, or add noise?

Every other stand in this repository measures a step before the answer. The
retrieval stand asks whether the right page is in the top five; the
application stand asks whether the token an operator needs reached the reader.
Neither asks the question a memory system is finally judged on: put the same
question to the same model twice, once with what memory returned and once with
nothing, and is the answer *better*? And — the number that matters — how often
is it *worse*?

So each case is answered twice and graded twice, and the run reports three
fractions rather than one:

    lift     right with memory, wrong without   — memory earned its cost
    neutral  same verdict both ways             — memory changed nothing
    harm     right without memory, wrong with   — memory displaced knowledge

A stand that cannot report harm is not measuring attribution, it is measuring
recall with extra steps.

What is borrowed from arXiv 2605.29630 (Entity-Collision) and what is not is
stated in `docs/research/2026-08-28-does-memory-lift-the-answer.md`. Short
version: the stratification, the collision degree and the paired bootstrap are
reproduced; the paper's testbed, its three embedders and its BM25 floor are
not, because this vault has one pinned encoder and one corpus. This is an
analogue built from this vault's own material, and it is labelled one
everywhere it is reported.

Honesty rails, both load-bearing:

  * The grader never sees the retrieval, the prompt or the confidence envelope
    — see `lift_corpus.grade`. The gold for a world case is the recorded
    output of a command run on this machine; for a vault case it is the token
    the application stand already verified appears verbatim in the gold page.
  * `NEW-122`: at a byte-identical prompt this provider disagrees with itself
    on 2 of 23 questions — 8.7 points. A net lift under that is reported as
    indistinguishable from provider noise, never as a win. `--noise-probe`
    measures that floor again, in-band, on this run's own recorded pools.

    uv run python benchmark/run_lift_attribution.py --pools-out /tmp/pools.json
    uv run python benchmark/run_lift_attribution.py --pools /tmp/pools.json
    uv run python benchmark/run_lift_attribution.py --pools /tmp/pools.json --noise-probe
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
ROOT = BENCHMARK_DIR.parent
for _extra in (ROOT / "scripts", BENCHMARK_DIR):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import lift_corpus  # noqa: E402
from lift_corpus import Case  # noqa: E402
from retrieval_paths import DEFAULT_PATH, PATHS, rows, warm  # noqa: E402

TOP_K = 5
MAX_ANSWER_TOKENS = 220
MAX_NOTE_CHARS = 1800
MAX_NOTES = 5

# Identical in both conditions on purpose. The only difference between the two
# calls is whether a notes block follows the question; an instruction that
# differed as well would confound the thing being attributed.
ANSWER_SYSTEM_PROMPT = (
    "You answer a short factual question. Reply with the answer itself in one "
    "or two sentences, naming the exact value, flag or number asked for. Do "
    "not explain your reasoning. If you do not know, say you do not know."
)

NOTES_HEADER = (
    "Notes retrieved from a knowledge base. They may or may not be relevant to "
    "the question. Use them if they help."
)


def _note_text(row: dict) -> str:
    """Whatever text the product returned with this row, bounded."""
    for field in ("text", "snippet", "excerpt", "content", "preview", "summary"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value[:MAX_NOTE_CHARS]
    return ""


def _note_body(row: dict, vault: Path) -> str:
    """The row's own text, or a bounded head of the page it names."""
    text = _note_text(row)
    if text:
        return text
    return _page_head(vault / str(row.get("path") or ""))


def _page_head(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_NOTE_CHARS]
    except OSError:
        return ""


def render_notes(pool: list[dict], vault: Path) -> str:
    """The memory condition's context, built only from what retrieval returned."""
    parts = []
    for row in pool[:MAX_NOTES]:
        parts.append(f"--- {row.get('path', '?')} ---\n{_note_body(row, vault)}")
    return "\n\n".join(parts)


def with_memory_prompt(case: Case, notes: str) -> str:
    return f"{NOTES_HEADER}\n\n{notes}\n\nQuestion: {case.question}\nAnswer:"


def closed_book_prompt(case: Case) -> str:
    return f"Question: {case.question}\nAnswer:"


def _row_for_pool(row: dict, vault: Path) -> dict:
    return {"path": str(row.get("path") or ""), "text": _note_body(row, vault)}


def _timed_rows(path: str, question: str, limit: int) -> tuple[list[dict], str | None, float]:
    """One retrieval, once. Recording a trace from a second call would describe
    a retrieval the recorded rows did not come from."""
    started = time.monotonic()
    try:
        found = rows(path, question, limit)
    except TimeoutError as exceeded:
        return [], f"{type(exceeded).__name__}: {exceeded}", round(time.monotonic() - started, 3)
    return found, None, round(time.monotonic() - started, 3)


def _pool_for(case: Case, path: str, vault: Path, limit: int) -> dict:
    found, error, seconds = _timed_rows(path, case.question, limit)
    return {
        "case_id": case.case_id,
        "seconds": seconds,
        "error": error,
        "rows": [_row_for_pool(row, vault) for row in found],
    }


def record_pools(cases: list[Case], path: str, vault: Path, limit: int) -> dict:
    """One retrieval per case, kept, so every later comparison uses one pool.

    Recorded 2026-08-26: both vault stands wander between runs of identical
    code because the optional legs are deadline-bound and drop under load.
    Comparing conditions on live re-queries would fold that wander into the
    attribution number, so it is paid once here and never again.
    """
    warm(path)
    pools = {}
    for index, case in enumerate(cases, start=1):
        pools[case.case_id] = _pool_for(case, path, vault, limit)
        print(f"[pool {index}/{len(cases)}] {case.case_id}", flush=True)
    return {"retrieval_path": path, "limit": limit, "pools": pools}


def _provider_call():
    """The shared client, called from a neutral directory.

    `claude -p` loads the working directory's `CLAUDE.md`, and this one
    `@`-imports about 300 KB of operating instructions, which turns a one-line
    factual question into an agent turn about something else. Measured
    2026-08-28: 175 s and a wrong-topic answer from this repository against
    12 s and the right answer from a neutral directory.
    """
    from llm_client import call_llm

    os.chdir(tempfile.mkdtemp(prefix="lift-attribution-"))
    return call_llm


def _answered(call, prompt: str) -> tuple[str, float]:
    started = time.monotonic()
    raw = call(prompt, ANSWER_SYSTEM_PROMPT, MAX_ANSWER_TOKENS)
    return (raw or "").strip(), round(time.monotonic() - started, 2)


def _graded_pair(case: Case, call, notes: str) -> dict:
    without, without_s = _answered(call, closed_book_prompt(case))
    with_mem, with_s = _answered(call, with_memory_prompt(case, notes))
    correct_without = lift_corpus.grade(without, case)
    correct_with = lift_corpus.grade(with_mem, case)
    return {
        "answer_without": without[:600],
        "answer_with": with_mem[:600],
        "correct_without": correct_without,
        "correct_with": correct_with,
        "outcome": lift_corpus.classify(correct_without, correct_with),
        "seconds_without": without_s,
        "seconds_with": with_s,
    }


def _pool_of(pools: dict, case: Case) -> dict:
    return pools.get("pools", {}).get(case.case_id, {"rows": []})


def score_case(case: Case, call, pools: dict, vault: Path) -> dict:
    pool = _pool_of(pools, case)
    notes = render_notes(pool.get("rows", []), vault)
    graded = _graded_pair(case, call, notes)
    return {
        **case.as_dict(),
        **graded,
        "retrieved": [row.get("path") for row in pool.get("rows", [])],
        "retrieval_error": pool.get("error"),
        "notes_chars": len(notes),
    }


def _probe_row(case: Case, call, pools: dict, vault: Path) -> dict:
    """The with-memory leg again, same pool, same bytes. Only the model moves."""
    notes = render_notes(_pool_of(pools, case).get("rows", []), vault)
    answer, seconds = _answered(call, with_memory_prompt(case, notes))
    return {
        "case_id": case.case_id,
        "probe_answer": answer[:600],
        "probe_correct": lift_corpus.grade(answer, case),
        "probe_seconds": seconds,
    }


def _paired(rows_: list[dict], probes: list[dict]) -> list[tuple[dict, dict]]:
    by_id = {row["case_id"]: row for row in rows_}
    present = [probe for probe in probes if probe["case_id"] in by_id]
    return [(by_id[probe["case_id"]], probe) for probe in present]


def _differs(pair: tuple[dict, dict]) -> bool:
    row, probe = pair
    return bool(row["correct_with"]) != bool(probe["probe_correct"])


def _disagreement(rows_: list[dict], probes: list[dict]) -> dict:
    pairs = _paired(rows_, probes)
    return _noise_report(len(pairs), sum(1 for pair in pairs if _differs(pair)))


def _noise_report(total: int, differing: int) -> dict:
    if total == 0:
        return {"n": 0, "disagreements": 0, "points": None}
    return {
        "n": total,
        "disagreements": differing,
        "points": round(100 * differing / total, 2),
    }


def _collision_rows(rows_: list[dict], vault: Path) -> list[dict]:
    for row in rows_:
        probes = tuple(row.get("collision_probes", ()))
        row["collision_degree"] = lift_corpus.collision_degree(vault, probes)
    return rows_


def _thresholds_verdict(corpus: dict, overall: dict) -> dict:
    limits = corpus["thresholds"]
    harm = overall.get("harm_rate")
    net = overall.get("net_lift_rate")
    noisy = lift_corpus.indistinguishable(net, limits["noise_floor_points"])
    return {
        "max_harm_rate": limits["max_harm_rate"],
        "harm_within_limit": _within(harm, limits["max_harm_rate"]),
        "noise_floor_points": limits["noise_floor_points"],
        "net_lift_indistinguishable_from_noise": noisy,
    }


def _within(value: float | None, limit: float) -> bool | None:
    if value is None:
        return None
    return value <= limit


def build_report(corpus: dict, rows_: list[dict], pools: dict, probes: list[dict]) -> dict:
    outcomes = [row["outcome"] for row in rows_]
    overall = lift_corpus.summarise(outcomes)
    return {
        "corpus_id": corpus["corpus_id"],
        "retrieval_path": pools.get("retrieval_path"),
        "overall": overall,
        "net_lift_ci95": lift_corpus.bootstrap_net_ci(outcomes),
        "by_stratum": lift_corpus.by_stratum(rows_),
        "by_collision": lift_corpus.by_collision(rows_),
        "thresholds": _thresholds_verdict(corpus, overall),
        "noise_probe": _disagreement(rows_, probes),
        "cases": rows_,
        "probe_cases": probes,
    }


def _loaded_pools(given: str | None, cases: list[Case], args, vault: Path) -> dict:
    if given:
        return json.loads(Path(given).read_text(encoding="utf-8"))
    return record_pools(cases, args.path, vault, args.limit)


def _saved(pools: dict, out: str | None) -> None:
    if not out:
        return
    Path(out).write_text(json.dumps(pools, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"pools: {out}", flush=True)


def _scored(cases: list[Case], call, pools: dict, vault: Path) -> list[dict]:
    scored = []
    for index, case in enumerate(cases, start=1):
        row = score_case(case, call, pools, vault)
        scored.append(row)
        print(f"[{index}/{len(cases)}] {case.case_id} {row['outcome']}", flush=True)
    return scored


def _probed(cases: list[Case], call, pools: dict, vault: Path, enabled: bool) -> list[dict]:
    if not enabled:
        return []
    return [_probe_row(case, call, pools, vault) for case in cases]


def _regraded_row(row: dict, case: Case) -> dict:
    """The same recorded answers, judged by the current rubric."""
    without = lift_corpus.grade(row.get("answer_without"), case)
    with_memory = lift_corpus.grade(row.get("answer_with"), case)
    return {
        **row,
        **case.as_dict(),
        "correct_without": without,
        "correct_with": with_memory,
        "outcome": lift_corpus.classify(without, with_memory),
    }


def regraded(cases: list[Case], report_path: str) -> list[dict]:
    """Re-judge a finished run from its recorded answers, with no provider call.

    Recording every answer is what makes a rubric correction honest rather than
    a re-roll: the answers do not move, only the judgement does, so the before
    and the after are comparable and both can be published.
    """
    stored = json.loads(Path(report_path).read_text(encoding="utf-8"))["cases"]
    by_id = {case.case_id: case for case in cases}
    known = [row for row in stored if row.get("case_id") in by_id]
    return [_regraded_row(row, by_id[row["case_id"]]) for row in known]


def _baseline_rows(path: str | None, cases: list[Case], call, pools: dict, vault: Path) -> list[dict]:
    """Scored cases from an earlier report, or a fresh scoring pass.

    Reusing them is how the noise floor gets measured without paying for the
    whole stand twice: the probe repeats only the memory leg, on the same
    recorded pool, and the flips against the recorded verdict are this
    provider's disagreement with itself at a byte-identical prompt.
    """
    if not path:
        return _collision_rows(_scored(cases, call, pools, vault), vault)
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MEM-15 lift/neutral/harm attribution")
    parser.add_argument("--path", default=DEFAULT_PATH, choices=PATHS)
    parser.add_argument("--limit", type=int, default=TOP_K)
    parser.add_argument("--pools", default=None, help="replay recorded retrieval pools")
    parser.add_argument("--pools-out", default=None, help="write the pools this run recorded")
    parser.add_argument("--report", default=None, help="report JSON path")
    parser.add_argument("--cases", type=int, default=0, help="first N cases only (smoke)")
    parser.add_argument("--noise-probe", action="store_true", help="repeat the memory leg")
    parser.add_argument("--baseline", default=None, help="reuse an earlier report's scored cases")
    parser.add_argument("--regrade", default=None, help="re-judge a finished report, no provider calls")
    return parser.parse_args()


def _selected(cases: list[Case], count: int) -> list[Case]:
    if count <= 0:
        return cases
    return cases[:count]


def _regrade_only(args, corpus: dict, cases: list[Case], report_path: Path | None) -> int:
    rows_ = regraded(cases, args.regrade)
    _emit(build_report(corpus, rows_, {"retrieval_path": "regraded"}, []), report_path)
    return 0


def main() -> int:
    args = parse_args()
    vault = ROOT
    corpus = lift_corpus.load_corpus()
    cases = _selected(lift_corpus.all_cases(corpus), args.cases)
    report_path = Path(args.report).resolve() if args.report else None
    if args.regrade:
        return _regrade_only(args, corpus, cases, report_path)
    pools = _loaded_pools(args.pools, cases, args, vault)
    _saved(pools, args.pools_out)
    call = _provider_call()
    rows_ = _baseline_rows(args.baseline, cases, call, pools, vault)
    probes = _probed(cases, call, pools, vault, args.noise_probe)
    report = build_report(corpus, rows_, pools, probes)
    _emit(report, report_path)
    return 0


def _emit(report: dict, path: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if path:
        path.write_text(text, encoding="utf-8")
        print(f"report: {path}", flush=True)
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
