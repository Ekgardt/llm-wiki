"""Keep the session itself, not only what a classifier thought of it.

Measured on this vault's own sessions, the classifier answered "nothing worth
keeping" 39 times out of 40. The fix is not a better classifier: a 2026 ablation
that varied only the stored representation found verbatim conversation beating
extracted artifacts by 15.9 points on LoCoMo and 22.0 on LongMemEval-S, because
extraction commits to relevance before the question exists. So every captured
session now leaves a redacted, searchable copy of itself, and the classifier
decides only whether the session also deserves a compiled page.

The record keeps the conversation and drops the tool traffic to one line per
call — the studied setting is dialogue, and tool output is exactly the noise that
drowns the signal.

See knowledge/notes/session-evidence-retention-decision.md.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

SESSION_EVIDENCE_DIR = "knowledge/raw/sessions"
MAX_EVIDENCE_BYTES = 512 * 1024
MAX_TOOL_LINE_CHARS = 200
TRUNCATION_NOTE = "\n\n_(record truncated at the size limit)_\n"
# No dots: a session id needs none, and a name that cannot contain `..` is one
# less thing to reason about when it becomes a path.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_TOOL_INPUT_FIELDS = ("command", "file_path", "path", "pattern", "query", "url")


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("-", str(value or "")).strip("-")
    return cleaned[:64] or fallback


def evidence_relative_path(day: str, session_id: str) -> str:
    """`knowledge/raw/sessions/<day>/<session>.md`, always inside the vault."""
    return (
        f"{SESSION_EVIDENCE_DIR}/{_safe_component(day, 'undated')}"
        f"/{_safe_component(session_id, 'unknown-session')}.md"
    )


def _blocks_of(content: object) -> list[object]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _content_blocks(message: object) -> list[object]:
    if not isinstance(message, Mapping):
        return []
    return _blocks_of(message.get("content"))


def _tool_target(block: Mapping[str, object]) -> str:
    payload = block.get("input")
    if not isinstance(payload, Mapping):
        return ""
    for field in _TOOL_INPUT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_line(block: Mapping[str, object]) -> str:
    name = str(block.get("name") or "tool")
    target = " ".join(_tool_target(block).split())[:MAX_TOOL_LINE_CHARS]
    if not target:
        return f"- tool `{name}`"
    return f"- tool `{name}`: {target}"


def _text_of(block: Mapping[str, object]) -> str:
    value = block.get("text")
    if not isinstance(value, str):
        return ""
    return value.strip()


def _rendered_text(block: Mapping[str, object], role: str) -> str | None:
    text = _text_of(block)
    if not text:
        return None
    return f"**{role}:** {text}"


def _rendered_kind(block: Mapping[str, object], role: str) -> str | None:
    kind = str(block.get("type") or "")
    if kind == "tool_use":
        return _tool_line(block)
    if kind == "text":
        return _rendered_text(block, role)
    return None


def _rendered_block(block: object, role: str) -> str | None:
    """One line for a tool call, the text itself for a turn, nothing for output."""
    if not isinstance(block, Mapping):
        return None
    return _rendered_kind(block, role)


def _entry_role(entry: Mapping[str, object]) -> str:
    role = entry.get("type")
    if role in {"user", "assistant"}:
        return str(role)
    return ""


def _rendered_entry(entry: Mapping[str, object]) -> list[str]:
    role = _entry_role(entry)
    if not role:
        return []
    blocks = _content_blocks(entry.get("message"))
    lines = [_rendered_block(block, role) for block in blocks]
    return [line for line in lines if line]


def _decoded_entry(line: str) -> Mapping[str, object] | None:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, Mapping):
        return None
    return value


def render_transcript(text: str) -> str:
    """Render a JSONL transcript as conversation; keep anything else verbatim."""
    rendered: list[str] = []
    decoded_any = False
    for line in text.splitlines():
        entry = _decoded_entry(line)
        if entry is None:
            continue
        decoded_any = True
        rendered.extend(_rendered_entry(entry))
    if not decoded_any:
        return text.strip()
    return "\n\n".join(rendered)


def _frontmatter(fields: Mapping[str, object]) -> str:
    lines = ["---", "type: raw-source", "status: active", "confidence: high"]
    # Not `user`: these are the user's words, but raw and unreviewed, so a page
    # compiled from them must still outrank them in retrieval.
    lines.append("source_authority: session")
    for key in ("session", "project", "host", "event", "captured_at", "source_event_id"):
        value = fields.get(key)
        if value:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _bounded(body: str) -> str:
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_EVIDENCE_BYTES:
        return body
    kept = encoded[:MAX_EVIDENCE_BYTES].decode("utf-8", errors="ignore")
    return kept + TRUNCATION_NOTE


def render_session_document(fields: Mapping[str, object], transcript: str) -> str:
    """The whole page: frontmatter, a title that names the session, the turns."""
    session = str(fields.get("session") or "unknown session")
    title = f"# Session {session}"
    body = render_transcript(transcript).strip()
    return _bounded(f"{_frontmatter(fields)}\n{title}\n\n{body}\n")


def _part_text(part: object) -> str | None:
    if not isinstance(part, Mapping):
        return None
    value = part.get("text")
    if not isinstance(value, str):
        return None
    return value


def _item_texts(item: object) -> list[str]:
    if not isinstance(item, Mapping):
        return []
    texts = [_part_text(part) for part in item.get("parts", [])]
    return [text for text in texts if text is not None]


def evidence_text(evidence: Sequence[object]) -> str:
    """The transcript text carried by a capture intent's evidence list."""
    parts: list[str] = []
    for item in evidence:
        parts.extend(_item_texts(item))
    return "\n".join(parts)


