"""Let a person confirm the machine labels the classification corpus carries.

`OPEN-034` is blocked on a step nobody could take. `build_flush_corpus.py` labels
real sessions with a rubric, and the labels turned out unstable: the same 40
sessions relabelled with a different excerpt length moved from 20/6/14 to 5/3/32.
So no accuracy number measured against them can be published as a fact. The
corpus already carries `label_provenance` and `label_reviewed`, and the runner
already calls a run with unreviewed labels provisional — but nothing let the
owner actually review one.

This is that command. It walks unreviewed cases one at a time, shows enough of
the session to judge, and never shows the machine's label until after the answer:
showing it first would anchor the judgement and destroy the agreement number this
exists to produce. Each answer is appended and fsynced before the next case, so a
closed terminal costs one case at most.

How many need reviewing. Agreement is reported as Cohen's kappa. Bujang and
Baharum (2017), "Guidelines of the minimum sample size requirements for Kappa
agreement test", derive minimum sizes from the Flack (1988) formula at 80% power
and alpha 0.05; the required n runs from a handful to nearly 700 depending on the
expected kappa and the marginal frequencies, and they warn that the formula
returns implausibly small numbers under favourable assumptions — anything below
about 30 should be treated as unsafe. That gives the floor here: below
`KAPPA_MINIMUM_CASES` reviewed cases this command refuses to report a kappa at
all and says so. The same guideline says to double the estimate when the two
raters' marginal frequencies differ, which is exactly this corpus's situation
(20/6/14 against 5/3/32 is badly unequal), so the default subsample is twice the
floor: `DEFAULT_SAMPLE` cases, about half an hour at thirty seconds each.

The subsample is seeded and drawn from the whole corpus before answered cases are
removed, so resuming a review continues the same sample rather than drawing a new
one. Interpretation bands are the Landis and Koch (1977) benchmarks.

Verdicts go to a sidecar beside the corpus. It holds case identifiers and tiers
only — never session text — and its name is denied by `.gitignore` for the same
reason the corpus is.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmark"))

from run_flush_classification import load_corpus  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))

from session_evidence import render_transcript  # noqa: E402

DEFAULT_CORPUS = ROOT / "benchmark/flush-classification-live.json"

# Below this many reviewed cases a kappa is not reported at all; see the module
# docstring for the source.
KAPPA_MINIMUM_CASES = 30
DEFAULT_SAMPLE = 60

# One case has to fit on a screen, so a long session is shown head and tail.
HEAD_CHARS = 1200
TAIL_CHARS = 800

TIERS = ("major", "minor", "ok")

ANSWERS = {
    "m": "major",
    "major": "major",
    "n": "minor",
    "minor": "minor",
    "o": "ok",
    "ok": "ok",
    "s": "skip",
    "skip": "skip",
    "q": "quit",
    "quit": "quit",
}

PROMPT = "  keeps [m]uch / [n] little / [o] nothing — or [s]kip, [q]uit: "

# Landis and Koch (1977) benchmarks, as upper bounds.
KAPPA_BANDS = (
    (0.00, "no better than chance"),
    (0.20, "slight"),
    (0.40, "fair"),
    (0.60, "moderate"),
    (0.80, "substantial"),
)

RULE = "=" * 72


def default_verdicts_path(corpus_path: Path) -> Path:
    """The sidecar beside the corpus; `.gitignore` denies this name."""
    return corpus_path.with_name(corpus_path.stem + ".review.jsonl")


def excerpt(transcript: str) -> str:
    """Head and tail of one session as conversation, bounded to fit a screen.

    The corpus holds the session's raw JSONL tail; a reviewer read 1200
    characters of record metadata and no words of the conversation (the owner's
    first run, 2026-09-23). The session-evidence renderer turns the lines into
    the conversation verbatim, tool calls as one line each; anything that is
    not JSONL is kept as it is.
    """
    text = (render_transcript(transcript) or transcript).strip()
    if len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    middle = f"\n\n… [{omitted} characters omitted] …\n\n"
    return text[:HEAD_CHARS] + middle + text[-TAIL_CHARS:]


def render_case(case: dict, position: int, total: int) -> str:
    """What the reviewer sees. Deliberately carries no machine judgement.

    Not shown: `expected_tier`, `required_markers` and `content_classes`. All
    three are the judge's opinion, and seeing any of them before answering would
    anchor the reviewer — which is the one thing this command must not do.
    """
    return (
        f"\n{RULE}\n"
        f"[{position}/{total}] {case['case_id']}  "
        f"({case['language']}, {case['event']})\n"
        f"{'-' * 72}\n"
        f"{excerpt(str(case['transcript']))}\n"
    )


def read_answer(stream, write) -> str:
    """One of major, minor, ok, skip, quit. End of input counts as quit."""
    while True:
        write(PROMPT)
        line = stream.readline()
        if not line:
            write("\n")
            return "quit"
        answer = ANSWERS.get(line.strip().casefold())
        if answer is not None:
            return answer
        write("  answer with m, n, o, s or q\n")


def build_record(case: dict, answer: str) -> dict:
    return {
        "case_id": str(case["case_id"]),
        "human_tier": answer,
        "machine_tier": str(case["expected_tier"]),
        "machine_provenance": str(case.get("label_provenance", "judge")),
        "reviewed_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def append_verdict(path: Path, record: dict) -> None:
    """Durable before the next case is shown: a closed terminal costs one case."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def load_verdicts(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        records[str(record["case_id"])] = record
    return records


