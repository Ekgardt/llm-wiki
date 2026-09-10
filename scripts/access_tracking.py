"""Legacy access adapters and explicit frontmatter promotion.

Tracks how often each page is accessed (search hits, advisory injection,
direct reads). This data powers:
- Forgetting curve (Ebbinghaus decay): pages with no access decay over time.
- Quality scoring: frequently-accessed pages are validated as useful.
- Advisory ranking: recently-accessed pages get boost in SessionStart.

New events are stored privately in cache/evidence-graph/telemetry.sqlite3.
The old cache/access_log.jsonl remains bounded read-only migration history.

Frontmatter fields updated on page files:
- access_count: int (how many times accessed)
- last_accessed: ISO timestamp (when last accessed)

Frontmatter promotion is manual and never automatic event transport.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes  # noqa: E402
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from reliable_memory import read_runtime_bytes, sha256_bytes  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
ACCESS_LOG_FILE = STATE_ROOT / "cache" / "access_log.jsonl"
MAX_ACCESS_PAGE_BYTES = MAX_KNOWLEDGE_PAGE_BYTES
MAX_LEGACY_ACCESS_LOG_BYTES = 16 * 1024 * 1024
MAX_LEGACY_ACCESS_LOG_LINES = 100_000
MAX_PAGES_PER_EXPORT = 100
MAX_CANDIDATES_SCANNED_PER_EXPORT = 1_000
MAX_EVENTS_PER_PAGE_EXPORT = 1_000

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _integer_field_lines(name: str) -> re.Pattern[str]:
    quoted = rf'(?:{re.escape(name)}|"{re.escape(name)}"|\'{re.escape(name)}\')'
    return re.compile(rf"^[ \t]*{quoted}[ \t]*:.*$", re.MULTILINE)


_INTEGER_VALUE = (
    r'(?:"(?P<double>\d+)"|\'(?P<single>\d+)\'|(?P<plain>\d+))'
    r"(?:[ \t]+#.*)?[ \t]*"
)


def _integer_line(fm: str, name: str) -> str | None:
    """The one line of `name`, None when absent; two lines are an error."""
    lines = list(_integer_field_lines(name).finditer(fm))
    if len(lines) > 1:
        raise ValueError(f"duplicate {name} field")
    if not lines:
        return None
    return lines[0].group(0).removesuffix("\r")


def _parse_frontmatter_integer(fm: str, name: str) -> tuple[int | None, bool]:
    line = _integer_line(fm, name)
    if line is None:
        return None, False
    quoted = rf'(?:{re.escape(name)}|"{re.escape(name)}"|\'{re.escape(name)}\')'
    value = re.fullmatch(rf"[ \t]*{quoted}[ \t]*:[ \t]*" + _INTEGER_VALUE, line)
    if value is None:
        raise ValueError(f"malformed {name} field")
    digits = value.group("double") or value.group("single") or value.group("plain")
    return int(digits), True


def _set_frontmatter_integer(fm: str, name: str, value: int, present: bool) -> str:
    if present:
        return _integer_field_lines(name).sub(f"{name}: {value}", fm, count=1)
    return f"{fm}\n{name}: {value}"


def record_access(slug: str, source: str = "search", query: str | None = None,
                  rank: int | None = None) -> None:
    """Best-effort compatibility adapter to durable retrieval telemetry.

    Args:
        slug: The page slug (filename without .md).
        source: 'search' | 'session-start' | 'compile' | 'direct'.
        query: The search query that surfaced this page (if applicable).
        rank: The position this page was shown at (if applicable).
    """
    try:
        from retrieval_telemetry import best_effort_make_event, best_effort_record_event

        kind = {
            "search": "impression",
            "session-start": "context_injected",
            "direct": "page_read",
            "compile": "page_read",
        }.get(source, "page_read")
        event = best_effort_make_event(
            event_kind=kind,
            query=query,
            retrieval_mode="legacy-search" if kind == "impression" else "direct",
            candidate_id=slug,
            rank=rank if kind == "impression" else None,
            generation="legacy",
            source_tool=source,
        )
        if event is not None:
            best_effort_record_event(event)
    except Exception:
        pass


# Pages the last flush could not export, named: [{"slug", "error"}]. The
# cursor still advances past them (a bounded scan never stalls on one page)
# and a wrapped cursor retries them. Research:
# docs/research/2026-09-10-a-page-that-cannot-be-flushed-is-named.md
_FLUSH_FAILURES: list[dict[str, str]] = []


def last_flush_failures() -> list[dict[str, str]]:
    return list(_FLUSH_FAILURES)


def _note_flush_failure(slug: str, error: BaseException) -> None:
    from secret_redact import describe_error

    described = describe_error(error)
    _FLUSH_FAILURES.append({"slug": slug, "error": described})
    print(f"access_tracking: {slug}: not exported ({described})", file=sys.stderr)


def _page_events(slug: str, watermark: int) -> list:
    from retrieval_telemetry import read_events_after

    return read_events_after(slug, after_sequence=watermark, limit=MAX_EVENTS_PER_PAGE_EXPORT)


def _with_last_accessed(fm: str, last_accessed: str) -> str:
    if re.search(r"^last_accessed:", fm, re.MULTILINE):
        return re.sub(
            r"^last_accessed:.*$",
            f"last_accessed: {last_accessed}",
            fm,
            count=1,
            flags=re.MULTILINE,
        )
    return f"{fm}\nlast_accessed: {last_accessed}"


def _updated_frontmatter(fm: str, count: int, last_accessed: str, final_sequence: int) -> str:
    access_count, count_present = _parse_frontmatter_integer(fm, "access_count")
    _watermark, watermark_present = _parse_frontmatter_integer(fm, "access_telemetry_sequence")
    fm = _set_frontmatter_integer(fm, "access_count", (access_count or 0) + count, count_present)
    fm = _with_last_accessed(fm, last_accessed)
    return _set_frontmatter_integer(
        fm, "access_telemetry_sequence", final_sequence, watermark_present
    )


def _rendered_page(content: str, count: int, last_accessed: str, final_sequence: int) -> str:
    fm_match = FRONTMATTER_RE.match(content)
    if fm_match is None:
        return (
            f"---\naccess_count: {count}\nlast_accessed: {last_accessed}\n"
            f"access_telemetry_sequence: {final_sequence}\n---\n\n{content}"
        )
    fm = _updated_frontmatter(fm_match.group(1), count, last_accessed, final_sequence)
    return f"---\n{fm}\n---\n" + content[fm_match.end():]


def _page_watermark(content: str) -> int:
    fm_match = FRONTMATTER_RE.match(content)
    fm = fm_match.group(1) if fm_match else ""
    watermark, _present = _parse_frontmatter_integer(fm, "access_telemetry_sequence")
    return watermark or 0


def _page_change(page_path: Path, slug: str, source_bytes: bytes, events: list) -> tuple:
    """(operation id, changes, preconditions) for one page's pending events."""
    content = source_bytes.decode("utf-8")
    count = len(events)
    first_sequence, final_sequence = events[0].sequence, events[-1].sequence
    last_accessed = max(item.event.timestamp for item in events)
    encoded = _rendered_page(content, count, last_accessed, final_sequence).encode("utf-8")
    operation_id = stable_operation_id(
        "access-telemetry", f"{slug}:{first_sequence}-{final_sequence}:{count}", encoded
    )
    precondition = {
        page_path.relative_to(KNOWLEDGE_DIR.parent.parent).as_posix(): sha256_bytes(source_bytes)
    }
    return operation_id, {page_path: encoded}, precondition