def _capture_day(fields: Mapping[str, object]) -> str:
    captured = str(fields.get("captured_at") or "")
    return captured[:10] or "undated"


def intent_fields(record: Mapping[str, object], captured_at: str) -> dict[str, object]:
    return {
        "session": record.get("session") or "unknown-session",
        "project": record.get("project_slug"),
        "host": record.get("host"),
        "event": record.get("event"),
        "captured_at": captured_at,
        "source_event_id": record.get("source_event_id"),
    }


def write_session_evidence(
    vault: Path,
    fields: Mapping[str, object],
    transcript: str,
    *,
    coordinator: object | None = None,
    owner: object | None = None,
) -> Path | None:
    """Write the session record; returns the path, or None when there is nothing.

    Never raises: losing the record is bad, but breaking capture is worse, and the
    tier decision that follows must not depend on this write.
    """
    from markdown_transaction import stable_operation_id

    document = render_session_document(fields, transcript)
    if not render_transcript(transcript).strip():
        return None
    relative = evidence_relative_path(_capture_day(fields), str(fields.get("session") or ""))
    path = Path(vault) / relative
    encoded = document.encode("utf-8")
    try:
        _write_record(
            stable_operation_id("session-evidence", relative, encoded),
            {path: encoded},
            coordinator,
            owner,
        )
    except Exception:  # noqa: BLE001
        return None
    return path


def _write_record(
    operation_id: str,
    changes: Mapping[Path, bytes],
    coordinator: object | None,
    owner: object | None,
) -> None:
    """Write through the caller's gate when it holds one, else claim our own.

    The capture worker already owns a writer lease, and claiming a second one
    raises `owner_identity_conflict` — swallowed above, which is why no queued
    session reached disk between the 2026-08-24 backfill and 2026-08-26.
    """
    from markdown_transaction import mutate_knowledge, mutate_owned_knowledge

    if coordinator is None or owner is None:
        mutate_knowledge(operation_id, changes)
        return
    mutate_owned_knowledge(coordinator, owner, operation_id, changes)
