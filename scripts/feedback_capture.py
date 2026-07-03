"""Feedback capture — learns from user corrections.

When the user corrects the agent ("no, use this instead", "actually,
we decided X"), this module detects the correction and saves it as
a feedback candidate. Candidates are promoted to knowledge pages
only when the user confirms — nothing is auto-promoted.

Inspired by nvk/llm-wiki's feedback curator (v0.12.0).

Detection patterns:
- "no, " / "not " / "actually " / "instead " → correction
- "remember that" / "don't forget" → explicit instruction
- "I prefer" / "always use" / "never use" → preference
- User rejecting an agent's suggestion → implicit correction

Usage (called from flush_memory.py or plugin on session.idle):
    uv run python scripts/feedback_capture.py --transcript <path>
    uv run python scripts/feedback_capture.py --list
    uv run python scripts/feedback_capture.py --promote <id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

FEEDBACK_DIR = ROOT / "memory" / "feedback"

# Patterns that indicate a user correction or preference
CORRECTION_PATTERNS = [
    (re.compile(r"\b(no|not|actually|instead|wait|stop)\b", re.IGNORECASE), "correction"),
    (re.compile(r"\b(remember|don'?t forget|keep in mind)\b", re.IGNORECASE), "instruction"),
    (re.compile(r"\b(I prefer|always use|never use|we (always|never))\b", re.IGNORECASE), "preference"),
    (re.compile(r"\b(wrong|incorrect|that'?s not|not right)\b", re.IGNORECASE), "rejection"),
    (re.compile(r"\b(should (be|use)|need to|must)\b", re.IGNORECASE), "requirement"),
]

# Patterns to ignore (noise)
NOISE_PATTERNS = re.compile(
    r"^(ok|okay|thanks|thank you|cool|nice|great|got it|sure|yes|yep|no problem|"
    r"make sense|sounds good|perfect|awesome|lol|haha|👀|👍|✅)\s*$",
    re.IGNORECASE,
)


def _detect_feedback_type(text: str) -> tuple[str | None, float]:
    """Detect if a text contains a correction/preference/instruction.

    Returns (type, confidence) or (None, 0).
    """
    if not text or len(text.strip()) < 10:
        return None, 0.0
    if NOISE_PATTERNS.match(text.strip()):
        return None, 0.0

    matches = []
    for pattern, ftype in CORRECTION_PATTERNS:
        if pattern.search(text):
            matches.append((ftype, 0.7))

    if not matches:
        return None, 0.0

    # Higher confidence if multiple patterns match
    best_type, best_conf = max(matches, key=lambda x: x[1])
    if len(matches) >= 2:
        best_conf = min(1.0, best_conf + 0.2)

    return best_type, best_conf


def capture_from_text(
    text: str,
    session_id: str = "unknown",
    slug: str = "unknown",
    trigger: str = "session-end",
) -> str | None:
    """Check if a text block contains feedback worth saving.

    Returns the candidate ID if saved, None if not.
    """
    ftype, confidence = _detect_feedback_type(text)
    if not ftype or confidence < 0.5:
        return None

    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

    # Create candidate record
    candidate_id = hashlib.sha256(
        f"{text}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:12]

    candidate = {
        "id": candidate_id,
        "type": ftype,
        "confidence": round(confidence, 2),
        "text": text.strip()[:500],
        "session_id": session_id,
        "project": slug,
        "trigger": trigger,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "status": "candidate",
    }

    # Write to feedback dir
    out = FEEDBACK_DIR / f"{candidate_id}.json"
    out.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return candidate_id


def list_candidates(status: str = "candidate") -> list[dict]:
    """List all feedback candidates."""
    if not FEEDBACK_DIR.exists():
        return []
    candidates = []
    for p in sorted(FEEDBACK_DIR.glob("*.json")):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if c.get("status") == status:
                candidates.append(c)
        except (json.JSONDecodeError, OSError):
            continue
    return candidates


def promote_candidate(candidate_id: str, category: str = "patterns") -> str | None:
    """Promote a feedback candidate to a knowledge page.

    Creates memory/knowledge/<category>/feedback-<id>.md with the
    feedback text as the page body.
    """
    candidate_file = FEEDBACK_DIR / f"{candidate_id}.json"
    if not candidate_file.exists():
        return None

    try:
        candidate = json.loads(candidate_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # Create knowledge page
    knowledge_dir = ROOT / "memory" / "knowledge" / category
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    page_name = f"feedback-{candidate_id[:8]}.md"
    page_path = knowledge_dir / page_name

    page_content = (
        "---\n"
        f"type: {candidate['type']}\n"
        f'title: "User feedback: {candidate["text"][:60]}..."\n'
        f'description: "Captured from {candidate["project"]} session"\n'
        f"timestamp: {candidate['captured_at']}\n"
        f"project: {candidate['project']}\n"
        f"confidence: {candidate['confidence']}\n"
        f"source_authority: user\n"
        "---\n\n"
        f"# User feedback ({candidate['type']})\n\n"
        f"One-sentence summary: {candidate['text'][:120]}\n\n"
        f"## {candidate['type'].title()}\n"
        f"{candidate['text']}\n\n"
        f"## Evidence\n"
        f"- Captured from session `{candidate['session_id']}` "
        f"in project `{candidate['project']}` "
        f"({candidate['trigger']})\n"
        f"- Confidence: {candidate['confidence']}\n\n"
        f"## Related\n"
        f"- [[memory/feedback/{candidate_id}.json]]\n"
    )
    page_path.write_text(page_content, encoding="utf-8")

    # Update candidate status
    candidate["status"] = "promoted"
    candidate["promoted_to"] = page_path.relative_to(ROOT).as_posix()
    candidate_file.write_text(
        json.dumps(candidate, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return page_path.relative_to(ROOT).as_posix()


def main() -> int:
    p = argparse.ArgumentParser(description="Feedback capture and management.")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="List unpromoted feedback candidates")
    sub.add_parser("list-all", help="List all feedback (including promoted)")

    promote = sub.add_parser("promote", help="Promote a candidate to knowledge page")
    promote.add_argument("id", help="Candidate ID")
    promote.add_argument("--category", default="patterns", help="Knowledge category")

    args = p.parse_args()

    if args.command == "list":
        candidates = list_candidates("candidate")
        if not candidates:
            print("(no feedback candidates)")
            return 0
        print(f"Feedback candidates ({len(candidates)}):\n")
        for c in candidates:
            print(f"  [{c['id'][:8]}] ({c['type']}, conf={c['confidence']}) {c['text'][:80]}...")
            print(f"    project: {c['project']}, captured: {c['captured_at']}")
            print()
    elif args.command == "list-all":
        all_c = list_candidates("candidate") + list_candidates("promoted")
        print(f"All feedback ({len(all_c)}):\n")
        for c in all_c:
            status = "✅" if c["status"] == "promoted" else "⏳"
            print(f"  {status} [{c['id'][:8]}] ({c['type']}) {c['text'][:60]}...")
    elif args.command == "promote":
        result = promote_candidate(args.id, args.category)
        if result:
            print(f"Promoted to: {result}")
        else:
            print(f"Candidate {args.id} not found")
            return 1
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
