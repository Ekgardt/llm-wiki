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
drowns the signal. One tool result is not traffic: a foreground subagent's report,
which is another agent's conclusion in prose, is kept as `**subagent report:**`
(docs/research/2026-09-24-a-subagents-report-is-kept-with-its-session.md).

See knowledge/notes/session-evidence-retention-decision.md.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

SESSION_EVIDENCE_DIR = "knowledge/raw/sessions"
MAX_EVIDENCE_BYTES = 512 * 1024
MAX_TOOL_LINE_CHARS = 200
# The host's subagent tool: `Agent`, named `Task` by older hosts.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
MAX_SUBAGENT_REPORT_CHARS = 8000
SUBAGENT_REPORT_CUT = " _(report cut at the size limit)_"
TRUNCATION_NOTE = "\n\n_(record truncated at the size limit)_\n"
# No dots: a session id needs none, and a name that cannot contain `..` is one
# less thing to reason about when it becomes a path.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_TOOL_INPUT_FIELDS = ("command", "file_path", "path", "pattern", "query", "url")
# Every record is redacted where it is written, one line per header value; the body is
# cut to the bound plus this much first, so a secret at the final cut was whole when
# it was redacted. See `docs/research/2026-09-14-every-session-record-is-redacted.md`.
REDACTION_SLACK_CHARS = 64 * 1024
_LINE_BREAKING = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")


_NAME_LIMIT = 64
_NAME_DIGEST_CHARS = 12


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("-", str(value or "")).strip("-")
    return cleaned[:_NAME_LIMIT] or fallback


def _session_component(session_id: str, document: bytes) -> str:
    """The session's own id when it is a safe name, else a name that cannot collide.

    Replacing characters, cutting at 64 or falling back for a missing id maps
    many sessions to one name, and the write replaces the file: the second
    session's record took the first one's place. Such a name carries a digest
    of the raw id (of the record itself when there is no id); an id that is
    already safe keeps its name (research
    2026-09-11-the-seven-questions-the-audits-left-open.md, memory Q3).
    """
    safe = _safe_component(session_id, "")
    if safe and safe == session_id:
        return safe
    source = session_id.encode("utf-8") if session_id else document
    digest = hashlib.sha256(source).hexdigest()[:_NAME_DIGEST_CHARS]
    stem = (safe or "unknown-session")[: _NAME_LIMIT - _NAME_DIGEST_CHARS - 1]
    return f"{stem}-{digest}"


def evidence_relative_path(day: str, session_id: str, document: bytes = b"") -> str:
    """`knowledge/raw/sessions/<day>/<session>.md`, always inside the vault."""
    return (
        f"{SESSION_EVIDENCE_DIR}/{_safe_component(day, 'undated')}"
        f"/{_session_component(str(session_id or ''), document)}.md"
    )


PART_DIGEST_CHARS = 8


def _free_relative_path(vault: Path, fields: Mapping[str, object], document: bytes) -> str:
    """The session's record path, or a sibling when that name holds another window.

    A second capture of one session on one day used to replace the first, and
    the day's record set, which consolidation reads by name, did not change: the
    earlier tail was lost and the new one never read. A different window is
    written beside it as `<session>@<8 hex of its sha256>.md`; the same content
    keeps the first name, so a replay stays one record. See
    `docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.
    """
    base = evidence_relative_path(_capture_day(fields), str(fields.get("session") or ""), document)
    existing = vault / base
    if not existing.exists() or _same_bytes(existing, document):
        return base
    part = hashlib.sha256(document).hexdigest()[:PART_DIGEST_CHARS]
    return f"{base[: -len('.md')]}@{part}.md"


def _same_bytes(path: Path, document: bytes) -> bool:
    try:
        return path.read_bytes() == document
    except OSError:
        return False


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


def _field_text(value: object) -> str:
    """A target as text; a command given as a list of words is shown joined."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(word, str) for word in value):
        return " ".join(value)
    return ""


def _tool_target(block: Mapping[str, object]) -> str:
    payload = block.get("input")
    if not isinstance(payload, Mapping):
        return ""
    for field in _TOOL_INPUT_FIELDS:
        value = _field_text(payload.get(field))
        if value:
            return value
    return ""


def _tool_line(block: Mapping[str, object]) -> str:
    """One line per tool call; redacted before it is cut.

    Cut first, a secret split at the bound no longer matched its pattern and
    its first characters reached the record (audit 2026-09-26 C-6).
    """
    from secret_redact import redact_secrets

    name = str(block.get("name") or "tool")
    target = redact_secrets(" ".join(_tool_target(block).split()))[:MAX_TOOL_LINE_CHARS]
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


def _result_text(content: object) -> str:
    texts = [_text_of(block) for block in _blocks_of(content) if isinstance(block, Mapping)]
    return "\n\n".join(text for text in texts if text)


def _clipped_report(text: str) -> str:
    if len(text) <= MAX_SUBAGENT_REPORT_CHARS:
        return text
    return text[:MAX_SUBAGENT_REPORT_CHARS] + SUBAGENT_REPORT_CUT


def _subagent_report(block: Mapping[str, object], subagent_calls: frozenset[str]) -> str | None:
    """The report a foreground subagent returned; nothing for any other tool's output."""
    if block.get("tool_use_id") not in subagent_calls:
        return None
    text = _result_text(block.get("content"))
    if not text:
        return None
    return f"**subagent report:** {_clipped_report(text)}"


