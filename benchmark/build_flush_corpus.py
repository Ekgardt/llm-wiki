"""Build a classification corpus from this machine's real sessions.

`OPEN-034` asked how often a session holding a decision, a fix or a gotcha loses
it on the way into durable memory. The measurement stand existed; the corpus did
not — the one shipped in the repository is nine synthetic public cases, and the
register said a real number was impossible here because there was no installed
runtime. That stopped being true when the vault and the source became one
directory: the real transcripts are on this machine.

Each case carries exactly the text the product classifies: the whole transcript
rendered as conversation and bounded the way `flush_memory` bounds it. Until
2026-09-23 the corpus held the last 60 000 characters of raw host JSONL, which
the product had stopped reading on 2026-09-06; the judge then read injected
memory and rule text and called 37 of 40 sessions major.

Labels are produced by two readings that never see the product's tier names or
its classification prompt, so the corpus is not the system agreeing with itself.
The rubric reading asks what the session keeps. The quote reading asks for the
one passage a reader would still need a month later, verbatim, and the quote is
checked against the transcript: a quote that is not there invalidates the
reading. A case is confirmed when both readings name the same tier and contested
otherwise; contested cases stay in the corpus with both readings recorded and
the stand excludes them from every denominator. Nobody reviews labels by hand
(the owner's rule, 2026-09-23); the labels are ai-derived and say so.

The built corpus holds real session text and is never committed.

See docs/research/2026-08-23-labelling-real-sessions-for-classification.md and
docs/research/2026-09-23-the-corpus-labels-itself.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from flush_memory import (  # noqa: E402
    MAX_RECORD_CHARS,
    _bounded_classifier_evidence,
    _readable_evidence,
)
from reliable_memory import validate_schema  # noqa: E402

SCHEMA = ROOT / "benchmark/flush-classification-v3.schema.json"
DEFAULT_OUT = ROOT / "benchmark/flush-classification-live.json"
JUDGE_MAX_TOKENS = 900

# Deliberately not the product's vocabulary: neither reading names FLUSH_MAJOR,
# FLUSH_MINOR or FLUSH_OK, so a case is not labelled by the thing being measured.
JUDGE_SYSTEM_PROMPT = (
    "You read a software work session and report only what a reader would still "
    "need a month later. You never invent content. You answer with JSON alone."
)

RUBRIC_PROMPT = """Read this work session and answer three questions about it.

1. Does it contain a durable decision, a reusable lesson, or a non-obvious
   command or snippet worth keeping across sessions?
2. Failing that, does it contain a debugging gotcha (symptom to cause), an open
   question worth returning to, or a single useful observation?
3. Which short terms would have to survive into a one-paragraph summary for it
   to be faithful? Quote at most three, verbatim from the session, each at most
   five words — a name, an identifier, a file, a version, a number, a flag. Not
   whole sentences: a summary rewrites sentences and keeps terms. If the session
   carries nothing durable, return an empty list.

Answer with JSON only, no prose, in exactly this shape:
{{"keeps": "much" | "little" | "nothing", "kinds": [...], "phrases": [...]}}

`kinds` uses only these words: decision, lesson, command, gotcha, question.

=== SESSION ===
{transcript}
=== END ==="""

QUOTE_PROMPT = """Read this work session. Find the one passage a reader would still need a
month later, and copy it out exactly as it stands — one to three consecutive
sentences, verbatim, no paraphrase, no ellipsis.

Then say what the passage is:
- "much" if it states a decision with its reason, a reusable lesson, or a
  non-obvious command or snippet;
- "little" if it states only a debugging gotcha, an open question, or a single
  useful observation;
- "nothing" if no such passage exists — then the quote is an empty string.

Progress reports, plans, and descriptions of what was done are not passages a
reader needs later.

Answer with JSON only, no prose, in exactly this shape:
{{"quote": "...", "keeps": "much" | "little" | "nothing"}}

=== SESSION ===
{transcript}
=== END ==="""

KEEP_TO_TIER = {"much": "major", "little": "minor", "nothing": "ok"}
KNOWN_KINDS = {"decision", "lesson", "command", "gotcha", "question"}
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
WHITESPACE = re.compile(r"\s+")
SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")
# A sentence shorter than this is a term, not a passage, and proves nothing.
MIN_QUOTE_WORDS = 4
MAX_PASSAGE_CHARS = 1000
# Below this many cases a kappa between the two readings is not reported; see
# Bujang and Baharum (2017), cited in docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md.
KAPPA_MINIMUM_CASES = 30
TIERS = ("major", "minor", "ok")


@dataclass(frozen=True)
class Verdict:
    tier: str
    kinds: tuple[str, ...]
    phrases: tuple[str, ...]


def _ask(prompt: str) -> str:
    from llm_client import call_llm

    return call_llm(prompt, JUDGE_SYSTEM_PROMPT, max_tokens=JUDGE_MAX_TOKENS) or ""


def _judge(transcript: str) -> str:
    return _ask(RUBRIC_PROMPT.format(transcript=transcript))


def _quote_reader(transcript: str) -> str:
    return _ask(QUOTE_PROMPT.format(transcript=transcript))


def _json_object(text: str) -> dict:
    """The first JSON object in the answer; commentary after it is ignored.

    One quote reading of 2026-09-23 answered its JSON and then argued with
    itself in prose that held braces; first-to-last brace lost the case.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("judge did not answer with JSON")
    document, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(document, dict):
        raise ValueError("judge did not answer with a JSON object")
    return document


