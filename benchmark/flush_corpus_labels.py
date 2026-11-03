"""Pure reading interpretation and agreement for the classification corpus.

IO, prompts, provider calls and CLI orchestration belong to build_flush_corpus.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

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



def agreement_line(corpus: dict) -> str:
    counts = corpus["agreement"]
    kappa = counts["kappa"]
    figure = "not reported" if kappa is None else f"{kappa:.3f}"
    return (
        f"labels: {counts['confirmed']} confirmed, {counts['contested']} contested; "
        f"readings' kappa {figure}"
    )
