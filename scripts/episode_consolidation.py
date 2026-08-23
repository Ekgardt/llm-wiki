"""Turn a day of session records into durable knowledge, in the idle window.

Sessions are kept verbatim (`knowledge/raw/sessions/`), but keeping is not
remembering: nothing reads them, so nothing becomes a page. The 2026 survey of
agent memory lists principled consolidation as the first open frontier and
describes the shape this vault already has half of — raw episodes in a hot
buffer, promoted to durable storage only after validation. Letta reports 18%
higher accuracy and 2.5x lower cost per query from moving that work off the query
path; here it runs in the nightly pass, where nobody is waiting.

Promotion is validated, not trusted: every item the model returns must quote a
line that really occurs in one of the day's records, or it is dropped. What
survives is appended to the daily log as one entry, and the existing compile
pipeline — with its receipts, transactions and DLP boundary — turns it into
pages. There is no second writer.

See knowledge/notes/session-evidence-retention-decision.md (MEM-02).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, update_state  # noqa: E402
from session_evidence import SESSION_EVIDENCE_DIR  # noqa: E402

MAX_RECORDS = 12
# One budget for the day, shared between its records, instead of a fixed slice
# per record: a real session record runs to hundreds of kilobytes, and a 12 000
# character head showed the model the setup and none of the work — it answered
# "nothing durable" for a day that plainly had some.
MAX_PROMPT_CHARS = 200_000
MIN_RECORD_CHARS = 8_000
GAP_NOTE = "\n\n… (middle of the session omitted) …\n\n"
MAX_ITEMS = 8
MAX_QUOTE_CHARS = 240
MAX_TEXT_CHARS = 400
CONSOLIDATION_MAX_TOKENS = 1200
KINDS = ("decision", "lesson", "gotcha", "rule")
# A rule is procedural memory: it is read before acting, not searched for after.
# `build_guardrails` collects pattern pages whose summary carries one of these
# words and injects them at session start, so a "rule" that cannot be phrased as
# one is not a rule — it is a lesson, and it is kept as a lesson.
IMPERATIVE_MARKERS = ("do not", "don't", "never", "always", "must", "should")
MAX_TRIGGER_CHARS = 160

CONSOLIDATION_SYSTEM_PROMPT = (
    "You read a day of software work sessions and report only what a reader "
    "would still need a month later. You never invent content, you quote "
    "verbatim, and you answer with JSON alone."
)

CONSOLIDATION_PROMPT = """Below are records of the sessions from {day}.

Report the durable knowledge in them: decisions with their reason, reusable
lessons, and debugging gotchas (symptom to cause). Skip everything that was
routine work, status chatter, or a detail that only mattered inside one session.

For each item give:
- "kind": decision, lesson, gotcha, or rule
- "text": one sentence a reader would understand a month later. For a rule it
  must read as an instruction and contain one of: do not, never, always, must,
  should.
- "trigger": for a rule only — the situation in which it applies, so it can be
  read before acting rather than searched for afterwards
- "quote": a verbatim fragment from the record that supports it, under 200
  characters, copied exactly
- "session": the session id it came from

Report a rule only when the session shows something going wrong and being put
right: what should be done differently next time in that situation.

Answer with a JSON array and nothing else. If the day holds nothing durable,
answer with an empty array.

