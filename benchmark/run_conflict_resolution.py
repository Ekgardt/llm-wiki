"""MEM-14: does this vault's supersession pick the current value without an LLM?

The claim under test is narrow and it is the whole point: **no LLM takes part in
conflict resolution**. The stand proves or refutes it by holding the candidate
set fixed and varying only who resolves it.

Three arms see byte-identical candidates, taken from
`benchmark/conflict-resolution-v1.json`, which is derived from the
MemoryAgentBench FactConsolidation row by a deterministic frame table
(`factconsolidation_data.py`) with no model anywhere on that path:

* `vault` — this vault's own rule. Each observation becomes a `claim/v1` record
  whose `observed_at` carries the serial, and `bitemporal_claims.as_of` decides
  which belief survives. It may refuse; a refusal is scored as an abstention,
  never as an answer.
* `argmax` — the paper's resolver, `argmax(serial)`, which cannot abstain.
  Included so the difference between the two rules is visible rather than
  argued: on a total order they agree, and they can only diverge on a tie.
* `llm` — the baseline the claim is against: the same candidates handed to the
  provider, told that newer facts carry larger serials, asked for the current
  value. This mirrors the published LLM-judgment baseline, but at the resolver
  rather than at the pipeline, which is the isolation arXiv 2606.01435 calls
  "left to future work".

Scoring is the benchmark's own SubEM: an answer counts when the expected value
appears in it, case-folded. An abstention is reported separately and is *not*
counted as correct — the paper counts "no answer" as wrong under SubEM, and so
does the `accuracy` field here; `abstention_rate` is reported beside it so a
calibrated refusal is not confused with a wrong answer.

Ties are the interesting axis. This vault does not read a serial; it reads
`observed_at`, which the compile pipeline binds to the daily block the evidence
came from. Several claims lifted from one block therefore share an instant, and
the rule refuses (`bitemporal_ambiguous_observation`) rather than invent an
order. `--block-size` models exactly that: k consecutive serials collapse into
one observation instant. k=1 is the paper's total order; larger k is what a real
vault looks like when a compile part yields several claims at once.

Usage:
    uv run python benchmark/run_conflict_resolution.py --arms vault,argmax
    uv run python benchmark/run_conflict_resolution.py --arms vault,argmax,llm
    uv run python benchmark/run_conflict_resolution.py --sweep
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bitemporal_claims import BitemporalRefusal, as_of  # noqa: E402
from claims import _normalize_value  # noqa: E402
from factconsolidation_data import (  # noqa: E402
    Conflict,
    fixture_conflicts,
    load_fixture,
)

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "conflict-resolution-v1.json"
# Serial 0 lands here; serial n lands n // block_size seconds later. The date is
# arbitrary and only the order it induces is used.
EPOCH = "2026-01-01T00:00:00Z"
# Later than any observation this stand can build, so "what is true now" is what
# survived every supersession in the stream.
FAR_FUTURE = "2099-01-01T00:00:00Z"
SWEEP_BLOCK_SIZES = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512)

CORRECT = "correct"
WRONG = "wrong"
ABSTAIN = "abstain"


@dataclass(frozen=True)
class Outcome:
    """What one arm did with one conflict."""

    answer: str | None
    refusal: str | None = None
    seconds: float = 0.0


@dataclass
class ArmResult:
    """One arm's tally over the conflict set."""

    arm: str
    verdicts: list[str] = field(default_factory=list)
    refusals: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    # One row per conflict, so two runs of the same arm can be compared item by
    # item. Without it a provider's disagreement with itself is invisible, and
    # `NEW-122` measured that disagreement at 8.7 points on 23 questions.
    items: list[dict[str, object]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.verdicts)

    @property
    def correct(self) -> int:
        return self.verdicts.count(CORRECT)

    @property
    def wrong(self) -> int:
        return self.verdicts.count(WRONG)

    @property
    def abstained(self) -> int:
        return self.verdicts.count(ABSTAIN)

    def as_json(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "n": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "abstain": self.abstained,
            "accuracy": _ratio(self.correct, self.total),
            "abstention_rate": _ratio(self.abstained, self.total),
            "wrong_rate": _ratio(self.wrong, self.total),
            "accuracy_when_answered": _ratio(
                self.correct, self.correct + self.wrong
            ),
            "refusals": dict(sorted(self.refusals.items())),
            "seconds": round(self.seconds, 2),
            "items": self.items,
        }


def _ratio(part: int, whole: int) -> float | None:
    if whole == 0:
        return None
    return round(part / whole, 4)