def _kinds_of(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    named = [str(item).strip().casefold() for item in value]
    return tuple(sorted({item for item in named if item in KNOWN_KINDS}))


MAX_MARKER_WORDS = 5


def _quoted_in(phrase: str, transcript: str) -> bool:
    """A marker has to be short enough to survive a rewrite, and really present.

    The metric asks whether the product's summary still contains the marker. A
    quoted sentence answers a different question — summaries rewrite sentences —
    so anything longer than a term is dropped rather than counted as a loss.
    """
    if not phrase or len(phrase.split()) > MAX_MARKER_WORDS:
        return False
    return phrase in transcript


def _phrases_of(value: object, transcript: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    quoted = [str(item).strip() for item in value]
    return tuple(item for item in quoted if _quoted_in(item, transcript))[:3]


def _tier_of(document: dict) -> str:
    tier = KEEP_TO_TIER.get(str(document.get("keeps", "")).strip().casefold())
    if tier is None:
        raise ValueError("judge did not name what the session keeps")
    return tier


def parse_verdict(text: str, transcript: str) -> Verdict:
    document = _json_object(text)
    return Verdict(
        tier=_tier_of(document),
        kinds=_kinds_of(document.get("kinds")),
        phrases=_phrases_of(document.get("phrases"), transcript),
    )


def _normalised(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def _sentences(passage: str) -> list[str]:
    return [part.strip() for part in SENTENCE_END.split(passage) if part.strip()]


def quote_is_verbatim(quote: str, transcript: str) -> bool:
    """A passage counts only if the transcript really carries it.

    Sentence by sentence: of seven quotes voided on 2026-09-23, four stitched
    two real sentences that were not consecutive. A quote with one whole
    sentence of the session in it is grounded; one with none is invented.
    """
    text = _normalised(transcript)
    return any(
        len(sentence.split()) >= MIN_QUOTE_WORDS and sentence in text
        for sentence in _sentences(_normalised(quote))
    )


@dataclass(frozen=True)
class QuoteReading:
    tier: str | None
    passage: str


def parse_quote_reading(text: str, transcript: str) -> QuoteReading:
    """The tier the quote reading supports, or None when its quote is not there.

    A reading that keeps nothing needs no quote. One that keeps something must
    show the passage; a passage the transcript does not contain is an invented
    one, and the reading is void rather than wrong. The passage is kept so a
    void reading can be inspected without asking the model again.
    """
    document = _json_object(text)
    tier = _tier_of(document)
    passage = _normalised(str(document.get("quote", "")))[:MAX_PASSAGE_CHARS]
    if tier == "ok":
        return QuoteReading(tier, passage)
    if quote_is_verbatim(passage, transcript):
        return QuoteReading(tier, passage)
    return QuoteReading(None, passage)


def _language(transcript: str) -> str:
    if CYRILLIC.search(transcript):
        return "RU"
    return "EN"


def _case_id(path: Path, index: int) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", path.stem.casefold()).strip("-")
    return f"live-{index:03d}-{stem[:40]}".strip("-")


def _markers(verdict: Verdict) -> list[str]:
    if verdict.tier == "ok":
        return []
    return [phrase[:200] for phrase in verdict.phrases]


def label_status(rubric_tier: str, quote_tier: str | None) -> str:
    if rubric_tier == quote_tier:
        return "confirmed"
    return "contested"


def build_case(
    path: Path, index: int, transcript: str, verdict: Verdict, quote: QuoteReading
) -> dict:
    return {
        "case_id": _case_id(path, index),
        "language": _language(transcript),
        "event": "session-end",
        "content_classes": list(verdict.kinds),
        "transcript": transcript,
        "expected_tier": verdict.tier,
        "required_markers": _markers(verdict),
        "label_provenance": "readings",
        "label_status": label_status(verdict.tier, quote.tier),
        "readings": {"rubric": verdict.tier, "quote": quote.tier, "passage": quote.passage},
    }


# The memory's own provider calls (`claude -p`, entrypoint `sdk-cli`) left a
# transcript each until 2026-09-17; the corpus built on 2026-08-23 took the 40
# newest transcripts and 39 were those calls, so the reviewer read the
# classifier's own prompt (2026-09-23). Only a session someone held is a case.
_OWN_CALL_ENTRYPOINT = "sdk-cli"
_HEAD_LINES = 40


def _entrypoint_of(path: Path) -> str | None:
    """The entry point named in the transcript's first records, or None."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for _ in range(_HEAD_LINES):
            line = handle.readline()
            if not line:
                return None
            entry = _decoded(line)
            if entry.get("entrypoint"):
                return str(entry["entrypoint"])
    return None


def _decoded(line: str) -> dict:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_session_transcript(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return _entrypoint_of(path) != _OWN_CALL_ENTRYPOINT
    except OSError:
        return False


def _transcripts(directory: Path, limit: int) -> list[Path]:
    found = sorted(
        (path for path in directory.rglob("*.jsonl") if _is_session_transcript(path)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return found[:limit]


def _excerpt(path: Path) -> str:
    """The text the product classifies: rendered conversation, then bounded."""
    raw = path.read_text(encoding="utf-8", errors="ignore")[-MAX_RECORD_CHARS:]
    if not raw.strip():
        return ""
    return _bounded_classifier_evidence(_readable_evidence(raw)).strip()


def _labelled_case(path: Path, index: int) -> dict | None:
    transcript = _excerpt(path)
    if not transcript:
        return None
    verdict = parse_verdict(_judge(transcript), transcript)
    quote = parse_quote_reading(_quote_reader(transcript), transcript)
    return build_case(path, index, transcript, verdict, quote)


def _reported_case(path: Path, index: int, report) -> dict | None:
    try:
        case = _labelled_case(path, index)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report(f"  skipped {path.name}: {type(error).__name__}")
        return None
    if case is None:
        report(f"  skipped {path.name}: nothing readable")
    return case


def _share(pairs: list[tuple[str, str]], index: int, tier: str) -> float:
    return sum(1 for pair in pairs if pair[index] == tier) / len(pairs)


def _observed_agreement(pairs: list[tuple[str, str]]) -> float:
    return sum(1 for first, second in pairs if first == second) / len(pairs)


def _expected_agreement(pairs: list[tuple[str, str]]) -> float:
    return sum(_share(pairs, 0, tier) * _share(pairs, 1, tier) for tier in TIERS)


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Chance-corrected agreement between the two readings' tiers."""
    if len(pairs) < KAPPA_MINIMUM_CASES:
        return None
    expected = _expected_agreement(pairs)
    if expected >= 1.0:
        return None
    return round((_observed_agreement(pairs) - expected) / (1.0 - expected), 3)


def agreement(cases: list[dict]) -> dict[str, object]:
    """How often the two readings agreed, and the chance-corrected figure."""
    confirmed = sum(1 for case in cases if case["label_status"] == "confirmed")
    pairs = [
        (str(case["readings"]["rubric"]), str(case["readings"]["quote"]))
        for case in cases
        if case["readings"]["quote"] is not None
    ]
    return {
        "confirmed": confirmed,
        "contested": len(cases) - confirmed,
        "kappa": cohens_kappa(pairs),
    }


def build_corpus(directory: Path, limit: int, report=print) -> dict:
    cases = []
    for index, path in enumerate(_transcripts(directory, limit), start=1):
        report(f"[{index}] {path.name}")
        case = _reported_case(path, index, report)
        if case is not None:
            cases.append(case)
    return {
        "corpus_id": "flush-classification-live",
        "schema_version": "flush-classification/v3",
        "source": str(directory),
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agreement": agreement(cases),
        "thresholds": {
            "tier_accuracy": 0.8,
            "durable_content_recall": 0.8,
            "false_promotion_rate": 0.2,
        },
        "cases": cases,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcripts", type=Path, default=Path.home() / ".claude" / "projects"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=40)
    return parser.parse_args(argv)


def agreement_line(corpus: dict) -> str:
    counts = corpus["agreement"]
    kappa = counts["kappa"]
    figure = "not reported" if kappa is None else f"{kappa:.3f}"
    return (
        f"labels: {counts['confirmed']} confirmed, {counts['contested']} contested; "
        f"readings' kappa {figure}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    corpus = build_corpus(args.transcripts, args.limit)
    if not corpus["cases"]:
        print("no readable transcripts: corpus not written", file=sys.stderr)
        return 1
    validate_schema(corpus, SCHEMA)
    args.out.write_text(
        json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(corpus['cases'])} case(s) → {args.out}")
    print(agreement_line(corpus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
