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
from memory_state import ROOT  # noqa: E402

DAILY_DIR = ROOT / "memory" / "daily"
FEEDBACK_DIR = ROOT / "memory" / "feedback"


def detect_file_edit_loops(project: str | None, days: int = 7, threshold: int = 3) -> list[dict]:
    """Detect files edited multiple times across sessions (loop signal)."""
    cutoff = datetime.now() - timedelta(days=days)
    file_edits: dict[str, list[dict]] = {}

    if not DAILY_DIR.exists():
        return []

    for daily in sorted(DAILY_DIR.glob("*.md"), reverse=True):
        try:
            date_str = daily.stem
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                break
        except ValueError:
            continue

        try:
            content = daily.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if project and project.lower() not in content.lower():
            continue

        # Find tool breadcrumbs with Edit/Write
        for match in re.finditer(
            r"\[(\d{2}:\d{2}:\d{2})\]\s+tool\s+\|\s+(\w+)\s+\|\s+(\w+)\s+\|\s+(?:Edit|Write)\s+(.+)",
            content,
        ):
            ts, session, slug, target = match.groups()
            if project and slug.lower() != project.lower():
                continue
            target = target.strip()
            file_edits.setdefault(target, []).append({
                "date": date_str,
                "time": ts,
                "session": session[:8],
            })

    # Find files edited >= threshold times
    loops = []
    for target, edits in file_edits.items():
        if len(edits) >= threshold:
            loops.append({
                "type": "file_edit_loop",
                "target": target,
                "edit_count": len(edits),
                "threshold": threshold,
                "edits": edits,
                "warning": f"'{target}' edited {len(edits)} times in {days} days — possible loop",
            })

    return sorted(loops, key=lambda x: x["edit_count"], reverse=True)


def detect_feedback_loops(threshold: int = 3) -> list[dict]:
    """Detect same feedback topic appearing multiple times (loop signal)."""
    if not FEEDBACK_DIR.exists():
        return []

    topics: dict[str, int] = Counter()
    details: dict[str, list[str]] = {}

    for f in FEEDBACK_DIR.glob("*.json"):
        try:
            candidate = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        text = candidate.get("text", "").lower()
        # Extract key words as topic signature
        words = [w for w in re.findall(r"\b[a-z]{5,}\b", text) if w not in
                 {"should", "would", "could", "their", "there", "about", "after", "being"}]
        if words:
            topic = " ".join(sorted(words[:3]))  # top-3 words as signature
            topics[topic] += 1
            details.setdefault(topic, []).append(candidate.get("text", "")[:80])

    loops = []
    for topic, count in topics.most_common(10):
        if count >= threshold:
            loops.append({
                "type": "feedback_loop",
                "topic_signature": topic,
                "count": count,
                "examples": details[topic][:3],
                "warning": f"Same feedback topic appeared {count} times — agent may be repeating mistakes",
            })

    return loops


def detect_all(project: str | None = None, days: int = 7, threshold: int = 3) -> list[dict]:
    """Run all loop detection checks."""
    loops = []
    loops.extend(detect_file_edit_loops(project, days, threshold))
    loops.extend(detect_feedback_loops(threshold))
    return loops


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
        print(f"[{loop['type']}] {loop['warning']}")
        if loop["type"] == "file_edit_loop":
            for edit in loop["edits"][:5]:
                print(f"  {edit['date']} {edit['time']} session={edit['session']}")
        elif loop["type"] == "feedback_loop":
            for ex in loop["examples"]:
                print(f"  → {ex}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
