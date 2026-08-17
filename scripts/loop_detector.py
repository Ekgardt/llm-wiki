"""Loop detector — prevents infinite "fix → review → redo" cycles.

When agents work in parallel or sequence, they can enter loops:
- Agent A fixes bug X
- Agent B reviews, asks for changes
- Agent A fixes again (slightly differently)
- Agent B reviews, asks for more changes
- ... repeat forever

This module detects such loops by tracking "fix attempts" per file/topic
and warning when the same target has been modified N times without
resolution.

Detection signals:
- Same file edited >3 times in one day by different agents
- Same topic/keyword appearing in >3 feedback candidates
- Same error message in >2 daily logs

Usage:
    uv run python scripts/loop_detector.py --project your-project
    uv run python scripts/loop_detector.py --project your-project --threshold 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_timeline import block_agent, parse_tool_breadcrumb  # noqa: E402
from memory_state import ROOT  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
FEEDBACK_DIR = ROOT / "knowledge" / "feedback"
EDIT_TOOLS = frozenset({"edit", "write", "multiedit", "multi_edit", "notebookedit", "notebook_edit"})
ERROR_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:(?:error|failure|failed)\s*:\s*(?P<generic>.+?)|"
    r"(?P<exception>[A-Za-z_][\w.]*(?:Error|Exception)\s*:\s*.+?))\s*$",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"\b\d+\b")


def _recent_daily_files(days: int) -> list[Path]:
    cutoff = datetime.now() - timedelta(days=days)
    if not DAILY_DIR.exists():
        return []
    recent = []
    for source in sorted(DAILY_DIR.glob("*.md"), reverse=True):
        try:
            file_date = datetime.strptime(source.stem, "%Y-%m-%d")
        except ValueError:
            continue
        if file_date >= cutoff:
            recent.append(source)
    return recent


def _daily_content(daily: Path, project: str | None) -> str:
    try:
        content = daily.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if project and project.lower() not in content.lower():
        return ""
    return content


def _is_project_edit(item: dict[str, str], project: str | None) -> bool:
    if item["tool"].lower() not in EDIT_TOOLS:
        return False
    return project is None or item["slug"].lower() == project.lower()


def _edit_from_line(
    line: str, date_str: str, project: str | None
) -> tuple[str, dict[str, str]] | None:
    item = parse_tool_breadcrumb(line)
    if item is None:
        return None
    if not _is_project_edit(item, project):
        return None
    target = item["target"] or "(unknown)"
    return target, {
        "date": date_str,
        "time": item["time"],
        "session": item["session"][:8],
        "agent": item["agent"],
    }


def _daily_edits(
    daily: Path, project: str | None
) -> list[tuple[str, dict[str, str]]]:
    edits = []
    for line in _daily_content(daily, project).splitlines():
        edit = _edit_from_line(line, daily.stem, project)
        if edit is not None:
            edits.append(edit)
    return edits


def _collect_file_edits(project: str | None, days: int) -> dict[str, list[dict[str, str]]]:
    file_edits: dict[str, list[dict[str, str]]] = {}
    for daily in _recent_daily_files(days):
        for target, record in _daily_edits(daily, project):
            file_edits.setdefault(target, []).append(record)
    return file_edits


def _file_loop(
    target: str, edits: list[dict[str, str]], threshold: int, days: int
) -> dict[str, object]:
    known_agents = sorted({item["agent"] for item in edits} - {"unknown"})
    loop_type = "single_agent_churn"
    if len(known_agents) > 1:
        loop_type = "multi_agent_loop"
    agents = known_agents or ["unknown"]
    return {
        "type": loop_type,
        "target": target,
        "agents": agents,
        "edit_count": len(edits),
        "threshold": threshold,
        "edits": edits,
        "warning": f"'{target}' edited {len(edits)} times in {days} days - possible loop",
    }


def detect_file_edit_loops(
    project: str | None, days: int = 7, threshold: int = 3
) -> list[dict]:
    """Classify repeated file edits as single- or multi-agent loops."""
    file_edits = _collect_file_edits(project, days)
    loops = [
        _file_loop(target, edits, threshold, days)
        for target, edits in file_edits.items()
        if len(edits) >= threshold
    ]
    return sorted(loops, key=lambda item: int(item["edit_count"]), reverse=True)


def _feedback_candidate(path: Path) -> dict[str, object] | None:
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return candidate if isinstance(candidate, dict) else None


def _feedback_topic(candidate: dict[str, object]) -> str | None:
    text = str(candidate.get("text", "")).lower()
    excluded = {"should", "would", "could", "their", "there", "about", "after", "being"}
    words = [
        word
        for word in re.findall(r"\b[^\W\d_]{5,}\b", text, re.UNICODE)
        if word not in excluded
    ]
    if not words:
        return None
    return " ".join(sorted(words[:3]))


def _collect_feedback_topics() -> tuple[Counter, dict[str, list[str]]]:
    topics: Counter = Counter()
    details: dict[str, list[str]] = {}
    for path in FEEDBACK_DIR.glob("*.json"):
        candidate = _feedback_candidate(path)
        topic = _feedback_topic(candidate) if candidate is not None else None
        if topic is not None:
            topics[topic] += 1
            details.setdefault(topic, []).append(str(candidate.get("text", ""))[:80])
    return topics, details


def _feedback_loop(
    topic: str, count: int, details: dict[str, list[str]]
) -> dict[str, object]:
    return {
        "type": "feedback_loop",
        "topic_signature": topic,
        "count": count,
        "examples": details[topic][:3],
        "warning": f"Same feedback topic appeared {count} times - agent may be repeating mistakes",
    }


def detect_feedback_loops(threshold: int = 3) -> list[dict]:
    """Detect same feedback topic appearing multiple times (loop signal)."""
    if not FEEDBACK_DIR.exists():
        return []
    topics, details = _collect_feedback_topics()
    return [
        _feedback_loop(topic, count, details)
        for topic, count in topics.most_common(10)
        if count >= threshold
    ]


def normalize_error(message: str) -> str:
    """Collapse volatile IDs and numbers without dropping the error family."""
    normalized = UUID_RE.sub("<uuid>", message.casefold())
    normalized = HEX_RE.sub("<hex>", normalized)
    normalized = NUMBER_RE.sub("<n>", normalized)
    return " ".join(normalized.split())


def _error_from_line(line: str, agent: str, date_str: str) -> dict[str, str] | None:
    match = ERROR_RE.match(line)
    if match is None:
        return None
    message = match.group("generic") or match.group("exception")
    return {
        "signature": normalize_error(message),
        "message": message[:200],
        "agent": agent,
        "date": date_str,
    }


def _errors_from_block(block: str, date_str: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    agent = block_agent(block)
    for line in block.splitlines():
        record = _error_from_line(line, agent, date_str)
        if record is not None:
            errors.append(record)
    return errors


def _errors_from_content(content: str, date_str: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for block in re.split(r"(?=^## \[)", content, flags=re.MULTILINE):
        errors.extend(_errors_from_block(block, date_str))
    return errors


def _collect_errors(project: str | None, days: int) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for daily in _recent_daily_files(days):
        records = _errors_from_content(_daily_content(daily, project), daily.stem)
        for record in records:
            grouped.setdefault(record["signature"], []).append(record)
    return grouped


def _recurring_error_loop(
    signature: str, records: list[dict[str, str]], threshold: int
) -> dict[str, object]:
    return {
        "type": "recurring_error",
        "signature": signature,
        "occurrence_count": len(records),
        "day_count": len({record["date"] for record in records}),
        "agents": sorted({record["agent"] for record in records} - {"unknown"}),
        "threshold": threshold,
        "examples": [record["message"] for record in records[:3]],
        "warning": f"Normalized error recurred on {len({record['date'] for record in records})} days",
    }


def detect_recurring_errors(
    project: str | None, days: int = 7, threshold: int = 3
) -> list[dict]:
    """Group normalized error families recurring across distinct daily logs."""
    grouped = _collect_errors(project, days)
    loops = [
        _recurring_error_loop(signature, records, threshold)
        for signature, records in grouped.items()
        if len({record["date"] for record in records}) >= threshold
    ]
    return sorted(loops, key=lambda item: int(item["day_count"]), reverse=True)


def detect_all(project: str | None = None, days: int = 7, threshold: int = 3) -> list[dict]:
    """Run all loop detection checks."""
    loops = []
    loops.extend(detect_file_edit_loops(project, days, threshold))
    loops.extend(detect_feedback_loops(threshold))
    loops.extend(detect_recurring_errors(project, days, threshold))
    return loops


def _edit_details(loop: dict) -> list[str]:
    return [
        f"  {edit['date']} {edit['time']} "
        f"agent={edit['agent']} session={edit['session']}"
        for edit in loop["edits"][:5]
    ]


def _example_details(loop: dict) -> list[str]:
    return [f"  -> {example}" for example in loop["examples"]]


def _no_details(_loop: dict) -> list[str]:
    return []


def _loop_details(loop: dict) -> list[str]:
    handlers = {
        "single_agent_churn": _edit_details,
        "multi_agent_loop": _edit_details,
        "feedback_loop": _example_details,
        "recurring_error": _example_details,
    }
    return handlers.get(loop["type"], _no_details)(loop)


def _print_loop(loop: dict) -> None:
    print(f"[{loop['type']}] {loop['warning']}")
    for line in _loop_details(loop):
        print(line)
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="Loop detector for multi-agent coordination.")
    p.add_argument("--project", default=None, help="Filter by project")
    p.add_argument("--days", type=int, default=7, help="Look-back window")
    p.add_argument("--threshold", type=int, default=3, help="Loop threshold (N repetitions)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    loops = detect_all(args.project, args.days, args.threshold)

    if args.json:
        print(json.dumps(loops, indent=2, ensure_ascii=False))
        return 0

    if not loops:
        print("No loops detected.")
        return 0

    print(f"Detected {len(loops)} potential loop(s):\n")
    for loop in loops:
        _print_loop(loop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
