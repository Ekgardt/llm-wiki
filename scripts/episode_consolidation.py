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
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, update_state  # noqa: E402
from session_evidence import SESSION_EVIDENCE_DIR  # noqa: E402

MAX_RECORDS = 12
# Twenty calls is a very busy night and still a bounded one. The bound belongs to
# the run, not to the day: a day with more batches than this stays pending and the
# next run continues it, skipping the batches its daily log already names. See
# `docs/research/2026-09-17-a-day-is-consolidated-whole-and-reopened-when-it-grows.md`.
MAX_BATCHES_PER_RUN = 20
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
    """Every record of the day. What is bounded is one run, not the day."""
    directory = session_day_directory(vault, day)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def record_set_digest(vault: Path, day: str) -> str:
    """The identity of a day's record set: the names it holds, in order.

    A day is recorded as consolidated together with this digest, so a record that
    arrives after the day was closed — a session that ended after midnight, an
    imported history — makes the day pending again instead of being never read.
    """
    from reliable_memory import sha256_bytes

    names = "\n".join(path.name for path in session_records(vault, day))
    return sha256_bytes(names.encode("utf-8"))


def record_batches(paths: list[Path]) -> list[list[Path]]:
    """One prompt's worth at a time.

    A day used to be truncated to the first twelve records, which was invisible
    and wrong the moment a day held more: the imported history has a day with 171
    sessions, and everything past the twelfth was marked consolidated without
    ever being read. Each batch is one call, and the number of them is bounded.
    """
    return [paths[index : index + MAX_RECORDS] for index in range(0, len(paths), MAX_RECORDS)]


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
    """The array the model answered with, read by the one JSON reply reader.

    First `[` to last `]` refused a reply that mentioned a `[[wikilink]]` before
    the array or a `[2]` after it. See
    `docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
    """
    from reply_json import reply_array

    try:
        value = reply_array(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("consolidation did not answer with a JSON array") from exc
    if not isinstance(value, list):
        raise ValueError("consolidation did not answer with a JSON array")
    return value


def _raw_field(item: dict, name: str, limit: int) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _one_line(text: str) -> str:
    """A daily log is line-structured: a field that could end its line could begin an entry.

    Research: docs/research/2026-09-17-a-lesson-is-one-line-from-a-session-that-holds-its-quote.md
    """
    return " ".join(text.split())


def _string_field(item: dict, name: str, limit: int) -> str:
    return _one_line(_raw_field(item, name, limit))


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
            # As the model gave it: it is matched against the records first.
            _raw_field(item, "quote", MAX_QUOTE_CHARS),
            "",
            _string_field(item, "trigger", MAX_TRIGGER_CHARS),
        )
    )


def _holding_session(quote: str, records: dict[str, str]) -> str | None:
    """The record the quote is in; an invention is in none. The model's own
    `session` is not trusted with the `Source:` line."""
    return next((stem for stem, text in records.items() if quote in text), None)


def _kept_lesson(item: object, records: dict[str, str]) -> Lesson | None:
    lesson = _lesson_of(item)
    if lesson is None:
        return None
    session = _holding_session(lesson.quote, records)
    if session is None:
        return None
    return replace(lesson, quote=_one_line(lesson.quote), session=session)


def grounded_lessons(raw: str, paths: list[Path]) -> list[Lesson]:
    records = {path.stem: _record_text(path) for path in paths}
    kept = [_kept_lesson(item, records) for item in _json_array(raw)]
    return [lesson for lesson in kept if lesson is not None][:MAX_ITEMS]


def _lesson_headline(lesson: Lesson) -> str:
    """A rule states its situation first, because that is when it must be read."""
    if lesson.kind != "rule":
        return f"  - **{lesson.kind.capitalize()}** — {lesson.text}"
    return f"  - **Rule** — When {_situation(lesson.trigger)}: {lesson.text}"


def _situation(trigger: str) -> str:
    """The trigger without its own leading "When": the model often writes one."""
    words = trigger.strip()
    if words[:5].casefold() == "when ":
        return words[5:].lstrip()
    return words


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