def reveal(record: dict) -> str:
    agreement = "agrees" if record["human_tier"] == record["machine_tier"] else "differs"
    return (
        f"  recorded. The machine said {record['machine_tier']}; "
        f"your answer {agreement}.\n"
    )


def _seeded_subset(cases: list[dict], sample: int | None, seed: int) -> list[dict]:
    """Drawn from the whole corpus, so resuming continues the same sample."""
    if sample is None or sample >= len(cases):
        return list(cases)
    ordered = sorted(cases, key=lambda case: str(case["case_id"]))
    return random.Random(seed).sample(ordered, sample)


def select_cases(
    cases: list[dict], verdicts: dict[str, dict], *, sample: int | None, seed: int
) -> list[dict]:
    chosen = _seeded_subset(cases, sample, seed)
    return [case for case in chosen if str(case["case_id"]) not in verdicts]


def _share(pairs: list[tuple[str, str]], index: int, tier: str) -> float:
    return sum(1 for pair in pairs if pair[index] == tier) / len(pairs)


def _observed_agreement(pairs: list[tuple[str, str]]) -> float:
    matched = sum(1 for human, machine in pairs if human == machine)
    return matched / len(pairs)


def _expected_agreement(pairs: list[tuple[str, str]]) -> float:
    return sum(_share(pairs, 0, tier) * _share(pairs, 1, tier) for tier in TIERS)


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Chance-corrected agreement between the human and the machine label."""
    if not pairs:
        return None
    expected = _expected_agreement(pairs)
    if expected >= 1.0:
        return None
    return (_observed_agreement(pairs) - expected) / (1.0 - expected)


def kappa_label(value: float) -> str:
    for upper, name in KAPPA_BANDS:
        if value <= upper:
            return name
    return "almost perfect"


def reviewed_pairs(
    cases: list[dict], verdicts: dict[str, dict]
) -> list[tuple[str, str]]:
    identifiers = {str(case["case_id"]) for case in cases}
    return [
        (str(record["human_tier"]), str(record["machine_tier"]))
        for record in verdicts.values()
        if str(record["case_id"]) in identifiers
    ]


def agreement_line(pairs: list[tuple[str, str]]) -> str:
    if len(pairs) < KAPPA_MINIMUM_CASES:
        return (
            f"agreement: not reported — {len(pairs)} reviewed case(s) is too few; "
            f"a kappa needs at least {KAPPA_MINIMUM_CASES}"
        )
    value = cohens_kappa(pairs)
    if value is None:
        return "agreement: kappa is undefined — every label falls in one category"
    return f"agreement: Cohen's kappa {value:.3f} ({kappa_label(value)})"


def summary_lines(cases: list[dict], verdicts: dict[str, dict]) -> list[str]:
    pairs = reviewed_pairs(cases, verdicts)
    return [
        RULE,
        f"reviewed: {len(pairs)} of {len(cases)} case(s)",
        f"remaining: {len(cases) - len(pairs)}",
        agreement_line(pairs),
    ]


def opening_lines(cases: list[dict], pending: list[dict]) -> list[str]:
    return [
        RULE,
        f"corpus: {len(cases)} case(s); {len(pending)} left in this sample",
        "Judge what a reader would still need a month later. The machine's own",
        "label is shown only after you answer.",
    ]


def review(
    pending: list[dict],
    verdicts: dict[str, dict],
    verdicts_path: Path,
    stream,
    write,
) -> None:
    for position, case in enumerate(pending, start=1):
        write(render_case(case, position, len(pending)))
        answer = read_answer(stream, write)
        if answer == "quit":
            break
        if answer == "skip":
            continue
        record = build_record(case, answer)
        append_verdict(verdicts_path, record)
        verdicts[record["case_id"]] = record
        write(reveal(record))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--verdicts", type=Path, default=None, help="default: beside the corpus"
    )
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    parser.add_argument(
        "--all", action="store_true", help="review every case, not a subsample"
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    if args.all:
        args.sample = None
    if args.verdicts is None:
        args.verdicts = default_verdicts_path(args.corpus)
    return args


def main(argv: list[str] | None = None, stream=None, write=None) -> int:
    args = parse_args(argv)
    stream = stream or sys.stdin
    write = write or _flushing_write
    corpus = load_corpus(args.corpus)
    verdicts = load_verdicts(args.verdicts)
    pending = select_cases(
        corpus["cases"], verdicts, sample=args.sample, seed=args.seed
    )
    _write_lines(write, opening_lines(corpus["cases"], pending))
    review(pending, verdicts, args.verdicts, stream, write)
    _write_lines(write, summary_lines(corpus["cases"], verdicts))
    return 0


def _flushing_write(text: str) -> None:
    """The prompt has no newline; unflushed, it stayed in the buffer while the
    reviewer waited for it (the owner's first run, 2026-09-23)."""
    sys.stdout.write(text)
    sys.stdout.flush()


def _write_lines(write, lines: list[str]) -> None:
    for line in lines:
        write(line + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