_RENDERERS = {
    "tool_use": lambda block, role, calls: _tool_line(block),
    "text": lambda block, role, calls: _rendered_text(block, role),
    "tool_result": lambda block, role, calls: _subagent_report(block, calls),
}


def _rendered_kind(
    block: Mapping[str, object], role: str, subagent_calls: frozenset[str]
) -> str | None:
    renderer = _RENDERERS.get(str(block.get("type") or ""))
    if renderer is None:
        return None
    return renderer(block, role, subagent_calls)


def _rendered_block(block: object, role: str, subagent_calls: frozenset[str]) -> str | None:
    """One line for a tool call, the text itself for a turn, a subagent's report."""
    if not isinstance(block, Mapping):
        return None
    return _rendered_kind(block, role, subagent_calls)


def _entry_role(entry: Mapping[str, object]) -> str:
    role = entry.get("type")
    if role in {"user", "assistant"}:
        return str(role)
    return ""


def _is_subagent_call(block: object) -> bool:
    if not isinstance(block, Mapping):
        return False
    return block.get("type") == "tool_use" and block.get("name") in SUBAGENT_TOOLS


def _subagent_ids_in(entry: Mapping[str, object] | None) -> list[str]:
    if entry is None or _entry_role(entry) != "assistant":
        return []
    blocks = _content_blocks(entry.get("message"))
    return [str(block.get("id")) for block in blocks if _is_subagent_call(block)]


def _subagent_call_ids(entries: Sequence[Mapping[str, object] | None]) -> frozenset[str]:
    """The ids of every subagent call in the transcript."""
    return frozenset(call for entry in entries for call in _subagent_ids_in(entry))


def _launched_in_background(entry: Mapping[str, object]) -> bool:
    """A background launch's result is a receipt; its report arrives as a notification."""
    result = entry.get("toolUseResult")
    return isinstance(result, Mapping) and result.get("isAsync") is True


def _entry_calls(entry: Mapping[str, object], subagent_calls: frozenset[str]) -> frozenset[str]:
    if _launched_in_background(entry):
        return frozenset()
    return subagent_calls


def _rendered_entry(
    entry: Mapping[str, object], subagent_calls: frozenset[str] = frozenset()
) -> list[str]:
    role = _entry_role(entry)
    if not role:
        return []
    calls = _entry_calls(entry, subagent_calls)
    blocks = _content_blocks(entry.get("message"))
    lines = [_rendered_block(block, role, calls) for block in blocks]
    return [line for line in lines if line]


def _decoded_entry(line: str) -> Mapping[str, object] | None:
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, Mapping):
        return None
    return value


# A Codex rollout line is `{"type": "response_item", "payload": {...}}` with the
# item tagged by its own `type`; it is read as the entry it stands for. See
# `docs/research/2026-09-25-a-codex-session-is-read-as-a-conversation.md`.
CODEX_ITEM_TYPE = "response_item"
_CODEX_ROLES = frozenset({"user", "assistant"})
_CODEX_TEXT_PARTS = frozenset({"input_text", "output_text"})
_CODEX_ARGUMENT_FIELDS = ("arguments", "action", "input")


def _codex_text_block(part: object) -> dict[str, object] | None:
    if not isinstance(part, Mapping) or part.get("type") not in _CODEX_TEXT_PARTS:
        return None
    return {"type": "text", "text": part.get("text")}