def _flush_page(slug: str) -> bool:
    """Export one page's pending events; True when the page was rewritten."""
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return False
    source_bytes = read_stable_bytes(page_path, MAX_ACCESS_PAGE_BYTES, label="access tracking page")
    events = _page_events(slug, _page_watermark(source_bytes.decode("utf-8")))
    if not events:
        return False
    operation_id, changes, preconditions = _page_change(page_path, slug, source_bytes, events)
    mutate_knowledge(operation_id, changes, preconditions=preconditions)
    return True


def flush_access_to_frontmatter(slug: str | None = None) -> int:
    """Explicitly promote a bounded durable telemetry slice to frontmatter.

    With a slug, flush that page: 1 when it was rewritten, 0 when nothing was
    pending or the page could not be exported (then it is named in
    `last_flush_failures()`). Without one, scan candidates from the cursor.
    """
    if slug is None:
        return _flush_candidates_with_cursor()
    try:
        return int(_flush_page(slug))
    except Exception as exc:  # noqa: BLE001 - named, never silent
        _note_flush_failure(slug, exc)
        return 0


def _candidate_batch(cursor: str, remaining: int) -> list[str] | None:
    """The next candidates after `cursor`; None when the store cannot answer."""
    from retrieval_telemetry import list_candidate_ids

    try:
        return list_candidate_ids(after_candidate=cursor, limit=min(remaining, 1_000))
    except Exception as exc:  # noqa: BLE001 - named, never silent
        _note_flush_failure("<candidates>", exc)
        return None


