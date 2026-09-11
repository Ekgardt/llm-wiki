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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from markdown_transaction import ABSENT, mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

FEEDBACK_DIR = ROOT / "knowledge" / "feedback"
MAX_FEEDBACK_CANDIDATE_BYTES = 1024 * 1024


def _redact_feedback(value):
    """Recursively redact every string in feedback content and metadata."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {
            redact_secrets(str(key)): _redact_feedback(item)
            for key, item in value.items()
        }
    return _redact_sequence(value)


def _redact_sequence(value):
    """Lists and tuples come back as redacted lists; any other value unchanged."""
    if isinstance(value, (list, tuple)):
        return [_redact_feedback(item) for item in value]
    return value

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
    if _feedback_noise(text):
        return None, 0.0
    matches = [(ftype, 0.7) for pattern, ftype in CORRECTION_PATTERNS if pattern.search(text)]
    if not matches:
        return None, 0.0
    best_type, best_conf = max(matches, key=lambda x: x[1])
    return best_type, _boosted_confidence(best_conf, len(matches))


def _feedback_noise(text: str) -> bool:
    if not text or len(text.strip()) < 10:
        return True
    return bool(NOISE_PATTERNS.match(text.strip()))


def _boosted_confidence(confidence: float, match_count: int) -> float:
    # Higher confidence if multiple patterns match
    if match_count >= 2:
        return min(1.0, confidence + 0.2)
    return confidence


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
    candidate = _redact_feedback(candidate)

    # Write to feedback dir
    out = FEEDBACK_DIR / f"{candidate_id}.json"
    encoded = json.dumps(candidate, indent=2, ensure_ascii=False).encode("utf-8")
    mutate_knowledge(
        stable_operation_id("feedback-capture", candidate_id, encoded),
        {out: encoded},
        preconditions={out.relative_to(ROOT).as_posix(): ABSENT},
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


_CATEGORY_TYPES = {
    "debugging": "debugging",
    "qa": "qa",
    "decisions": "decision",
    "concepts": "concept",
    "workflow": "workflow",
}


def promote_candidate(candidate_id: str, category: str = "patterns") -> str | None:
    """Promote a feedback candidate to a knowledge page.

    Creates knowledge/notes/<category>/feedback-<id>.md with the
    feedback text as the page body.
    """
    promotion = _promotion(candidate_id, category)
    if promotion is None:
        return None
    page_key = promotion.page_path.relative_to(ROOT).as_posix()
    mutate_knowledge(
        stable_operation_id(
            "feedback-promote", candidate_id, promotion.page_bytes + promotion.candidate_bytes
        ),
        {promotion.page_path: promotion.page_bytes, promotion.candidate_file: promotion.candidate_bytes},
        preconditions={
            page_key: ABSENT,
            promotion.candidate_file.relative_to(ROOT).as_posix(): sha256_bytes(
                promotion.candidate_bytes_before
            ),
        },
    )
    return page_key


@dataclass(frozen=True)
class _Promotion:
    """The page to create and the candidate's new bytes, bound to the bytes it was read as."""

    page_path: Path
    page_bytes: bytes
    candidate_file: Path
    candidate_bytes: bytes
    candidate_bytes_before: bytes


def _promotion(candidate_id: str, category: str) -> _Promotion | None:
    category = _allowed_category(candidate_id, category)
    if category is None:
        return None
    candidate_file = FEEDBACK_DIR / f"{candidate_id}.json"
    loaded = _read_candidate(candidate_file)
    if loaded is None:
        return None
    return _prepared_promotion(candidate_id, category, candidate_file, *loaded)


def _allowed_category(candidate_id: str, category: str) -> str | None:
    """The normalized category when both the ID and the category are acceptable."""
    # candidate_id is a SHA-256 hash prefix (hex). Reject anything that
    # could traverse outside FEEDBACK_DIR (path traversal, H-009).
    if not re.match(r"^[a-f0-9]{6,64}$", candidate_id or ""):
        return None
    category = (category or "patterns").strip().lower()
    if category not in ALLOWED_FEEDBACK_CATEGORIES:
        return None
    return category