def observed_at(serial: int, block_size: int) -> str:
    """The instant this vault would carry for a fact at that serial."""
    if block_size < 1:
        raise ValueError("block size must be at least 1")
    offset = serial // block_size
    return f"2026-01-01T{offset // 3600:02d}:{offset // 60 % 60:02d}:{offset % 60:02d}Z"


def claim_records(item: Conflict, block_size: int) -> list[dict[str, object]]:
    """Each observation as the `claim/v1` record this vault would hold."""
    return [
        _record(item, serial, value, block_size)
        for serial, value in item.observations
    ]


def _record(
    item: Conflict, serial: int, value: str, block_size: int
) -> dict[str, object]:
    return {
        "id": f"{item.key}#{serial}",
        "subject": item.subject,
        # Single-valued in this vault's partition, so a later value invalidates
        # an earlier one. The MQUAKE property rides in the qualifiers, which are
        # part of the bitemporal key.
        "relation": "has-value",
        "value": _normalize_value({"type": "entity", "value": value}),
        "qualifiers": [
            {"key": "property", "value": {"type": "string", "value": item.prop}}
        ],
        "observed_at": observed_at(serial, block_size),
        "validity": {"from": None, "to": None},
        "lifecycle": "active",
    }


def resolve_vault(item: Conflict, block_size: int) -> Outcome:
    """This vault's rule, refusing by name rather than guessing an order."""
    records = claim_records(item, block_size)
    started = time.monotonic()
    try:
        surviving = as_of(records, valid_at=FAR_FUTURE)
    except BitemporalRefusal as exc:
        return Outcome(None, _refusal_name(exc), time.monotonic() - started)
    return _single_survivor(surviving, time.monotonic() - started)


def _refusal_name(exc: BitemporalRefusal) -> str:
    return str(exc).split(":", 1)[0].strip()


def _single_survivor(surviving: Sequence[object], seconds: float) -> Outcome:
    if len(surviving) != 1:
        return Outcome(None, f"survivors_{len(surviving)}", seconds)
    record = surviving[0].record
    assert isinstance(record, Mapping)
    value = record["value"]
    assert isinstance(value, Mapping)
    return Outcome(str(value["value"]), None, seconds)


def resolve_argmax(item: Conflict, _block_size: int = 1) -> Outcome:
    """The paper's resolver: the highest serial wins, and it never abstains."""
    started = time.monotonic()
    highest = max(item.observations, key=lambda pair: pair[0])
    return Outcome(highest[1], None, time.monotonic() - started)


def resolve_argmax_observed(item: Conflict, block_size: int) -> Outcome:
    """`argmax` over the clock this vault actually has, instead of a serial.

    The paper's rule needs a total order and says so: "The approach generalizes
    to any total order but cannot handle partial orders." This vault's order
    comes from `observed_at`, which ties whenever two claims are lifted from one
    daily block. This arm is what a naive "latest timestamp wins" does with such
    a tie — `max` keeps the first maximum it saw, so the stale value is returned
    with no sign that anything was ambiguous. It exists to make the cost of not
    abstaining visible next to the vault's refusal.
    """
    started = time.monotonic()
    stamped = [
        (observed_at(serial, block_size), value)
        for serial, value in item.observations
    ]
    highest = max(stamped, key=lambda pair: pair[0])
    return Outcome(highest[1], None, time.monotonic() - started)


def llm_prompt(item: Conflict) -> str:
    """The same candidates, phrased as the published LLM-judgment baseline."""
    lines = [
        f"{serial}. {_sentence(item, value)}" for serial, value in item.observations
    ]
    facts = "\n".join(lines)
    return (
        "Here is a list of facts. Newer facts have larger serial numbers, so "
        "when two facts disagree the one with the larger serial number is the "
        "current one.\n\n"
        f"{facts}\n\n"
        f"Question: currently, what is the {item.prop} of {item.subject}?\n"
        "Answer with the value only, on one line, with no explanation. "
        "If the facts do not answer the question, reply exactly: NO ANSWER"
    )


def _sentence(item: Conflict, value: str) -> str:
    return f"The {item.prop} of {item.subject} is {value}."


def resolve_llm(item: Conflict, call: Callable[..., object]) -> Outcome:
    """The baseline the no-LLM claim is measured against."""
    started = time.monotonic()
    reply = call(llm_prompt(item), "", 100)
    seconds = time.monotonic() - started
    return _parsed_reply(reply, seconds)


def _parsed_reply(reply: object, seconds: float) -> Outcome:
    if not isinstance(reply, str) or not reply.strip():
        return Outcome(None, "provider_empty", seconds)
    text = reply.strip().splitlines()[0].strip()
    if text.casefold().startswith("no answer"):
        return Outcome(None, "llm_no_answer", seconds)
    return Outcome(text, None, seconds)