def _advance_cursor(candidate: str) -> bool:
    from retrieval_telemetry import set_export_cursor

    try:
        set_export_cursor(candidate)
    except Exception as exc:  # noqa: BLE001 - named, never silent
        _note_flush_failure("<cursor>", exc)
        return False
    return True


class _Scan:
    """One bounded pass over the candidates: counts and the moving cursor."""

    def __init__(self, cursor: str) -> None:
        self.cursor = cursor
        self.scanned = 0
        self.updated = 0

    def exhausted(self) -> bool:
        return (
            self.scanned >= MAX_CANDIDATES_SCANNED_PER_EXPORT
            or self.updated >= MAX_PAGES_PER_EXPORT
        )

    def visit(self, candidate: str) -> bool:
        """Flush one candidate and move the cursor; False when the cursor is stuck."""
        if (KNOWLEDGE_DIR / f"{candidate}.md").is_file():
            self.updated += flush_access_to_frontmatter(candidate)
        if not _advance_cursor(candidate):
            return False
        self.cursor = candidate
        self.scanned += 1
        return True

    def visit_all(self, candidates: list[str]) -> bool:
        for candidate in candidates:
            if self.exhausted():
                return True
            if not self.visit(candidate):
                return False
        return True


def _export_cursor() -> str | None:
    from retrieval_telemetry import get_export_cursor

    try:
        return get_export_cursor()
    except Exception as exc:  # noqa: BLE001 - named, never silent
        _note_flush_failure("<cursor>", exc)
        return None


def _next_batch(scan: _Scan, may_wrap: bool) -> tuple[list[str] | None, bool]:
    """The next candidates and whether one wrap to the start is still allowed."""
    remaining = MAX_CANDIDATES_SCANNED_PER_EXPORT - scan.scanned
    candidates = _candidate_batch(scan.cursor, remaining)
    if candidates or candidates is None or not may_wrap:
        return candidates, may_wrap
    scan.cursor = ""
    return _candidate_batch("", remaining), False


def _flush_candidates_with_cursor() -> int:
    _FLUSH_FAILURES.clear()
    cursor = _export_cursor()
    if cursor is None:
        return 0
    scan = _Scan(cursor)
    may_wrap = bool(cursor)
    while not scan.exhausted():
        candidates, may_wrap = _next_batch(scan, may_wrap)
        if not candidates or not scan.visit_all(candidates):
            break
    return scan.updated


def flush_all() -> int:
    """Explicitly promote bounded durable telemetry; never append JSONL."""
    return flush_access_to_frontmatter(None)


class _Stats:
    def __init__(self) -> None:
        self.total = 0
        self.last_ts: str | None = None
        self.sources: dict[str, int] = {}

    def count(self, timestamp: object, source: object) -> None:
        self.total += 1
        self._remember_latest(timestamp)
        name = source if isinstance(source, str) else "unknown"
        self.sources[name] = self.sources.get(name, 0) + 1

    def _remember_latest(self, timestamp: object) -> None:
        if not isinstance(timestamp, str) or not timestamp:
            return
        if self.last_ts is None or timestamp > self.last_ts:
            self.last_ts = timestamp

    def as_dict(self) -> dict:
        return {"total_count": self.total, "last_accessed": self.last_ts, "sources": self.sources}


def _count_telemetry(stats: _Stats, slug: str) -> None:
    try:
        from retrieval_telemetry import read_events

        for event in read_events(candidate_id=slug, limit=1_000):
            stats.count(event.timestamp, event.source_tool)
    except Exception as exc:  # noqa: BLE001 - the legacy log still answers
        _note_flush_failure(slug, exc)


