"""Build a classification corpus from this machine's real sessions.

`OPEN-034` asked how often a session holding a decision, a fix or a gotcha loses
it on the way into durable memory. The measurement stand existed; the corpus did
not — the one shipped in the repository is nine synthetic public cases, and the
register said a real number was impossible here because there was no installed
runtime. That stopped being true when the vault and the source became one
directory: the real transcripts are on this machine.

Labels come from a rubric that never sees the product's tier names or its
classification prompt, so the corpus is not simply the system agreeing with
itself. They are still model-produced, so every case records that and whether a
human has confirmed it. Current practice is unambiguous that a judge is
calibrated against human labels rather than substituted for them, so the report
this corpus feeds says "provisional" until the labels are reviewed.

The built corpus holds real session text and is never committed.

See docs/research/2026-08-23-labelling-real-sessions-for-classification.md.
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

from flush_memory import MAX_TRANSCRIPT_CHARS, read_transcript_excerpt  # noqa: E402
from reliable_memory import validate_schema  # noqa: E402

SCHEMA = ROOT / "benchmark/flush-classification-v2.schema.json"
DEFAULT_OUT = ROOT / "benchmark/flush-classification-live.json"
# The same budget the product gives the classifier in production: a shorter
# excerpt would measure a reader that sees less than the real one.
MAX_CASE_CHARS = MAX_TRANSCRIPT_CHARS
JUDGE_MAX_TOKENS = 900

# Deliberately not the product's vocabulary: this rubric never names FLUSH_MAJOR,
# FLUSH_MINOR or FLUSH_OK, so a case is not labelled by the thing being measured.
JUDGE_SYSTEM_PROMPT = (
    "You read a software work session and report only what a reader would still "
    "need a month later. You never invent content. You answer with JSON alone."
)

JUDGE_PROMPT = """Read this work session and answer three questions about it.

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

KEEP_TO_TIER = {"much": "major", "little": "minor", "nothing": "ok"}
KNOWN_KINDS = {"decision", "lesson", "command", "gotcha", "question"}
CYRILLIC = re.compile(r"[Ѐ-ӿ]")


@dataclass(frozen=True)
class Verdict:
    tier: str
    kinds: tuple[str, ...]
    phrases: tuple[str, ...]


def _judge(transcript: str) -> str:
    from llm_client import call_llm

    prompt = JUDGE_PROMPT.format(transcript=transcript)
    return call_llm(prompt, JUDGE_SYSTEM_PROMPT, max_tokens=JUDGE_MAX_TOKENS) or ""


def _json_object(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge did not answer with JSON")
    return json.loads(text[start : end + 1])


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
    """Only phrases that really occur in the session are kept as markers."""
    if not isinstance(value, list):
        return ()
    quoted = [str(item).strip() for item in value]
    return tuple(item for item in quoted if _quoted_in(item, transcript))[:3]


def parse_verdict(text: str, transcript: str) -> Verdict:
    document = _json_object(text)
    tier = KEEP_TO_TIER.get(str(document.get("keeps", "")).strip().casefold())
    if tier is None:
        raise ValueError("judge did not name what the session keeps")
    return Verdict(
        tier=tier,
        kinds=_kinds_of(document.get("kinds")),
        phrases=_phrases_of(document.get("phrases"), transcript),
    )


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


def build_case(path: Path, index: int, transcript: str, verdict: Verdict) -> dict:
    return {
        "case_id": _case_id(path, index),
        "language": _language(transcript),
        "event": "session-end",
        "content_classes": list(verdict.kinds),
        "transcript": transcript,
        "expected_tier": verdict.tier,
        "required_markers": _markers(verdict),
        "label_provenance": "judge",
        "label_reviewed": False,
    }


def _transcripts(directory: Path, limit: int) -> list[Path]:
    found = sorted(
        (path for path in directory.rglob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return found[:limit]


def _excerpt(path: Path) -> str:
    text = read_transcript_excerpt(path, max_chars=MAX_CASE_CHARS)
    return text.strip()


def _labelled_case(path: Path, index: int) -> dict | None:
    transcript = _excerpt(path)
    if not transcript:
        return None
    verdict = parse_verdict(_judge(transcript), transcript)
    return build_case(path, index, transcript, verdict)


def _reported_case(path: Path, index: int, report) -> dict | None:
    try:
        case = _labelled_case(path, index)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        report(f"  skipped {path.name}: {type(error).__name__}")
        return None
    if case is None:
        report(f"  skipped {path.name}: nothing readable")
    return case


def build_corpus(directory: Path, limit: int, report=print) -> dict:
    cases = []
    for index, path in enumerate(_transcripts(directory, limit), start=1):
        report(f"[{index}] {path.name}")
        case = _reported_case(path, index, report)
        if case is not None:
            cases.append(case)
    return {
        "corpus_id": "flush-classification-live",
        "schema_version": "flush-classification/v2",
        "source": str(directory),
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
