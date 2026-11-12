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
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flush_corpus_labels import CYRILLIC as CYRILLIC  # noqa: E402
from flush_corpus_labels import KAPPA_MINIMUM_CASES as KAPPA_MINIMUM_CASES  # noqa: E402
from flush_corpus_labels import KEEP_TO_TIER as KEEP_TO_TIER  # noqa: E402
from flush_corpus_labels import KNOWN_KINDS as KNOWN_KINDS  # noqa: E402
from flush_corpus_labels import MAX_MARKER_WORDS as MAX_MARKER_WORDS  # noqa: E402
from flush_corpus_labels import MAX_PASSAGE_CHARS as MAX_PASSAGE_CHARS  # noqa: E402
from flush_corpus_labels import MIN_QUOTE_WORDS as MIN_QUOTE_WORDS  # noqa: E402
from flush_corpus_labels import SENTENCE_END as SENTENCE_END  # noqa: E402
from flush_corpus_labels import TIERS as TIERS  # noqa: E402
from flush_corpus_labels import WHITESPACE as WHITESPACE  # noqa: E402
from flush_corpus_labels import QuoteReading as QuoteReading  # noqa: E402
from flush_corpus_labels import Verdict as Verdict  # noqa: E402
from flush_corpus_labels import _case_id as _case_id  # noqa: E402
from flush_corpus_labels import _expected_agreement as _expected_agreement  # noqa: E402
from flush_corpus_labels import _json_object as _json_object  # noqa: E402
from flush_corpus_labels import _kinds_of as _kinds_of  # noqa: E402
from flush_corpus_labels import _language as _language  # noqa: E402
from flush_corpus_labels import _markers as _markers  # noqa: E402
from flush_corpus_labels import _normalised as _normalised  # noqa: E402
from flush_corpus_labels import _observed_agreement as _observed_agreement  # noqa: E402
from flush_corpus_labels import _phrases_of as _phrases_of  # noqa: E402
from flush_corpus_labels import _quoted_in as _quoted_in  # noqa: E402
from flush_corpus_labels import _sentences as _sentences  # noqa: E402
from flush_corpus_labels import _share as _share  # noqa: E402
from flush_corpus_labels import _tier_of as _tier_of  # noqa: E402
from flush_corpus_labels import agreement as agreement  # noqa: E402
from flush_corpus_labels import agreement_line as agreement_line  # noqa: E402
from flush_corpus_labels import build_case as build_case  # noqa: E402
from flush_corpus_labels import cohens_kappa as cohens_kappa  # noqa: E402
from flush_corpus_labels import label_status as label_status  # noqa: E402
from flush_corpus_labels import parse_quote_reading as parse_quote_reading  # noqa: E402
from flush_corpus_labels import parse_verdict as parse_verdict  # noqa: E402
from flush_corpus_labels import quote_is_verbatim as quote_is_verbatim  # noqa: E402
from flush_memory import (  # noqa: E402
    MAX_RECORD_CHARS,
    bounded_classifier_evidence,
    readable_evidence,
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

def _ask(prompt: str) -> str:
    from llm_client import call_llm

    return call_llm(prompt, JUDGE_SYSTEM_PROMPT, max_tokens=JUDGE_MAX_TOKENS) or ""


def _judge(transcript: str) -> str:
    return _ask(RUBRIC_PROMPT.format(transcript=transcript))


def _quote_reader(transcript: str) -> str:
    return _ask(QUOTE_PROMPT.format(transcript=transcript))


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
    return bounded_classifier_evidence(readable_evidence(raw)).strip()


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
        "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
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