def _record_consolidation(day: str, count: int, records: int, digest: str) -> None:
    def mutate(state: dict) -> None:
        days = state.setdefault("consolidated_session_days", {})
        days[day] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "records": records,
            "items": count,
            "record_set": digest,
        }
        progress = state.get(PROGRESS_KEY)
        if isinstance(progress, dict):
            progress.pop(day, None)

    update_state(mutate)


# A batch whose reply cannot be read this many times is recorded as failed and
# no longer paid for. See `docs/research/2026-09-14-a-day-that-failed-is-tried-again.md`.
MAX_BATCH_ATTEMPTS = 3
PROGRESS_KEY = "consolidation_progress"
# The last line of a batch's block names the batch, so a run whose checkpoint was not
# saved finds the block instead of asking the model again. See
# `docs/research/2026-09-14-a-written-batch-is-found-not-asked-again.md`.
BATCH_MARKER = "<!-- llm-wiki-episode-batch:{key} -->"
_BATCH_MARKER_RE = re.compile(r"<!-- llm-wiki-episode-batch:([0-9a-f]{64}) -->")


class ConsolidationUnavailable(RuntimeError):
    """The provider returned nothing: this day, and every other, stays pending."""


@dataclass
class _Progress:
    """Which batches of a day are finished, so a rerun neither skips nor repeats one."""

    done: set[str]
    attempts: dict[str, int]
    failed: list[str]
    items: int = 0
    written: str | None = None

    def finished(self, key: str, count: int, path: str | None) -> None:
        self.done.add(key)
        self.items += count
        self.written = path or self.written

    def failed_once(self, key: str) -> None:
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] >= MAX_BATCH_ATTEMPTS:
            self.done.add(key)
            self.failed.append(key)

    def as_state(self) -> dict[str, object]:
        return {
            "done": sorted(self.done),
            "attempts": dict(self.attempts),
            "failed": list(self.failed),
            "items": self.items,
            "written": self.written,
        }


def _stored_progress(state: dict | None, day: str) -> dict:
    """The day's checkpoint as stored, or an empty one."""
    days = dict(state or {}).get(PROGRESS_KEY)
    if not isinstance(days, dict):
        return {}
    stored = days.get(day)
    if not isinstance(stored, dict):
        return {}
    return stored


def _day_progress(state: dict | None, day: str) -> _Progress:
    stored = {"done": (), "attempts": {}, "failed": (), "items": 0, "written": None}
    stored.update(_stored_progress(state, day))
    return _Progress(
        set(stored["done"]),
        dict(stored["attempts"]),
        list(stored["failed"]),
        int(stored["items"]),
        stored["written"],
    )


def _save_progress(day: str, progress: _Progress) -> None:
    def mutate(state: dict) -> None:
        state.setdefault(PROGRESS_KEY, {})[day] = progress.as_state()

    update_state(mutate)


def _batch_key(vault: Path, day: str, batch: list[Path]) -> str:
    """A batch's identity: its vault, its day and the bytes of every record in it."""
    from reliable_memory import sha256_bytes

    parts = [str(Path(vault).resolve()), day]
    parts.extend(f"{path.name}:{sha256_bytes(path.read_bytes())}" for path in batch)
    return sha256_bytes("\n".join(parts).encode("utf-8"))


def _consolidation_record(state: dict, day: str) -> dict | None:
    days = state.get("consolidated_session_days", {})
    if not isinstance(days, dict):
        return None
    stored = days.get(day)
    return stored if isinstance(stored, dict) else None


def _already_consolidated(vault: Path, state: dict, day: str) -> bool:
    """Closed, unless the day's records have changed since it was closed.

    A day consolidated before this digest existed carries none, and stays closed:
    re-reading the whole imported history would cost a provider call per batch of
    it and could not find anything new.
    """
    stored = _consolidation_record(state, day)
    if stored is None:
        return False
    recorded = stored.get("record_set")
    if not isinstance(recorded, str) or not recorded:
        return True
    return recorded == record_set_digest(vault, day)


