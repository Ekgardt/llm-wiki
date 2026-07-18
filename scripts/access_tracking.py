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
from bounded_io import read_stable_bytes  # noqa: E402
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from reliable_memory import read_runtime_bytes, sha256_bytes  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
ACCESS_LOG_FILE = STATE_ROOT / "cache" / "access_log.jsonl"
MAX_ACCESS_PAGE_BYTES = 4 * 1024 * 1024
MAX_LEGACY_ACCESS_LOG_BYTES = 16 * 1024 * 1024
MAX_LEGACY_ACCESS_LOG_LINES = 100_000
MAX_PAGES_PER_EXPORT = 100
MAX_CANDIDATES_SCANNED_PER_EXPORT = 1_000
MAX_EVENTS_PER_PAGE_EXPORT = 1_000

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _integer_field_lines(name: str) -> re.Pattern[str]:
    quoted = rf'(?:{re.escape(name)}|"{re.escape(name)}"|\'{re.escape(name)}\')'
    return re.compile(rf"^[ \t]*{quoted}[ \t]*:.*$", re.MULTILINE)


def _parse_frontmatter_integer(fm: str, name: str) -> tuple[int | None, bool]:
    lines = list(_integer_field_lines(name).finditer(fm))
    if len(lines) > 1:
        raise ValueError(f"duplicate {name} field")
    if not lines:
        return None, False
    quoted = rf'(?:{re.escape(name)}|"{re.escape(name)}"|\'{re.escape(name)}\')'
    value = re.fullmatch(
        rf"[ \t]*{quoted}[ \t]*:[ \t]*"
        r'(?:"(?P<double>\d+)"|\'(?P<single>\d+)\'|(?P<plain>\d+))'
        r"(?:[ \t]+#.*)?[ \t]*",
        lines[0].group(0).removesuffix("\r"),
    )
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


def flush_access_to_frontmatter(slug: str | None = None) -> int:
    """Explicitly promote a bounded durable telemetry slice to frontmatter.

    Args:
        slug: If provided, flush only this page. If None, flush all pending.

    Returns:
        Number of pages updated.
    """
    from retrieval_telemetry import read_events_after

    if slug is None:
        return _flush_candidates_with_cursor()
    slugs = [slug]

    updated = 0
    for s in slugs:
        page_path = KNOWLEDGE_DIR / f"{s}.md"
        if not page_path.exists():
            continue

        try:
            source_bytes = read_stable_bytes(
                page_path, MAX_ACCESS_PAGE_BYTES, label="access tracking page"
            )
            content = source_bytes.decode("utf-8")

            fm_match = FRONTMATTER_RE.match(content)
            fm = fm_match.group(1) if fm_match else ""
            access_count, access_count_present = _parse_frontmatter_integer(
                fm, "access_count"
            )
            watermark_value, watermark_present = _parse_frontmatter_integer(
                fm, "access_telemetry_sequence"
            )
            watermark = watermark_value or 0
            events = read_events_after(
                s,
                after_sequence=watermark,
                limit=MAX_EVENTS_PER_PAGE_EXPORT,
            )
            if not events:
                continue
            count = len(events)
            first_sequence = events[0].sequence
            final_sequence = events[-1].sequence
            last_accessed = max(item.event.timestamp for item in events)

            if fm_match:
                fm = _set_frontmatter_integer(
                    fm,
                    "access_count",
                    (access_count or 0) + count,
                    access_count_present,
                )

                # Update or add last_accessed.
                if re.search(r"^last_accessed:", fm, re.MULTILINE):
                    fm = re.sub(
                        r"^last_accessed:.*$",
                        f"last_accessed: {last_accessed}",
                        fm,
                        count=1,
                        flags=re.MULTILINE,
                    )
                else:
                    fm += f"\nlast_accessed: {last_accessed}"

                fm = _set_frontmatter_integer(
                    fm,
                    "access_telemetry_sequence",
                    final_sequence,
                    watermark_present,
                )

                new_content = f"---\n{fm}\n---\n" + content[fm_match.end():]
            else:
                # No frontmatter — add one.
                new_content = (
                    f"---\naccess_count: {count}\nlast_accessed: {last_accessed}\n"
                    f"access_telemetry_sequence: {final_sequence}\n---\n\n{content}"
                )

            encoded = new_content.encode("utf-8")
            mutate_knowledge(
                stable_operation_id(
                    "access-telemetry",
                    f"{s}:{first_sequence}-{final_sequence}:{count}",
                    encoded,
                ),
                {page_path: encoded},
                preconditions={
                    page_path.relative_to(KNOWLEDGE_DIR.parent.parent).as_posix():
                        sha256_bytes(source_bytes)
                },
            )
            updated += 1
        except Exception:
            continue

    return updated


def _flush_candidates_with_cursor() -> int:
    from retrieval_telemetry import (
        get_export_cursor,
        list_candidate_ids,
        set_export_cursor,
    )

    try:
        cursor = get_export_cursor()
    except Exception:
        return 0
    may_wrap = bool(cursor)
    wrapped = False
    scanned = 0
    updated = 0

    while (
        scanned < MAX_CANDIDATES_SCANNED_PER_EXPORT
        and updated < MAX_PAGES_PER_EXPORT
    ):
        remaining = MAX_CANDIDATES_SCANNED_PER_EXPORT - scanned
        try:
            candidates = list_candidate_ids(
                after_candidate=cursor,
                limit=min(remaining, 1_000),
            )
        except Exception:
            return updated
        if not candidates:
            if may_wrap and not wrapped:
                cursor = ""
                wrapped = True
                continue
            break

        for candidate in candidates:
            if (
                scanned >= MAX_CANDIDATES_SCANNED_PER_EXPORT
                or updated >= MAX_PAGES_PER_EXPORT
            ):
                break
            page_path = KNOWLEDGE_DIR / f"{candidate}.md"
            if page_path.is_file():
                updated += flush_access_to_frontmatter(candidate)
            try:
                set_export_cursor(candidate)
            except Exception:
                return updated
            cursor = candidate
            scanned += 1

    return updated


def flush_all() -> int:
    """Explicitly promote bounded durable telemetry; never append JSONL."""
    return flush_access_to_frontmatter(None)


def get_access_stats(slug: str) -> dict:
    """Get bounded telemetry plus read-only legacy JSONL statistics.

    Returns:
        Dict with: total_count, last_accessed, sources (dict).
    """
    total = 0
    last_ts = None
    sources: dict[str, int] = {}

    try:
        from retrieval_telemetry import read_events

        for event in read_events(candidate_id=slug, limit=1_000):
            total += 1
            if last_ts is None or event.timestamp > last_ts:
                last_ts = event.timestamp
            sources[event.source_tool] = sources.get(event.source_tool, 0) + 1
    except Exception:
        pass

    try:
        if ACCESS_LOG_FILE.exists():
            raw = read_runtime_bytes(
                ACCESS_LOG_FILE,
                ACCESS_LOG_FILE.parent,
                max_bytes=MAX_LEGACY_ACCESS_LOG_BYTES,
            )
            for line in raw.decode("utf-8", errors="strict").splitlines()[
                :MAX_LEGACY_ACCESS_LOG_LINES
            ]:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if entry.get("slug") != slug:
                    continue
                total += 1
                ts = entry.get("timestamp", "")
                if isinstance(ts, str) and ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
                src = entry.get("source", "unknown")
                if not isinstance(src, str):
                    src = "unknown"
                sources[src] = sources.get(src, 0) + 1
    except (OSError, PermissionError, UnicodeError, ValueError):
        pass

    return {"total_count": total, "last_accessed": last_ts, "sources": sources}


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