def _read_candidate(candidate_file: Path) -> tuple[dict, bytes] | None:
    """(redacted candidate, its bytes as read); None when absent or unreadable."""
    if not candidate_file.exists():
        return None
    try:
        candidate_bytes_before = read_stable_bytes(
            candidate_file,
            MAX_FEEDBACK_CANDIDATE_BYTES,
            label="feedback candidate",
        )
        candidate = json.loads(candidate_bytes_before.decode("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None
    return _redact_feedback(candidate), candidate_bytes_before


def _prepared_promotion(
    candidate_id: str, category: str, candidate_file: Path, candidate: dict, candidate_bytes_before: bytes
) -> _Promotion | None:
    # Create knowledge page (containment-checked, flat layout)
    notes_root = (ROOT / "knowledge" / "notes").resolve()
    page_path = notes_root / f"feedback-{candidate_id[:8]}.md"
    # Containment guard: the resolved page path must stay inside the
    # knowledge root (defense-in-depth on top of the category whitelist).
    if not page_path.resolve().is_relative_to(notes_root):
        return None
    page_bytes = _promoted_page(candidate, candidate_id, _promoted_type(category, candidate)).encode("utf-8")
    # Update candidate status
    candidate["status"] = "promoted"
    candidate["promoted_to"] = page_path.relative_to(ROOT).as_posix()
    candidate_bytes = json.dumps(candidate, indent=2, ensure_ascii=False).encode("utf-8")
    return _Promotion(page_path, page_bytes, candidate_file, candidate_bytes, candidate_bytes_before)


def _promoted_type(category: str, candidate: dict) -> str:
    """The page type an explicit category names; otherwise the candidate's mapped type.

    The `--category` CLI arg otherwise only affected the (now-flat) path, so
    an explicit category was silently ignored in the frontmatter.
    """
    return _CATEGORY_TYPES.get(category) or _FEEDBACK_TYPE_MAP.get(candidate.get("type", ""), "pattern")


def _yaml_escape(s: str) -> str:
    """YAML-escape interpolated fields (backslashes, quotes, newlines) like compile_memory.py."""
    return (
        str(s)
        .replace(chr(92), chr(92) + chr(92))
        .replace(chr(34), chr(92) + chr(34))
        .replace(chr(10), " ")
        .replace(chr(13), " ")
    )


def _promoted_page(candidate: dict, candidate_id: str, page_type: str) -> str:
    _esc = _yaml_escape
    return (
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


def _capture_from_stdin() -> int:
    """OpenCode plugin contract: JSON on stdin, no CLI args.

    Payload: {"text": "...", "session_id": "...", "slug": "...", "trigger": "..."}
    """
    payload = _stdin_payload()
    if payload is None:
        return 0
    cid = capture_from_text(
        _payload_field(payload, "text", ""),
        session_id=_payload_field(payload, "session_id", "unknown"),
        slug=_payload_field(payload, "slug", "unknown"),
        trigger=_payload_field(payload, "trigger", "stdin"),
    )
    if cid:
        print(cid)
    return 0


def _payload_field(payload: dict, name: str, default: str) -> str:
    return str(payload.get(name) or default)


def _stdin_payload() -> dict | None:
    """The JSON object on stdin when it carries non-blank text; None otherwise."""
    payload = _stdin_json()
    if not isinstance(payload, dict):
        return None
    if not _payload_field(payload, "text", "").strip():
        return None
    return payload


def _stdin_json() -> object:
    try:
        raw = sys.stdin.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    # No args + non-TTY stdin → capture path (OpenCode plugin).
    if len(sys.argv) == 1 and not sys.stdin.isatty():
        return _capture_from_stdin()
    parser = _argument_parser()
    args = parser.parse_args()
    command = _COMMANDS.get(args.command)
    if command is None:
        parser.print_help()
        return 0
    return command(args)


def _argument_parser() -> argparse.ArgumentParser:
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
    return p


def _list_command(args: argparse.Namespace) -> int:
    candidates = list_candidates("candidate")
    if not candidates:
        print("(no feedback candidates)")
        return 0
    print(f"Feedback candidates ({len(candidates)}):\n")
    for c in candidates:
        print(f"  [{c['id'][:8]}] ({c['type']}, conf={c['confidence']}) {c['text'][:80]}...")
        print(f"    project: {c['project']}, captured: {c['captured_at']}")
        print()
    return 0


def _list_all_command(args: argparse.Namespace) -> int:
    all_c = list_candidates("candidate") + list_candidates("promoted")
    print(f"All feedback ({len(all_c)}):\n")
    for c in all_c:
        status = "✅" if c["status"] == "promoted" else "⏳"
        print(f"  {status} [{c['id'][:8]}] ({c['type']}) {c['text'][:60]}...")
    return 0


def _promote_command(args: argparse.Namespace) -> int:
    result = promote_candidate(args.id, args.category)
    if not result:
        print(f"Candidate {args.id} not found")
        return 1
    print(f"Promoted to: {result}")
    return 0


def _capture_command(args: argparse.Namespace) -> int:
    text = _capture_text(args)
    if text is None:
        return 1
    cid = capture_from_text(
        text,
        session_id=args.session_id,
        slug=args.slug,
        trigger=args.trigger,
    )
    print(cid if cid else "(no feedback detected)")
    return 0


def _capture_text(args: argparse.Namespace) -> str | None:
    """The text to scan; None after saying on stderr why there is none."""
    text = _source_text(args)
    if text is None:
        return None
    if not text.strip():
        print("feedback_capture: no text provided", file=sys.stderr)
        return None
    return text


def _source_text(args: argparse.Namespace) -> str | None:
    if not args.transcript:
        return args.text or ""
    try:
        return Path(args.transcript).read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        print(f"feedback_capture: cannot read transcript: {e}", file=sys.stderr)
        return None


_COMMANDS = {
    "list": _list_command,
    "list-all": _list_all_command,
    "promote": _promote_command,
    "capture": _capture_command,
}


if __name__ == "__main__":
    raise SystemExit(main())