def _legacy_lines() -> list[str]:
    if not ACCESS_LOG_FILE.exists():
        return []
    raw = read_runtime_bytes(
        ACCESS_LOG_FILE, ACCESS_LOG_FILE.parent, max_bytes=MAX_LEGACY_ACCESS_LOG_BYTES
    )
    return raw.decode("utf-8", errors="strict").splitlines()[:MAX_LEGACY_ACCESS_LOG_LINES]


def _legacy_entry(line: str) -> dict | None:
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    return entry if isinstance(entry, dict) else None


def _count_legacy(stats: _Stats, slug: str) -> None:
    try:
        lines = _legacy_lines()
    except (OSError, PermissionError, UnicodeError, ValueError) as exc:
        _note_flush_failure(slug, exc)
        return
    for line in lines:
        entry = _legacy_entry(line)
        if entry is not None and entry.get("slug") == slug:
            stats.count(entry.get("timestamp", ""), entry.get("source", "unknown"))


def get_access_stats(slug: str) -> dict:
    """Get bounded telemetry plus read-only legacy JSONL statistics.

    Returns:
        Dict with: total_count, last_accessed, sources (dict).
    """
    stats = _Stats()
    _count_telemetry(stats, slug)
    _count_legacy(stats, slug)
    return stats.as_dict()


def decay_score(slug: str, page_type: str = "concept",
                confidence: str = "medium") -> float:
    """Calculate Ebbinghaus-inspired decay score for a page.

    Score 0.0-1.0. Higher = more relevant/alive. Low scores are archive candidates.

    Formula: base_importance * exp(-delta_t / half_life) + reinforcement * access_count

    - base_importance: from confidence (high=1.0, medium=0.7, low=0.4)
    - delta_t: days since last access (or creation if never accessed)
    - half_life: per type (debugging=30d, pattern=90d, concept=365d, decision=inf)
    - reinforcement: access_count * 0.05 (capped at 0.3)
    """
    import math

    base_map = {"high": 1.0, "medium": 0.7, "low": 0.4}
    base = base_map.get(confidence, 0.7)

    half_life_map = {
        "debugging": 30,
        "pattern": 90,
        "gap": 60,
        "qa": 180,
        "concept": 365,
        "decision": 99999,  # effectively never decay
        "entity": 99999,
        "synthesis": 365,
    }
    half_life = half_life_map.get(page_type, 180)

    stats = get_access_stats(slug)
    access_count = stats["total_count"]
    last_accessed = stats["last_accessed"]

    if last_accessed:
        try:
            last_dt = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
            now = datetime.now(last_dt.tzinfo) if last_dt.tzinfo else datetime.now()
            delta_days = max(0, (now - last_dt).days)
        except (ValueError, TypeError):
            delta_days = 0
    else:
        delta_days = 0  # Never accessed — treat as "just created" for decay

    # Decay component.
    decay = base * math.exp(-delta_days / half_life) if half_life < 99999 else base

    # Reinforcement from access.
    reinforcement = min(0.3, access_count * 0.05)

    return round(min(1.0, decay + reinforcement), 4)


def main() -> int:
    """CLI: show access stats or flush pending."""
    import argparse
    p = argparse.ArgumentParser(description="Access tracking for knowledge pages.")
    p.add_argument(
        "--flush",
        action="store_true",
        help="Explicitly export bounded durable telemetry to page frontmatter.",
    )
    p.add_argument("--stats", type=str, default=None, help="Show stats for a slug.")
    p.add_argument("--decay", type=str, default=None, help="Show decay score for a slug.")
    args = p.parse_args()

    if args.flush:
        n = flush_all()
        print(f"Exported durable telemetry to {n} page(s).")
        for failure in last_flush_failures():
            print(f"  not exported: {failure['slug']}: {failure['error']}")
        return 0

    if args.stats:
        stats = get_access_stats(args.stats)
        print(json.dumps(stats, indent=2))
        return 0

    if args.decay:
        score = decay_score(args.decay)
        print(f"Decay score for {args.decay}: {score}")
        return 0

    from retrieval_telemetry import TELEMETRY_DB

    print(f"Durable telemetry: {TELEMETRY_DB}")
    print(f"Legacy read-only access history: {ACCESS_LOG_FILE}")
    print("Use --flush for explicit bounded frontmatter promotion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