# The same bound the compile gives its provider calls (`COMPILE_PROVIDER_CEILING_S`).
# Under the client's 90 s default the catch-up pass of 2026-09-23 stopped the
# provider mid-answer on one day's records and the whole night counted as
# failed. See `docs/research/2026-09-23-the-rest-of-the-live-audit.md`.
CONSOLIDATION_PROVIDER_CEILING_S = 300


def _call_provider(prompt: str) -> str | None:
    from llm_client import call_ceiling, call_llm

    with call_ceiling(CONSOLIDATION_PROVIDER_CEILING_S):
        return call_llm(
            prompt, CONSOLIDATION_SYSTEM_PROMPT, max_tokens=CONSOLIDATION_MAX_TOKENS
        )


def _write_block(day: str, lessons: list[Lesson], moment: datetime, key: str) -> Path:
    from daily_log_append import append_daily

    return append_daily(
        "episodes",
        day,
        render_block(day, lessons, moment) + BATCH_MARKER.format(key=key) + "\n",
        _operation_id(day, lessons),
    )


def _consolidate_batch(
    day: str, batch: list[Path], call, moment: datetime, key: str
) -> tuple[int, str | None]:
    """(durable items written, path) for one prompt's worth of records."""
    reply = call(build_prompt(day, batch))
    if not reply:
        raise ConsolidationUnavailable("consolidation provider returned nothing")
    lessons = grounded_lessons(reply, batch)
    if not lessons:
        return 0, None
    return len(lessons), str(_write_block(day, lessons, moment, key))


