"""Session feedback loop — track if injected decisions are honored or contradicted.

Implements the repowise self-correcting flywheel:
1. At SessionStart, decisions are injected into context.
2. During the session, the agent may honor or contradict them.
3. At compile time, check if corrections in the session contradict injected decisions.
4. If contradicted: bump staleness. If honored: boost confidence.

This makes the system self-correcting — decisions that stop being true
stop being injected.

Usage:
    uv run python scripts/session_feedback.py --check     # check last session
    uv run python scripts/session_feedback.py --stats      # show feedback stats
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
DAILY_DIR = ROOT / "knowledge" / "daily"
FEEDBACK_FILE = STATE_ROOT / "run" / "session_feedback.json"

# Negation patterns indicating a correction (decision was contradicted).
CORRECTION_PATTERNS = [
    r"instead\s+of",
    r"not\s+anymore",
    r"don'?t\s+(use|do)",
    r"shouldn'?t",
    r"wrong",
    r"incorrect",
    r"actually",
    r"no[,\s]+we\s+(should|use|need)",
    r"changed\s+(to|from)",
    r"updated\s+(to|from)",
    r"replaced",
    r"deprecated",
]


def record_injection(slug: str, session_id: str = "") -> None:
    """Record that a decision was injected into a session."""
    data = _load_feedback()
    data.setdefault("injections", []).append({
        "slug": slug,
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    })
    _save_feedback(data)


def check_session_for_corrections(slug: str) -> str:
    """Check if recent daily logs contain corrections about a decision.

    Returns: 'honored', 'contradicted', or 'unknown'.
    """
    if not DAILY_DIR.exists():
        return "unknown"

    # Check last 3 days of daily logs for correction patterns mentioning the slug.
    now = datetime.now()
    for days_ago in range(3):
        date = now.timestamp() - (days_ago * 86400)
        date_str = datetime.fromtimestamp(date).strftime("%Y-%m-%d")
        daily_path = DAILY_DIR / f"{date_str}.md"
        if not daily_path.exists():
            continue

        try:
            content = daily_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Check if slug or its title is mentioned with correction patterns.
        slug_words = slug.replace("-", " ").lower()
        content_lower = content.lower()

        if slug.lower() in content_lower or slug_words in content_lower:
            for pattern in CORRECTION_PATTERNS:
                if re.search(pattern, content_lower):
                    return "contradicted"
            # Mentioned but no correction → honored.
            return "honored"

    return "unknown"


def update_decision_staleness(slug: str, status: str) -> None:
    """Update a decision's staleness based on feedback.

    'contradicted' → increase staleness score (decision may be outdated).
    'honored' → decrease staleness score (decision is valid).
    'unknown' → no change.
    """
    if status == "unknown":
        return

    data = _load_feedback()
    scores = data.setdefault("staleness_scores", {})
    current = scores.get(slug, 0.0)

    if status == "contradicted":
        current = min(1.0, current + 0.3)
    elif status == "honored":
        current = max(0.0, current - 0.1)

    scores[slug] = round(current, 2)
    data["last_checked"] = {slug: datetime.now().isoformat(timespec="seconds")}
    _save_feedback(data)


def get_staleness_score(slug: str) -> float:
    """Get the current staleness score for a decision (0.0 = fresh, 1.0 = stale)."""
    data = _load_feedback()
    return data.get("staleness_scores", {}).get(slug, 0.0)


def should_inject(slug: str, threshold: float = 0.7) -> bool:
    """Should this decision still be injected? False if too stale."""
    return get_staleness_score(slug) < threshold


def run_feedback_check(verbose: bool = True) -> dict:
    """Check all injected decisions for corrections. Returns stats."""
    data = _load_feedback()
    injections = data.get("injections", [])
    if not injections:
        return {"checked": 0, "honored": 0, "contradicted": 0, "unknown": 0}

    # Get unique slugs from recent injections.
    checked_slugs: set[str] = set()
    stats = {"checked": 0, "honored": 0, "contradicted": 0, "unknown": 0}

    for inj in injections[-50:]:  # Last 50 injections.
        slug = inj.get("slug", "")
        if not slug or slug in checked_slugs:
            continue
        checked_slugs.add(slug)

        status = check_session_for_corrections(slug)
        update_decision_staleness(slug, status)
        stats["checked"] += 1
        stats[status] += 1

        if verbose and status != "unknown":
            print(f"  {slug}: {status}")

    # Clear processed injections.
    data["injections"] = []
    _save_feedback(data)

    return stats


def _load_feedback() -> dict:
    """Load feedback data from JSON file."""
    if not FEEDBACK_FILE.exists():
        return {}
    try:
        return json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_feedback(data: dict) -> None:
    """Save feedback data to JSON file."""
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(FEEDBACK_FILE, json.dumps(data, indent=2))


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Session feedback loop — decision tracking.")
    p.add_argument("--check", action="store_true", help="Check recent sessions for corrections.")
    p.add_argument("--stats", action="store_true", help="Show feedback statistics.")
    args = p.parse_args()

    if args.stats:
        data = _load_feedback()
        scores = data.get("staleness_scores", {})
        print(f"Tracked decisions: {len(scores)}")
        for slug, score in sorted(scores.items(), key=lambda x: -x[1]):
            print(f"  {slug}: {score} ({'stale' if score > 0.7 else 'fresh'})")
        return 0

    if args.check:
        stats = run_feedback_check()
        print(f"Checked: {stats['checked']}, "
              f"Honored: {stats['honored']}, "
              f"Contradicted: {stats['contradicted']}, "
              f"Unknown: {stats['unknown']}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