{records}"""


@dataclass(frozen=True)
class Lesson:
    kind: str
    text: str
    quote: str
    session: str
    trigger: str = ""


def session_day_directory(vault: Path, day: str) -> Path:
    return Path(vault) / SESSION_EVIDENCE_DIR / day


def session_records(vault: Path, day: str) -> list[Path]:
    directory = session_day_directory(vault, day)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())[:MAX_RECORDS]


def _record_text(path: Path) -> str:
    """The whole record; the prompt decides how much of it fits."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _record_share(count: int) -> int:
    return max(MIN_RECORD_CHARS, MAX_PROMPT_CHARS // max(count, 1))


def _within_share(text: str, share: int) -> str:
    """Head and tail, not just the head: a session's work is rarely at its start."""
    if len(text) <= share:
        return text
    half = share // 2
    return text[:half] + GAP_NOTE + text[-half:]


def _records_block(paths: list[Path]) -> str:
    share = _record_share(len(paths))
    parts = [
        f"=== session {path.stem} ===\n{_within_share(_record_text(path), share)}"
        for path in paths
    ]
    return "\n\n".join(part for part in parts if part.strip())


def build_prompt(day: str, paths: list[Path]) -> str:
    return CONSOLIDATION_PROMPT.format(day=day, records=_records_block(paths))


def _json_array(raw: str) -> list:
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("consolidation did not answer with a JSON array")
    value = json.loads(raw[start : end + 1])
    if not isinstance(value, list):
        raise ValueError("consolidation did not answer with a JSON array")
    return value


def _string_field(item: dict, name: str, limit: int) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _lesson_kind(item: dict) -> str | None:
    kind = _string_field(item, "kind", 20).casefold()
    if kind not in KINDS:
        return None
    return kind


def _is_imperative(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in IMPERATIVE_MARKERS)


def _valid_rule(kind: str, text: str, trigger: str) -> bool:
    """A rule needs a situation to fire in and words that make it an instruction."""
    if kind != "rule":
        return True
    return bool(trigger) and _is_imperative(text)


def _complete_lesson(lesson: Lesson) -> Lesson | None:
    if not lesson.text or not lesson.quote:
        return None
    if not _valid_rule(lesson.kind, lesson.text, lesson.trigger):
        return None
    return lesson


def _lesson_of(item: object) -> Lesson | None:
    if not isinstance(item, dict):
        return None
    kind = _lesson_kind(item)
    if kind is None:
        return None
    return _complete_lesson(
        Lesson(
            kind,
            _string_field(item, "text", MAX_TEXT_CHARS),
            _string_field(item, "quote", MAX_QUOTE_CHARS),
            _string_field(item, "session", 80),
            _string_field(item, "trigger", MAX_TRIGGER_CHARS),
        )
    )


def _grounded(lesson: Lesson, corpus: str) -> bool:
    """The quote has to be in the day's records; an invention is dropped."""
    return lesson.quote in corpus


def _kept_lesson(item: object, corpus: str) -> Lesson | None:
    lesson = _lesson_of(item)
    if lesson is None:
        return None
    if not _grounded(lesson, corpus):
        return None
    return lesson


def grounded_lessons(raw: str, paths: list[Path]) -> list[Lesson]:
    corpus = "\n".join(_record_text(path) for path in paths)
    kept = [_kept_lesson(item, corpus) for item in _json_array(raw)]
    return [lesson for lesson in kept if lesson is not None][:MAX_ITEMS]


def _lesson_headline(lesson: Lesson) -> str:
    """A rule states its situation first, because that is when it must be read."""
    if lesson.kind != "rule":
        return f"  - **{lesson.kind.capitalize()}** — {lesson.text}"
    return f"  - **Rule** — When {lesson.trigger}: {lesson.text}"


def _lesson_shape(lesson: Lesson) -> list[str]:
    """A rule says what page it wants to become, because that is how it gets read.

    `build_guardrails` collects pattern pages whose one-sentence summary carries
    the instruction and injects them at session start. Losing the instruction in
    the summary would leave the rule searchable but never read.
    """
    if lesson.kind != "rule":
        return []
    return ["    Kind: procedural rule — pattern page, keep the instruction in the summary"]


def _lesson_lines(lesson: Lesson) -> list[str]:
    return [
        _lesson_headline(lesson),
        f"    > {lesson.quote}",
        *_lesson_shape(lesson),
        f"    Source: `{SESSION_EVIDENCE_DIR}/…/{lesson.session}.md`",
    ]


def render_block(day: str, lessons: list[Lesson], moment: datetime) -> str:
    """One daily-log entry: the compile pipeline binds evidence inside an entry."""
    header = (
        f"- `[{moment.strftime('%H:%M:%S')}] episodes | {day}` "
        f"{len(lessons)} durable item(s) consolidated from the day's sessions"
    )
    lines = [header]
    for lesson in lessons:
        lines.extend(_lesson_lines(lesson))
    return "\n".join(lines) + "\n"


def _operation_id(day: str, lessons: list[Lesson]) -> str:
    from reliable_memory import sha256_bytes

    payload = json.dumps(
        [[item.kind, item.text, item.quote, item.trigger] for item in lessons],
        ensure_ascii=False,
    ).encode("utf-8")
    return f"episodes:{day}:{sha256_bytes(payload)[:16]}"


def _record_consolidation(day: str, count: int, records: int) -> None:
    def mutate(state: dict) -> None:
        days = state.setdefault("consolidated_session_days", {})
        days[day] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "records": records,
            "items": count,
        }

    update_state(mutate)


def _already_consolidated(state: dict, day: str) -> bool:
    days = state.get("consolidated_session_days", {})
    return isinstance(days, dict) and day in days


def _call_provider(prompt: str) -> str:
    from llm_client import call_llm

    return call_llm(
        prompt, CONSOLIDATION_SYSTEM_PROMPT, max_tokens=CONSOLIDATION_MAX_TOKENS
    ) or ""


def _write_block(day: str, lessons: list[Lesson], moment: datetime) -> Path:
    from daily_log_append import append_daily

    return append_daily(
        "episodes",
        day,
        render_block(day, lessons, moment),
        _operation_id(day, lessons),
    )


def consolidate_day(
    vault: Path,
    day: str,
    *,
    call=_call_provider,
    state: dict | None = None,
    moment: datetime | None = None,
) -> dict[str, object]:
    """Consolidate one day of session records; returns what happened and why."""
    skipped = _skip_reason(vault, day, state)
    if skipped is not None:
        return {"status": "skipped", "reason": skipped, "items": 0}
    paths = session_records(vault, day)
    lessons = grounded_lessons(call(build_prompt(day, paths)), paths)
    if not lessons:
        _record_consolidation(day, 0, len(paths))
        return {"status": "empty", "reason": None, "items": 0}
    path = _write_block(day, lessons, moment or datetime.now())
    _record_consolidation(day, len(lessons), len(paths))
    return {"status": "written", "reason": None, "items": len(lessons), "path": str(path)}


def _skip_reason(vault: Path, day: str, state: dict | None) -> str | None:
    """Why this day needs no work, or None to consolidate it."""
    if state is not None and _already_consolidated(state, day):
        return "already_consolidated"
    if not session_records(vault, day):
        return "no_records"
    return None


def _default_day() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default=None, help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--vault", type=Path, default=ROOT)
    return parser.parse_args(argv)


def _safe_state() -> dict:
    from memory_state import load_state

    try:
        return load_state()
    except Exception:  # noqa: BLE001 - state is a report here, never a precondition
        return {}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    day = args.day or _default_day()
    try:
        outcome = consolidate_day(args.vault, day, state=_safe_state())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"episode consolidation skipped: {type(error).__name__}", file=sys.stderr)
        return 0
    print(json.dumps({"day": day, **outcome}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