def _logged_batches(vault: Path, day: str) -> frozenset[str]:
    """Batch keys already written to a daily log dated from `day` on."""
    directory = Path(vault) / "knowledge" / "daily"
    if not directory.is_dir():
        return frozenset()
    logs = [path for path in directory.glob("*.md") if path.stem >= day]
    found: set[str] = set()
    for path in logs:
        found.update(_BATCH_MARKER_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return frozenset(found)


def _batches_to_find(vault: Path, day: str, keys: list[str], progress: _Progress) -> frozenset[str]:
    """The logged batches, read only when the checkpoint leaves one unfinished."""
    if set(keys) <= progress.done:
        return frozenset()
    return _logged_batches(vault, day)


def consolidate_day(
    vault: Path,
    day: str,
    *,
    call=_call_provider,
    state: dict | None = None,
    moment: datetime | None = None,
    deadline: float | None = None,
) -> dict[str, object]:
    """Consolidate one day of session records; returns what happened and why.

    Finished batches are checkpointed, so a day that stops part-way — out of time,
    or a reply that could not be read — resumes where it stopped on the next run.
    """
    skipped = _skip_reason(vault, day, state)
    if skipped is not None:
        return {"status": "skipped", "reason": skipped, "items": 0}
    paths = session_records(vault, day)
    batches = record_batches(paths)
    keys = [_batch_key(vault, day, batch) for batch in batches]
    progress = _day_progress(state, day)
    logged = _batches_to_find(vault, day, keys, progress)
    run = _BatchRun(day, call, moment or datetime.now(), progress, deadline, logged)
    run.all(batches, keys)
    if not set(keys) <= progress.done:
        return _day_outcome("partial", progress, len(batches))
    _record_consolidation(
        day, progress.items, len(paths), record_set_digest(vault, day)
    )
    return _day_outcome(_finished_status(progress), progress, len(batches))


def _finished_status(progress: _Progress) -> str:
    if progress.items:
        return "written"
    return "empty"


@dataclass
class _BatchRun:
    """Each batch gets its own moment: two entries in one second are ambiguous.

    A daily entry is located by its timestamp, and the compile refuses evidence
    whose timestamp names more than one entry. Batches finish in well under a
    second, so a shared moment made twelve entries indistinguishable and no
    compile of that day could ever bind its evidence.
    """

    day: str
    call: object
    when: datetime
    progress: _Progress
    deadline: float | None
    logged: frozenset[str] = frozenset()
    attempted: int = 0

    def spent(self) -> bool:
        """This run is over: its time is up, or it has attempted its share."""
        if _out_of_time(self.deadline):
            return True
        return self.attempted >= MAX_BATCHES_PER_RUN

    def all(self, batches: list[list[Path]], keys: list[str]) -> None:
        """At most one run's worth of batches; the rest wait for the next run."""
        for index, (batch, key) in enumerate(zip(batches, keys)):
            if self.spent():
                return
            self.one(index, batch, key)

    def one(self, index: int, batch: list[Path], key: str) -> None:
        if key in self.progress.done:
            return
        self.attempted += 1
        if key in self.logged:
            self.progress.finished(key, 0, None)
            _save_progress(self.day, self.progress)
            return
        try:
            count, path = _consolidate_batch(
                self.day, batch, self.call, self.when + timedelta(seconds=index), key
            )
        except ValueError:
            self.progress.failed_once(key)
        else:
            self.progress.finished(key, count, path)
        _save_progress(self.day, self.progress)


def _out_of_time(deadline: float | None) -> bool:
    import time

    return deadline is not None and time.monotonic() >= deadline


def _day_outcome(status: str, progress: _Progress, batches: int) -> dict[str, object]:
    outcome: dict[str, object] = {
        "status": status,
        "reason": None,
        "items": progress.items,
        "batches": batches,
        "failed_batches": len(progress.failed),
    }
    if progress.written:
        outcome["path"] = progress.written
    return outcome


def _record_days(vault: Path) -> list[str]:
    directory = Path(vault) / SESSION_EVIDENCE_DIR
    if not directory.is_dir():
        return []
    return sorted(item.name for item in directory.iterdir() if item.is_dir())


def pending_days(vault: Path, state: dict, today: str | None = None) -> list[str]:
    """Days before today whose records are not all consolidated yet, oldest first.

    Today is never pending: its sessions are still being written, and closing it
    at noon would leave its evening unread. A day whose records changed since it
    was consolidated — a session captured late, for instance — is pending again.
    """
    before = _today_or(today)
    return [day for day in _record_days(vault) if _pending(vault, day, before, state)]


def _today_or(today: str | None) -> str:
    if today is not None:
        return today
    return datetime.now().strftime("%Y-%m-%d")


def _pending(vault: Path, day: str, before: str, state: dict) -> bool:
    return day < before and not _already_consolidated(vault, state, day)


def _skip_reason(vault: Path, day: str, state: dict | None) -> str | None:
    """Why this day needs no work, or None to consolidate it."""
    if state is not None and _already_consolidated(vault, state, day):
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
    parser.add_argument(
        "--all-pending",
        action="store_true",
        help="Catch up every day that has records and was never consolidated",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="With --all-pending: stop after N days"
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=0.0,
        help="Start no new batch after this many seconds (0: no budget)",
    )
    return parser.parse_args(argv)


def _safe_state() -> dict:
    from memory_state import load_state

    try:
        return load_state()
    except Exception:  # noqa: BLE001 - state is a report here, never a precondition
        return {}


def _consolidate_reported(vault: Path, day: str, deadline: float | None = None) -> bool:
    """One day, with its outcome printed; False when the run should stop.

    A failure ends that day, not the run — except a provider that returned nothing,
    which every other day would meet too.
    """
    try:
        outcome = consolidate_day(vault, day, state=_safe_state(), deadline=deadline)
    except ConsolidationUnavailable as error:
        print(f"episode consolidation stopped: {error}", file=sys.stderr)
        return False
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"episode consolidation skipped: {type(error).__name__}", file=sys.stderr)
        return True
    print(json.dumps({"day": day, **outcome}, ensure_ascii=False))
    return True


def _budget_deadline(seconds: float) -> float | None:
    import time

    if seconds <= 0:
        return None
    return time.monotonic() + seconds


def _selected_days(args: argparse.Namespace) -> list[str]:
    if not args.all_pending:
        return [args.day or _default_day()]
    days = pending_days(args.vault, _safe_state())
    if args.limit > 0:
        return days[: args.limit]
    return days


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    deadline = _budget_deadline(args.budget_seconds)
    for day in _selected_days(args):
        if _out_of_time(deadline):
            break
        if not _consolidate_reported(args.vault, day, deadline):
            # The provider returned nothing: the step did not do its work, and a
            # zero exit hid that from the nightly log.
            # Research: docs/research/2026-09-17-a-step-no-provider-answered-is-not-green.md
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