def verdict(outcome: Outcome, truth: str) -> str:
    """SubEM: the expected value must appear in the answer, case-folded."""
    if outcome.answer is None:
        return ABSTAIN
    if truth.strip().casefold() in outcome.answer.strip().casefold():
        return CORRECT
    return WRONG


def run_arm(
    arm: str,
    items: Sequence[Conflict],
    resolver: Callable[[Conflict], Outcome],
) -> ArmResult:
    """One arm over the whole conflict set, tallying refusals by name."""
    result = ArmResult(arm)
    for item in items:
        _record_outcome(result, item, resolver(item))
    return result


def _record_outcome(result: ArmResult, item: Conflict, outcome: Outcome) -> None:
    scored = verdict(outcome, item.truth)
    result.verdicts.append(scored)
    result.seconds += outcome.seconds
    result.items.append(
        {
            "key": item.key,
            "truth": item.truth,
            "answer": outcome.answer,
            "refusal": outcome.refusal,
            "verdict": scored,
        }
    )
    if outcome.refusal is None:
        return
    result.refusals[outcome.refusal] = result.refusals.get(outcome.refusal, 0) + 1


def provider_call() -> Callable[..., object]:
    """The shared client, called from a neutral directory.

    `claude -p` loads the working directory's `CLAUDE.md`, and this vault's
    imports about 300 KB of operating instructions, which turns a one-value
    resolver call into an agent turn about something else — measured 2026-08-28
    at 175s from this repository against 12.59s from a neutral directory
    (`benchmark/longmemeval_judge.py`). Every provider call in this stand runs
    after this chdir.
    """
    from llm_client import call_llm

    os.chdir(tempfile.mkdtemp(prefix="conflict-resolution-"))
    return call_llm


def sweep(items: Sequence[Conflict], sizes: Sequence[int]) -> list[dict[str, object]]:
    """Abstention against observation granularity: the tie axis, measured."""
    rows = []
    for size in sizes:
        result = run_arm("vault", items, lambda item, k=size: resolve_vault(item, k))
        rows.append({"block_size": size, **result.as_json()})
    return rows


def _selected(items: Sequence[Conflict], only_gold: bool) -> tuple[Conflict, ...]:
    if not only_gold:
        return tuple(items)
    return tuple(item for item in items if item.gold_confirmed)


def _arm_runners(
    names: Sequence[str], block_size: int
) -> list[tuple[str, Callable[[Conflict], Outcome]]]:
    table: dict[str, Callable[[], Callable[[Conflict], Outcome]]] = {
        "vault": lambda: (lambda item: resolve_vault(item, block_size)),
        "argmax": lambda: resolve_argmax,
        "argmax_observed": lambda: (
            lambda item: resolve_argmax_observed(item, block_size)
        ),
        "llm": _llm_runner,
    }
    return [(name, table[name]()) for name in names if name in table]


def _llm_runner() -> Callable[[Conflict], Outcome]:
    call = provider_call()
    return lambda item: resolve_llm(item, call)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--arms", default="vault,argmax")
    parser.add_argument("--block-size", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--gold-confirmed", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    # Fixed before any provider call moves the cwd out of this repository.
    args.fixture = args.fixture.resolve()
    args.out = args.out.resolve() if args.out is not None else None
    document = load_fixture(args.fixture)
    items = _selected(fixture_conflicts(document), args.gold_confirmed)
    items = items[: args.limit] if args.limit > 0 else items
    report = _report(document, items, args)
    _emit(report, args.out)
    return 0


def _report(
    document: Mapping[str, object], items: Sequence[Conflict], args: object
) -> dict[str, object]:
    names = [name.strip() for name in str(getattr(args, "arms")).split(",")]
    report: dict[str, object] = {
        "schema_version": "conflict-resolution-report/v1",
        "row_id": document.get("row_id"),
        "source_sha256": document.get("source_sha256"),
        "parsed_facts": document.get("parsed_facts"),
        "unparsed_lines": len(document.get("unparsed_lines") or []),
        "conflicts_scored": len(items),
        "gold_confirmed_only": bool(getattr(args, "gold_confirmed")),
        "block_size": int(getattr(args, "block_size")),
        "arms": [
            run_arm(name, items, runner).as_json()
            for name, runner in _arm_runners(names, int(getattr(args, "block_size")))
        ],
    }
    if getattr(args, "sweep"):
        report["sweep"] = sweep(items, SWEEP_BLOCK_SIZES)
    return report


def _emit(report: Mapping[str, object], out: Path | None) -> None:
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    print(text)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