def _codex_message(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    role = payload.get("role")
    if role not in _CODEX_ROLES:
        return None
    return {"type": role, "message": {"content": _codex_text_blocks(payload.get("content"))}}


def _codex_text_blocks(parts: object) -> list[dict[str, object]]:
    if not isinstance(parts, list):
        return []
    blocks = [_codex_text_block(part) for part in parts]
    return [block for block in blocks if block]


def _json_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _codex_arguments(payload: Mapping[str, object]) -> Mapping[str, object]:
    """The call's arguments: a JSON string, a shell action, or free text (no target)."""
    for field in _CODEX_ARGUMENT_FIELDS:
        parsed = _json_mapping(payload.get(field))
        if parsed is not None:
            return parsed
    return {}


def _codex_tool_call(payload: Mapping[str, object]) -> Mapping[str, object]:
    name = payload.get("name") or payload.get("type")
    call = {"type": "tool_use", "name": name, "input": _codex_arguments(payload)}
    return {"type": "assistant", "message": {"content": [call]}}


_CODEX_ITEMS = {
    "message": _codex_message,
    "function_call": _codex_tool_call,
    "custom_tool_call": _codex_tool_call,
    "local_shell_call": _codex_tool_call,
}


def _codex_entry(payload: object) -> Mapping[str, object] | None:
    if not isinstance(payload, Mapping):
        return None
    reader = _CODEX_ITEMS.get(str(payload.get("type") or ""))
    return None if reader is None else reader(payload)


def _conversation_entry(entry: Mapping[str, object] | None) -> Mapping[str, object] | None:
    """A Claude line as it is; a Codex item as the turn or tool call it records."""
    if entry is None or entry.get("type") != CODEX_ITEM_TYPE:
        return entry
    return _codex_entry(entry.get("payload"))


CAPTURE_GAP_TYPE = "capture_gap"


def capture_gap_line(dropped_bytes: int) -> str:
    """The line that says, in the transcript's own format, what was not captured.

    A plain sentence between two halves of a JSONL transcript was dropped by the
    renderer below, so the stored record of a cut session read as if it were whole.
    See `docs/research/2026-09-17-the-evidence-fits-its-record-and-says-what-it-dropped.md`.
    """
    note = (
        f"_({dropped_bytes} bytes of this transcript were not captured; "
        "the durable record keeps the beginning and the end.)_"
    )
    entry = {"type": CAPTURE_GAP_TYPE, "dropped_bytes": dropped_bytes, "note": note}
    return json.dumps(entry, ensure_ascii=False)


def _gap_note(entry: Mapping[str, object] | None) -> str | None:
    if entry is None or entry.get("type") != CAPTURE_GAP_TYPE:
        return None
    return str(entry.get("note") or "")


def _is_conversation(entry: Mapping[str, object] | None) -> bool:
    return entry is not None and _gap_note(entry) is None


def _verbatim_line(line: str) -> str:
    note = _gap_note(_decoded_entry(line))
    return line if note is None else note


def _conversation_lines(
    entry: Mapping[str, object] | None, subagent_calls: frozenset[str]
) -> list[str]:
    if entry is None:
        return []
    note = _gap_note(entry)
    if note is not None:
        return [note]
    return _rendered_entry(entry, subagent_calls)


def render_transcript(text: str) -> str:
    """Render a JSONL transcript as conversation; keep anything else verbatim."""
    lines = text.splitlines()
    entries = [_conversation_entry(_decoded_entry(line)) for line in lines]
    if not any(map(_is_conversation, entries)):
        return "\n".join(map(_verbatim_line, lines)).strip()
    subagent_calls = _subagent_call_ids(entries)
    rendered: list[str] = []
    for entry in entries:
        rendered.extend(_conversation_lines(entry, subagent_calls))
    return "\n\n".join(rendered)


def _frontmatter(fields: Mapping[str, object]) -> str:
    lines = ["---", "type: raw-source", "status: active", "confidence: high"]
    # Not `user`: these are the user's words, but raw and unreviewed, so a page
    # compiled from them must still outrank them in retrieval.
    lines.append("source_authority: session")
    for key in ("session", "project", "host", "event", "captured_at", "source_event_id"):
        value = fields.get(key)
        if value:
            lines.append(f"{key}: {_header_value(value)}")
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
    return _document_from_body(fields, render_transcript(transcript).strip())


def _header_value(value: object) -> str:
    """One redacted line: a line break in a value would write a header of its own."""
    from secret_redact import redact_secrets

    return redact_secrets(_LINE_BREAKING.sub(" ", str(value)).strip())


def _redacted_body(body: str) -> str:
    from secret_redact import redact_secrets

    return redact_secrets(body[: MAX_EVIDENCE_BYTES + REDACTION_SLACK_CHARS])


def _document_from_body(fields: Mapping[str, object], body: str) -> str:
    session = _header_value(fields.get("session") or "unknown session")
    title = f"# Session {session}"
    return _bounded(f"{_frontmatter(fields)}\n{title}\n\n{_redacted_body(body)}\n")


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
    tier decision that follows must not depend on this write. A lost record is
    written to the capture-failure trail, so it is never silent (audit H5,
    `docs/research/2026-09-10-a-lost-session-record-is-written-down.md`).
    """
    from markdown_transaction import stable_operation_id

    body = render_transcript(transcript).strip()
    if not body:
        return None
    document = _document_from_body(fields, body)
    encoded = document.encode("utf-8")
    relative = _free_relative_path(Path(vault), fields, encoded)
    path = Path(vault) / relative
    try:
        _write_record(
            stable_operation_id("session-evidence", relative, encoded),
            {path: encoded},
            coordinator,
            owner,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        _record_lost_record(exc, str(fields.get("session") or ""))
        return None
    return path


def _record_lost_record(error: BaseException, session_id: str) -> None:
    from capture_diagnostics import record_capture_failure

    record_capture_failure(
        "session_evidence",
        f"{type(error).__name__}: {error}",
        error=error,
        session_id=session_id or None,
    )


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
