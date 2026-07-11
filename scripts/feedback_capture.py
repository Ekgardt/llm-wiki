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
    # OpenCode plugin: JSON on stdin (no args)
    echo '{"text":"...","session_id":"...","slug":"..."}' | uv run python scripts/feedback_capture.py
    uv run python scripts/feedback_capture.py capture --transcript <path>
    uv run python scripts/feedback_capture.py list
    uv run python scripts/feedback_capture.py promote <id>
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
from memory_state import ROOT, atomic_write  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

FEEDBACK_DIR = ROOT / "knowledge" / "feedback"

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

    # Redact secrets from the feedback text before persisting it (mirrors
    # the secret_redact pass that all capture hooks run).
    text = redact_secrets(text)

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
    atomic_write(
        out,
        json.dumps(candidate, indent=2, ensure_ascii=False),
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


ALLOWED_FEEDBACK_CATEGORIES = frozenset(
    {"patterns", "decisions", "debugging", "concepts", "qa", "workflow"}
)

# Feedback classification types are NOT canonical OKF types. Map them to
# the closest canonical type (see okf_types.CANONICAL_TYPES) so promoted
# pages pass lint. The original classification is preserved in the
# `feedback_type:` frontmatter field for traceability.
_FEEDBACK_TYPE_MAP: dict[str, str] = {
    "correction": "pattern",
    "instruction": "pattern",
    "preference": "decision",
    "rejection": "decision",
    "requirement": "qa",
    "concepts": "concept",
    "workflow": "workflow",
}


def promote_candidate(candidate_id: str, category: str = "patterns") -> str | None:
    """Promote a feedback candidate to a knowledge page.

    Creates knowledge/notes/<category>/feedback-<id>.md with the
    feedback text as the page body.
    """
    # candidate_id is a SHA-256 hash prefix (hex). Reject anything that
    # could traverse outside FEEDBACK_DIR (path traversal, H-009).
    if not re.match(r"^[a-f0-9]{6,64}$", candidate_id or ""):
        return None

    category = (category or "patterns").strip().lower()
    if category not in ALLOWED_FEEDBACK_CATEGORIES or "/" in category or ".." in category:
        return None

    candidate_file = FEEDBACK_DIR / f"{candidate_id}.json"
    if not candidate_file.exists():
        return None

    try:
        candidate = json.loads(candidate_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    # Create knowledge page (containment-checked, flat layout)
    notes_root = (ROOT / "knowledge" / "notes").resolve()
    knowledge_dir = notes_root  # flat layout
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    page_name = f"feedback-{candidate_id[:8]}.md"
    page_path = knowledge_dir / page_name

    # Containment guard: the resolved page path must stay inside the
    # knowledge root (defense-in-depth on top of the category whitelist).
    if not page_path.resolve().is_relative_to(notes_root):
        return None

    # YAML-escape interpolated fields (backslashes, quotes, newlines) using
    # the same pattern as compile_memory.py to prevent frontmatter injection.
    def _esc(s: str) -> str:
        return (
            str(s)
            .replace(chr(92), chr(92) + chr(92))
            .replace(chr(34), chr(92) + chr(34))
            .replace(chr(10), " ")
            .replace(chr(13), " ")
        )

    # Map category to canonical type if provided. The `--category` CLI
    # arg otherwise only affected the (now-flat) path, so an explicit
    # category was silently ignored in the frontmatter.
    if category and category != "patterns":
        type_from_category = {
            "debugging": "debugging",
            "qa": "qa",
            "decisions": "decision",
            "concepts": "concept",
            "workflow": "workflow",
        }.get(category)
        if type_from_category:
            page_type = type_from_category
        else:
            page_type = _FEEDBACK_TYPE_MAP.get(candidate.get("type", ""), "pattern")
    else:
        page_type = _FEEDBACK_TYPE_MAP.get(candidate.get("type", ""), "pattern")

    page_content = (
        "---\n"
        f"type: {_esc(page_type)}\n"
        f"feedback_type: {_esc(candidate['type'])}\n"
        f'title: "{_esc("User feedback: " + candidate["text"][:60])}..."\n'
        f'description: "{_esc("Captured from " + candidate["project"] + " session")}"\n'
        f"timestamp: {_esc(candidate['captured_at'])}\n"
        f"project: {_esc(candidate['project'])}\n"
        f"confidence: {_esc(candidate['confidence'])}\n"
        f"source_authority: user\n"
        "---\n\n"
        f"# User feedback ({candidate['type']})\n\n"
        f"One-sentence summary: {_esc(candidate['text'][:120])}\n\n"
        f"## {candidate['type'].title()}\n"
        f"{_esc(candidate['text'])}\n\n"
        f"## Evidence\n"
        f"- Captured from session `{_esc(candidate['session_id'])}` "
        f"in project `{_esc(candidate['project'])}` "
        f"({_esc(candidate['trigger'])})\n"
        f"- Confidence: {candidate['confidence']}\n\n"
        f"## Related\n"
        f"- [[knowledge/feedback/{candidate_id}.json]]\n"
    )
    atomic_write(page_path, page_content)

    # Update candidate status
    candidate["status"] = "promoted"
    candidate["promoted_to"] = page_path.relative_to(ROOT).as_posix()
    atomic_write(
        candidate_file,
        json.dumps(candidate, indent=2, ensure_ascii=False),
    )

    return page_path.relative_to(ROOT).as_posix()


def _capture_from_stdin() -> int:
    """OpenCode plugin contract: JSON on stdin, no CLI args.

    Payload: {"text": "...", "session_id": "...", "slug": "...", "trigger": "..."}
    """
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    text = str(payload.get("text") or "")
    if not text.strip():
        return 0
    cid = capture_from_text(
        text,
        session_id=str(payload.get("session_id") or "unknown"),
        slug=str(payload.get("slug") or "unknown"),
        trigger=str(payload.get("trigger") or "stdin"),
    )
    if cid:
        print(cid)
    return 0


def main() -> int:
    # No args + non-TTY stdin → capture path (OpenCode plugin).
    if len(sys.argv) == 1 and not sys.stdin.isatty():
        return _capture_from_stdin()

    p = argparse.ArgumentParser(description="Feedback capture and management.")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="List unpromoted feedback candidates")
    sub.add_parser("list-all", help="List all feedback (including promoted)")

    promote = sub.add_parser("promote", help="Promote a candidate to knowledge page")
    promote.add_argument("id", help="Candidate ID")
    promote.add_argument("--category", default="patterns", help="Knowledge page type for frontmatter (e.g. patterns, debugging, qa). Does not affect the path — all pages are flat under knowledge/notes/.")

    capture = sub.add_parser("capture", help="Capture feedback from text or transcript")
    capture.add_argument("--text", default="", help="Raw feedback text")
    capture.add_argument("--transcript", default="", help="Path to transcript file")
    capture.add_argument("--session-id", default="unknown")
    capture.add_argument("--slug", default="unknown")
    capture.add_argument("--trigger", default="cli")

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
    elif args.command == "capture":
        text = args.text or ""
        if args.transcript:
            try:
                text = Path(args.transcript).read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                print(f"feedback_capture: cannot read transcript: {e}", file=sys.stderr)
                return 1
        if not text.strip():
            print("feedback_capture: no text provided", file=sys.stderr)
            return 1
        cid = capture_from_text(
            text,
            session_id=args.session_id,
            slug=args.slug,
            trigger=args.trigger,
        )
        if cid:
            print(cid)
        else:
            print("(no feedback detected)")
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
