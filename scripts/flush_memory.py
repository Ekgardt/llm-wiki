"""Flush one session event into knowledge/daily/YYYY-MM-DD.md.

Run as a detached background process by PreCompact / SessionEnd hooks.

Responsibilities:
1. Read the transcript at `--transcript` (JSONL Claude Code transcript).
2. Ask the unified llm_client (auto-detected backend: OpenCode / Codex /
   Claude CLI / OpenAI / Ollama) to classify + summarize the session
   into one of three tiers (Phase 0.5 upgrade):
     - FLUSH_MAJOR: decisions/lessons worth compiling
     - FLUSH_MINOR: commands/gotchas/open-questions — save but no auto-compile
     - FLUSH_OK:    pure status/progress chatter — skip entirely
3. For MAJOR/MINOR: append the structured summary to today's daily log
   with an `[HH:MM:SS] event | session_id` header block and a `Tier:`
   metadata line. For OK: do not append anything.
4. Dedupe: skip if the same project/session/event occurrence was flushed in
   the last 60s.
5. If local time >= MEMORY_COMPILE_AFTER_HOUR (default 18) AND tier is
   MAJOR AND today's daily log changed since last compile: spawn
   compile via `maybe_compile` (PID-locked). (MINOR no longer triggers
   compile — this prevents the compile pipeline from churning on
   sessions that contain only minor gotchas.)

The 3-tier scale replaces the previous binary FLUSH_OK/no-FLUSH_OK.
Empirically the old threshold was too aggressive (12 consecutive
empty flushes recorded in state.json as of 2026-04-23): the LLM
returned FLUSH_OK for any session that lacked a clean "decisions"
section, even when useful gotchas or commands were present.

State lives in $LLM_WIKI_STATE_ROOT/run/state.json (default:
$LLM_WIKI_ROOT/run/state.json — inside the vault, gitignored) so git
doesn't track runtime churn.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from maybe_compile import spawn_compile_if_idle  # noqa: E402
from memory_state import (  # noqa: E402
    ROOT,
    file_hash,
    load_state,
    trusted_compiled_daily_hashes,
    update_state,
)
from secret_redact import redact_secrets  # noqa: E402

DAILY_DIR = ROOT / "knowledge" / "daily"
DEDUPE_WINDOW_SECONDS = 60
MAX_TRANSCRIPT_CHARS = 60_000
MAX_TRANSCRIPT_SOURCE_BYTES = 8 * 1024 * 1024
TRANSCRIPT_SOURCE_CHUNK_BYTES = 64 * 1024
STAGED_TRANSCRIPT_PREFIX = "llm-wiki-precompact-"
STAGED_TRANSCRIPT_SUFFIX = ".txt"
MAX_PROVENANCE_CHARS = 500
BODY_XML_TAG_RE = re.compile(r"</?(?:analysis|summary)>", re.IGNORECASE)
BODY_HEADING_PREFIX_RE = re.compile(r"^##\s+\[")
BODY_COMPACT_PREFIX_RE = re.compile(r"^\s*-\s*`\[")
DAILY_RECORD_COMPLETION_MARKER = "<!-- llm-wiki-record-complete -->"
FLUSH_SECTION_HEADINGS = (
    "Decisions made",
    "Lessons / patterns",
    "Commands / snippets",
    "Gotchas / debugging",
    "Open questions",
)
FLUSH_HEADING_LINES = {f"**{heading}**": heading for heading in FLUSH_SECTION_HEADINGS}
FLUSH_MAJOR_SECTION_HEADINGS = frozenset(FLUSH_SECTION_HEADINGS[:2])
FLUSH_MINOR_SECTION_HEADINGS = frozenset(FLUSH_SECTION_HEADINGS[2:])
FLUSH_BULLET_RE = re.compile(r"^[ \t]*-[ \t]+(?=\S).*\S[ \t]*$")
TRANSCRIPT_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
TRANSCRIPT_ROLES = frozenset({"user", "assistant"})
CAPTURE_ID_RE = re.compile(r"[0-9a-f]{64}")

@dataclass(frozen=True)
class TranscriptReadResult:
    text: str
    successful: bool


@dataclass(frozen=True)
class FlushProcessStatus:
    code: int
    durable: bool
    project_slug: str
    project_root: str
    project_identity_confirmed: bool = False


def _validate_flush_body(tier_token: str, body: str) -> None:
    allowed = (
        frozenset(FLUSH_SECTION_HEADINGS)
        if tier_token == "FLUSH_MAJOR"
        else FLUSH_MINOR_SECTION_HEADINGS
    )
    current_heading: str | None = None
    bullet_count = 0
    section_count = 0
    seen_headings: set[str] = set()
    for line in body.splitlines():
        if not line.strip():
            continue
        heading = FLUSH_HEADING_LINES.get(line)
        if heading is not None:
            if current_heading is not None and bullet_count == 0:
                raise ValueError("flush classification section has no non-empty bullet")
            if heading not in allowed:
                raise ValueError("flush classification section is not allowed for tier")
            current_heading = heading
            bullet_count = 0
            section_count += 1
            seen_headings.add(heading)
            continue
        if current_heading is None or FLUSH_BULLET_RE.fullmatch(line) is None:
            raise ValueError("flush classification body violates section grammar")
        bullet_count += 1
    if section_count == 0 or bullet_count == 0:
        raise ValueError("flush classification section has no non-empty bullet")
    if tier_token == "FLUSH_MAJOR" and seen_headings.isdisjoint(
        FLUSH_MAJOR_SECTION_HEADINGS
    ):
        raise ValueError("flush classification MAJOR response has no major section")


def _classify_response(raw: str) -> tuple[str, str]:
    """Split the LLM response into (tier, body).

    FLUSH_OK must be the exact response. MAJOR/MINOR responses must start
    with the exact tier token and contain at least one allowed section.

    Returns:
        ("major" | "minor" | "ok", remaining_text)

    The remaining_text is the structured summary for MAJOR/MINOR tiers
    and is empty for OK.
    """
    stripped = str(raw or "").strip()
    if stripped == "FLUSH_OK":
        return "ok", ""
    if stripped.startswith("FLUSH_OK"):
        raise ValueError("flush classification response is not the exact FLUSH_OK token")
    lines = stripped.splitlines()
    if not lines or lines[0] not in {"FLUSH_MAJOR", "FLUSH_MINOR"}:
        raise ValueError("invalid flush classification token")
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError("flush classification response has no distilled body")
    _validate_flush_body(lines[0], body)
    return lines[0].removeprefix("FLUSH_").lower(), body


def _occurred_datetime(value: str | None, fallback: datetime | None = None) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(
                f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
            )
        except (TypeError, ValueError):
            pass
    return fallback or datetime.now()


def _resolve_project_identity(
    project_slug: str | None,
    project_root: str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, Path] | None:
    explicit = str(project_slug or "").strip()
    raw_root = str(project_root or "").strip()
    try:
        from session_start_project_state import (
            _slug_identity_key,
            confirm_project_identity,
            resolve_project_root,
        )

        resolution = resolve_project_root(
            {"project_root": raw_root} if raw_root else {},
            env=os.environ if env is None else env,
        )
        resolved_root = resolution.root
        if resolved_root is None or not resolved_root.is_dir():
            return None

        confirmed = confirm_project_identity(
            resolved_root,
            ROOT / "knowledge" / "projects",
        )
        if confirmed is None:
            return None
        slug = confirmed[0]
        return (
            (slug, resolved_root)
            if not explicit or _slug_identity_key(explicit) == _slug_identity_key(slug)
            else None
        )
    except Exception:  # noqa: BLE001 - persistence must fail closed
        return None


def _resolve_project_slug(
    project_slug: str | None,
    project_root: str | None,
) -> str | None:
    identity = _resolve_project_identity(project_slug, project_root)
    return identity[0] if identity is not None else None


def _sanitize_provenance(value: object, default: str = "unknown") -> str:
    redacted = redact_secrets(str(value or default))
    one_line = " ".join(redacted.split()).replace("`", "'")
    return one_line[:MAX_PROVENANCE_CHARS] or default


def _neutralize_daily_record_headers(body: str) -> str:
    from daily_log_append import neutralize_capture_marker_prefix

    lines: list[str] = []
    for raw_line in body.splitlines():
        line = neutralize_capture_marker_prefix(raw_line)
        normalized = BODY_XML_TAG_RE.sub("", line)
        marker = "#" if BODY_HEADING_PREFIX_RE.match(normalized) else ""
        if not marker and BODY_COMPACT_PREFIX_RE.match(normalized):
            marker = "-"
        if not marker and normalized == DAILY_RECORD_COMPLETION_MARKER:
            marker = "<"
        position = line.find(marker) if marker else -1
        lines.append(
            f"{line[:position]}\\{line[position:]}" if position >= 0 else line
        )
    return "\n".join(lines)


def render_flush_block(
    tier: str,
    body: str,
    *,
    event: str,
    session_id: str,
    trigger: str,
    project_slug: str,
    project_root: str,
    occurred_at: str,
    deferred: bool = False,
    idempotency_marker: str = "",
) -> tuple[str, str]:
    """Render and redact one classified daily block from source metadata."""
    occurred = _occurred_datetime(_sanitize_provenance(occurred_at, ""))
    event_name = _sanitize_provenance(event, "session-end")
    if deferred:
        event_name = _sanitize_provenance(f"deferred-{event_name}")
    event_name = event_name.replace("|", "/")
    source_session = _sanitize_provenance(session_id).replace("|", "/")
    source_trigger = _sanitize_provenance(trigger)
    raw_root = str(project_root or "").strip()
    try:
        source_root = (
            str(Path(raw_root).resolve())
            if Path(raw_root).is_absolute()
            else _sanitize_provenance(raw_root)
        )
    except (OSError, RuntimeError, ValueError):
        source_root = _sanitize_provenance(raw_root)
    source_slug = _sanitize_provenance(project_slug)
    source_tier = _sanitize_provenance(tier)
    header = (
        f"\n## [{occurred.strftime('%H:%M:%S')}] {event_name} | {source_session}\n"
    )
    meta = (
        f"- Trigger: `{source_trigger}`\n"
        f"- Project slug: `{source_slug}`\n"
        f"- Project root JSON: {json.dumps(source_root, ensure_ascii=False)}\n"
        f"- Tier: `{source_tier}`\n"
        f"- Source session: `{source_session}`\n"
    )
    safe_body = _neutralize_daily_record_headers(redact_secrets(body.strip()))
    safe_marker = _sanitize_provenance(idempotency_marker, "")
    marker = f"{safe_marker}\n" if safe_marker else ""
    block = (
        header
        + meta
        + "\n"
        + safe_body
        + "\n"
        + marker
        + DAILY_RECORD_COMPLETION_MARKER
        + "\n"
    )
    return occurred.strftime("%Y-%m-%d"), block


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--capture-id", action="store_true")
    p.add_argument("--event", required=True, choices=["session-end", "pre-compact"])
    p.add_argument("--session-id", default="unknown")
    p.add_argument("--transcript", default="")
    p.add_argument("--transcript-stdin", action="store_true")
    p.add_argument("--delete-transcript", action="store_true")
    p.add_argument("--trigger", default="")
    p.add_argument("--project-slug", default="")
    p.add_argument("--project-root", default="")
    p.add_argument("--occurred-at", default="")
    return p.parse_args()


def _transcript_path_allowed(path: Path) -> bool:
    """Only allow transcript paths from known agent session directories.

    `transcript_path` arrives from hook JSON (untrusted input). A broad
    allowlist (e.g. all of ``$HOME``) would let a crafted payload point
    at ``~/.ssh/id_rsa`` and ship its contents to the LLM. Instead we
    restrict to the specific directories where Claude Code, Codex, and
    OpenCode store session transcripts, plus the system temp dir and the
    vault-local cache for testing.
    """
    import tempfile

    try:
        p = path.resolve()
    except OSError:
        return False

    allowed_prefixes: list[Path] = []
    # Claude Code transcripts
    home = Path.home()
    allowed_prefixes.append(home / ".claude")
    # Codex transcripts
    allowed_prefixes.append(home / ".codex")
    # OpenCode transcripts
    allowed_prefixes.append(home / ".config" / "opencode")
    # Vault-local temp (for testing)
    if ROOT.exists():
        allowed_prefixes.append(ROOT / "cache")
    # System temp (for Claude Code compacted transcripts)
    allowed_prefixes.append(Path(tempfile.gettempdir()))

    # Must also have a known transcript extension.
    if p.suffix not in (".jsonl", ".json", ".txt", ".log"):
        return False

    # Must be under one of the allowed directories.
    for prefix in allowed_prefixes:
        try:
            prefix_resolved = prefix.resolve()
        except OSError:
            continue
        try:
            p.relative_to(prefix_resolved)
            return True
        except ValueError:
            continue
    return False


def read_stream_tail(stream, max_chars: int, chunk_size: int = 8_192) -> str:
    """Read a bounded text tail without first allocating the whole input."""
    if max_chars <= 0 or chunk_size <= 0:
        return ""
    tail = ""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return tail
        tail = (tail + chunk)[-max_chars:]


def read_stdin_bounded(stream, max_bytes: int = MAX_TRANSCRIPT_SOURCE_BYTES) -> str | None:
    """Read UTF-8 stdin up to a source-byte limit without partial acceptance."""
    if max_bytes < 0:
        return None
    binary = getattr(stream, "buffer", None)
    if binary is not None:
        raw = binary.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None

    chunks: list[str] = []
    total = 0
    while True:
        chunk = stream.read(TRANSCRIPT_SOURCE_CHUNK_BYTES)
        if not chunk:
            return "".join(chunks)
        try:
            total += len(chunk.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            return None
        if total > max_bytes:
            return None
        chunks.append(chunk)


def _capture_id_cli(args: argparse.Namespace) -> int:
    if (
        not args.transcript_stdin
        or args.transcript
        or args.delete_transcript
        or not args.session_id
        or args.session_id == "unknown"
        or not args.trigger
        or not args.project_slug
        or not args.project_root
    ):
        return 2
    provenance = (
        args.session_id,
        args.trigger,
        args.project_slug,
        args.project_root,
    )
    if any(
        value != value.strip()
        or len(value) > MAX_PROVENANCE_CHARS
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
        for value in provenance
    ):
        return 2
    transcript = read_stdin_bounded(sys.stdin)
    if transcript is None or not _normalize_transcript_excerpt(transcript):
        return 2
    print(
        build_capture_id(
            transcript,
            args.event,
            session_id=args.session_id,
            trigger=args.trigger,
            project_slug=args.project_slug,
            project_root=args.project_root,
        )
    )
    return 0


def _normalize_transcript_excerpt(transcript_excerpt: str) -> str:
    normalized = str(transcript_excerpt).replace("\r\n", "\n").replace("\r", "\n")
    return normalized.strip()[-MAX_TRANSCRIPT_CHARS:]


def build_capture_id(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str,
    trigger: str,
    project_slug: str,
    project_root: str,
) -> str:
    """Return the stable identity of one bounded conversational capture."""
    normalized = _normalize_transcript_excerpt(transcript_excerpt)
    transcript_sha256 = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    canonical = json.dumps(
        {
            "event": str(event),
            "session_id": str(session_id),
            "trigger": str(trigger),
            "project_slug": str(project_slug),
            "project_root": str(project_root),
            "transcript_sha256": transcript_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _conversation_text_parts(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for part in value:
        if isinstance(part, str):
            text = part.strip()
        elif isinstance(part, Mapping):
            part_type = part.get("type")
            raw_text = part.get("text")
            text = (
                raw_text.strip()
                if isinstance(part_type, str)
                and part_type in TRANSCRIPT_TEXT_PART_TYPES
                and isinstance(raw_text, str)
                else ""
            )
        else:
            text = ""
        if text:
            texts.append(text)
    return texts


def _conversation_record_text(record: Mapping) -> str:
    message = record.get("message")
    payload = record.get("payload")
    if isinstance(message, Mapping):
        source = message
    elif isinstance(payload, Mapping) and payload.get("type") == "message":
        source = payload
    else:
        source = record
    raw_role = source.get("role") or record.get("role")
    role = raw_role if isinstance(raw_role, str) else ""
    record_type = record.get("type")
    if role not in TRANSCRIPT_ROLES and isinstance(record_type, str):
        role = record_type
    if role not in TRANSCRIPT_ROLES:
        return ""
    content = source.get("content")
    if content is None:
        content = source.get("parts")
    if content is None:
        content = source.get("text")
    texts = _conversation_text_parts(content)
    text = "\n".join(texts)
    return f"{role}: {text}" if text else ""


def _conversation_tail(records: list[Mapping], max_chars: int) -> str:
    messages: list[str] = []
    remaining = max_chars
    for record in reversed(records):
        message = _conversation_record_text(record)
        if not message:
            continue
        separator_chars = 2 if messages else 0
        available = remaining - separator_chars
        if available <= 0:
            break
        if len(message) > available:
            prefix, separator, content = message.partition(": ")
            role_prefix = f"{prefix}{separator}" if separator else ""
            message = (
                role_prefix + content[-(available - len(role_prefix)):]
                if role_prefix and available > len(role_prefix)
                else message[-available:]
            )
        messages.append(message)
        remaining -= len(message) + separator_chars
        if remaining == 0:
            break
    return "\n\n".join(reversed(messages))


def _extract_jsonl_conversation(raw: str, max_chars: int) -> str:
    records: list[Mapping] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(record, Mapping):
            continue
        records.append(record)
    return _conversation_tail(records, max_chars)


def _read_jsonl_conversation_tail(
    stream,
    max_chars: int,
    chunk_size: int = 8_192,
    max_source_bytes: int = MAX_TRANSCRIPT_SOURCE_BYTES,
) -> tuple[str, bool]:
    """Read complete JSONL records backward with bounded working memory."""
    max_chars = min(max_chars, MAX_TRANSCRIPT_CHARS)
    if max_chars <= 0 or chunk_size <= 0 or max_source_bytes <= 0:
        return "", False
    position = stream.seek(0, os.SEEK_END)
    prefix = b""
    discarding_oversized_line = False
    max_scan_bytes = max_source_bytes
    scanned_bytes = 0
    messages: list[str] = []
    remaining = max_chars
    parsed_record = False
    stop = False

    def consume(raw_line: bytes) -> None:
        nonlocal parsed_record, remaining, stop
        line = raw_line.strip()
        if not line or len(line) > max_scan_bytes:
            return
        try:
            record = json.loads(line)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            return
        if not isinstance(record, Mapping):
            return
        parsed_record = True
        message = _conversation_record_text(record)
        if not message:
            return
        separator_chars = 2 if messages else 0
        available = remaining - separator_chars
        if available <= 0:
            stop = True
            return
        if len(message) > available:
            prefix, separator, content = message.partition(": ")
            role_prefix = f"{prefix}{separator}" if separator else ""
            message = (
                role_prefix + content[-(available - len(role_prefix)):]
                if role_prefix and available > len(role_prefix)
                else message[-available:]
            )
            stop = True
        messages.append(message)
        remaining -= len(message) + separator_chars
        stop = remaining == 0

    while position > 0 and scanned_bytes < max_scan_bytes and not stop:
        read_size = min(chunk_size, position, max_scan_bytes - scanned_bytes)
        position -= read_size
        stream.seek(position)
        chunk = stream.read(read_size)
        scanned_bytes += len(chunk)
        if discarding_oversized_line:
            boundary = chunk.rfind(b"\n")
            if boundary < 0:
                continue
            chunk = chunk[: boundary + 1]
            discarding_oversized_line = False
        parts = (chunk + prefix).split(b"\n")
        prefix = parts[0]
        for raw_line in reversed(parts[1:]):
            consume(raw_line)
            if stop:
                break
        if len(prefix) > max_scan_bytes:
            prefix = b""
            discarding_oversized_line = position > 0

    if position == 0 and not stop and not discarding_oversized_line:
        consume(prefix)
    scan_complete = position == 0 and not discarding_oversized_line
    successful = bool(messages) or (scan_complete and parsed_record)
    return "\n\n".join(reversed(messages)), successful


def _extract_json_document(raw: bytes, max_chars: int) -> tuple[str, bool]:
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return "", False
    if isinstance(document, Mapping):
        records = [document]
    elif isinstance(document, list):
        records = [record for record in document if isinstance(record, Mapping)]
    else:
        return "", False
    return _conversation_tail(records, max_chars), True


def _staged_transcript_allowed(path: Path) -> bool:
    """Allow deletion only for regular files in our exact temp namespace."""
    if (
        not path.name.startswith(STAGED_TRANSCRIPT_PREFIX)
        or not path.name.endswith(STAGED_TRANSCRIPT_SUFFIX)
    ):
        return False
    try:
        if path.parent.resolve() != Path(tempfile.gettempdir()).resolve():
            return False
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and not (reparse_flag and file_attributes & reparse_flag)
    )


def _read_transcript_tail_result(
    path: Path,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
    *,
    staged: bool = False,
) -> TranscriptReadResult:
    max_chars = min(max_chars, MAX_TRANSCRIPT_CHARS)
    if max_chars <= 0:
        return TranscriptReadResult("", False)
    try:
        allowed = (
            _staged_transcript_allowed(path)
            if staged
            else _transcript_path_allowed(path)
        )
        if not allowed:
            return TranscriptReadResult("", False)
        if path.suffix == ".jsonl":
            with path.open("rb") as stream:
                text, parsed = _read_jsonl_conversation_tail(stream, max_chars)
            return TranscriptReadResult(text, parsed)
        if path.suffix == ".json":
            with path.open("rb") as stream:
                raw = stream.read(MAX_TRANSCRIPT_SOURCE_BYTES + 1)
            if len(raw) > MAX_TRANSCRIPT_SOURCE_BYTES:
                return TranscriptReadResult("", False)
            text, parsed = _extract_json_document(raw, max_chars)
            return TranscriptReadResult(text, parsed)
        with path.open("rb") as stream:
            raw = stream.read(MAX_TRANSCRIPT_SOURCE_BYTES + 1)
        if len(raw) > MAX_TRANSCRIPT_SOURCE_BYTES:
            return TranscriptReadResult("", False)
        return TranscriptReadResult(
            raw.decode("utf-8", errors="ignore")[-max_chars:],
            True,
        )
    except OSError:
        return TranscriptReadResult("", False)


def read_transcript_tail(
    path: Path,
    max_chars: int = MAX_TRANSCRIPT_CHARS,
    *,
    delete_after: bool = False,
) -> str:
    result = _read_transcript_tail_result(
        path,
        max_chars,
        staged=delete_after,
    )
    if delete_after and result.successful:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return result.text


def _build_flush_queue_payload(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str = "unknown",
    trigger: str = "",
    project_slug: str = "",
    project_root: str = "",
    occurred_at: str = "",
    project_identity_confirmed: bool = False,
) -> dict[str, object] | None:
    """Build one redacted immediate/deferred classification request."""
    transcript_excerpt = _normalize_transcript_excerpt(transcript_excerpt)
    if not transcript_excerpt:
        return None
    capture_id = build_capture_id(
        transcript_excerpt,
        event,
        session_id=session_id,
        trigger=trigger,
        project_slug=project_slug,
        project_root=project_root,
    )
    transcript_excerpt = redact_secrets(transcript_excerpt)

    prompt = f"""You are classifying + distilling a Claude Code session transcript.

Event: {event}

=== STEP 1 — CLASSIFY ===
First, decide the tier of this session by scanning the transcript:

- FLUSH_MAJOR  — requires a concrete DECISION with rationale or a reusable
  LESSON/pattern worth remembering across sessions.

- FLUSH_MINOR  — contains only one or more of: a concrete debug GOTCHA
  (symptom→cause→fix), an OPEN QUESTION worth returning to, or a non-obvious
  COMMAND/snippet — but no decisions or lessons. Worth saving but not worth
  auto-compiling.

- FLUSH_OK covers status/progress updates, audit/review verdicts or findings,
  file/path/code summaries, facts derivable from code/config, navigation,
  service/system prompts, shell telemetry, and other material that a future
  session can recover without memory.

Be strict. Status updates are FLUSH_OK even if they mention real work —
the bar is "would a future session in this project benefit from knowing
this?". When in doubt, choose the lower tier.

=== STEP 2 — DISTILL (skip for FLUSH_OK) ===
For FLUSH_MAJOR and FLUSH_MINOR, produce a Markdown block with ONLY
these sections that apply (skip empty sections):

- **Decisions made** — concrete choices with reasons (MAJOR only).
- **Lessons / patterns** — reusable insights (MAJOR only).
- **Commands / snippets** — non-obvious invocations (any tier).
- **Gotchas / debugging** — symptom → cause → fix (any tier).
- **Open questions** — unresolved, worth returning to (any tier).

Each included section must contain at least one non-empty bullet beginning
with `- `. Do not emit prose outside sections. FLUSH_MINOR may use only
Commands / snippets, Gotchas / debugging, and Open questions.

Be terse. Each bullet should fit on one line. Do NOT narrate what was
done — that is status, not memory.

=== OUTPUT FORMAT ===
Emit EXACTLY one of these tokens as the FIRST line of your response,
followed by a blank line, then (if MAJOR/MINOR) the distilled block:

For MAJOR:
FLUSH_MAJOR

<distilled markdown block>

For MINOR:
FLUSH_MINOR

<distilled markdown block>

For OK (no second line allowed):
FLUSH_OK

Do not add preamble, apologies, or trailing explanation. The first
non-blank line MUST be the tier token.

--- BEGIN TRANSCRIPT EXCERPT ---
{transcript_excerpt}
--- END TRANSCRIPT EXCERPT ---
"""

    system_prompt = (
        "You classify and distill Claude Code transcripts into a 3-tier "
        "memory scale. Your default bias is toward FLUSH_OK — most "
        "sessions are status chatter and should not pollute the daily "
        "log. You only emit FLUSH_MAJOR when you can point to a concrete "
        "decision or lesson in the transcript. No preamble, no apologies."
    )
    payload: dict[str, object] = {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "max_tokens": 1500,
        "enqueued_by": "flush_memory",
        "event": event,
        "session_id": session_id,
        "trigger": trigger,
        "project_slug": project_slug,
        "project_root": project_root,
        "occurred_at": occurred_at,
        "capture_id": capture_id,
    }
    provenance_fields = (
        "event",
        "session_id",
        "trigger",
        "project_slug",
        "project_root",
        "occurred_at",
        "enqueued_by",
    )
    provenance_values = tuple(payload[field] for field in provenance_fields)
    complete = all(
        isinstance(value, str)
        and value == value.strip()
        and 0 < len(value) <= MAX_PROVENANCE_CHARS
        and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
        and value
        == " ".join(redact_secrets(str(value or "")).split())[:MAX_PROVENANCE_CHARS]
        for value in provenance_values
    )
    if not project_identity_confirmed or not complete:
        return payload
    if (
        payload["event"] not in {"session-end", "pre-compact"}
        or any(
            str(payload[field]).casefold() == "unknown"
            for field in ("session_id", "project_slug", "project_root")
        )
        or payload["enqueued_by"] != "flush_memory"
    ):
        return payload
    raw_occurred_at = str(payload["occurred_at"])
    normalized_occurred_at = (
        f"{raw_occurred_at[:-1]}+00:00"
        if raw_occurred_at.endswith(("Z", "z"))
        else raw_occurred_at
    )
    try:
        datetime.fromisoformat(normalized_occurred_at)
    except ValueError:
        return payload
    payload["provenance_version"] = 1
    return payload


def _enqueue_flush_payload(payload: dict[str, object]) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from memory_queue import enqueue

        enqueue("flush", payload)
    except Exception:
        return False
    return True


def _enqueue_transcript_fallback(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str,
    trigger: str,
    project_slug: str,
    project_root: str,
    occurred_at: str,
    project_identity_confirmed: bool = False,
) -> bool:
    try:
        payload = _build_flush_queue_payload(
            transcript_excerpt,
            event,
            session_id=session_id,
            trigger=trigger,
            project_slug=project_slug,
            project_root=project_root,
            occurred_at=occurred_at,
            project_identity_confirmed=project_identity_confirmed,
        )
    except Exception:
        return False
    return payload is not None and _enqueue_flush_payload(payload)


def summarize_with_llm(
    transcript_excerpt: str,
    event: str,
    *,
    session_id: str = "unknown",
    trigger: str = "",
    project_slug: str = "",
    project_root: str = "",
    occurred_at: str = "",
    enqueue_on_unavailable: bool = True,
    project_identity_confirmed: bool = False,
) -> str:
    """Ask the LLM to classify + distill the transcript into a tier + body."""
    request = _build_flush_queue_payload(
        transcript_excerpt,
        event,
        session_id=session_id,
        trigger=trigger,
        project_slug=project_slug,
        project_root=project_root,
        occurred_at=occurred_at,
        project_identity_confirmed=project_identity_confirmed,
    )
    if request is None:
        return ""

    def defer_unavailable() -> None:
        if enqueue_on_unavailable and not _enqueue_flush_payload(request):
            raise RuntimeError("durable flush enqueue failed")

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        defer_unavailable()
        return ""
    try:
        text = call_llm(
            str(request["prompt"]),
            str(request["system_prompt"]),
            max_tokens=int(request["max_tokens"]),
        )
    except Exception:
        defer_unavailable()
        return ""
    if not text:
        # No backend available (call_llm returned None) — enqueue for
        # deferred processing so the content isn't silently lost as
        # FLUSH_OK. Drained at the next active session via memory_queue.
        defer_unavailable()
        return ""
    return text.strip()


def append_daily(day: str, block: str) -> Path:
    from daily_log_append import locked_append

    out = DAILY_DIR / f"{day}.md"
    locked_append(out, block)
    return out


def append_daily_once(day: str, block: str, marker: str) -> Path:
    from daily_log_append import locked_append_once

    out = DAILY_DIR / f"{day}.md"
    return locked_append_once(out, block, marker)


def dedupe_key(
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> str:
    identity = json.dumps(
        {
            "event": str(event),
            "occurred_at": str(occurred_at),
            "project_root": str(project_root),
            "project_slug": str(project_slug),
            "session_id": str(session_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def direct_flush_marker(
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> str:
    key = dedupe_key(
        session_id,
        event,
        occurred_at,
        project_slug,
        project_root,
    )
    return f"<!-- llm-wiki-direct-flush: {key} -->"


def capture_marker(capture_id: str) -> str:
    if not isinstance(capture_id, str) or CAPTURE_ID_RE.fullmatch(capture_id) is None:
        raise ValueError("capture_id must be canonical lowercase 64-hex")
    return f"<!-- llm-wiki-capture: {capture_id} -->"


def should_skip(
    state: dict,
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> bool:
    key = dedupe_key(
        session_id,
        event,
        occurred_at,
        project_slug,
        project_root,
    )
    last = state.get("flush_dedupe", {}).get(key)
    if not last:
        return False
    return (time.time() - float(last)) < DEDUPE_WINDOW_SECONDS


def record_flush(
    state: dict,
    session_id: str,
    event: str,
    occurred_at: str,
    project_slug: str,
    project_root: str,
) -> None:
    dedupe = state.setdefault("flush_dedupe", {})
    key = dedupe_key(
        session_id,
        event,
        occurred_at,
        project_slug,
        project_root,
    )
    dedupe[key] = time.time()
    # Prune stale entries so the dict doesn't grow unbounded.
    cutoff = time.time() - DEDUPE_WINDOW_SECONDS * 4
    stale = [k for k, ts in dedupe.items() if float(ts) < cutoff]
    for k in stale:
        del dedupe[k]


def maybe_trigger_compile(state: dict, daily_path: Path, tier: str) -> None:
    """Spawn compile only for FLUSH_MAJOR content, after the hour cutoff.

    Always goes through `maybe_compile.spawn_compile_if_idle` so the PID
    lock is the single concurrency gate (hooks / wrappers / schedulers
    must not spawn `compile_memory.py` directly).
    """
    if tier != "major":
        return
    try:
        hour_cutoff = int(os.environ.get("MEMORY_COMPILE_AFTER_HOUR", "18"))
    except ValueError:
        hour_cutoff = 18
    if datetime.now().hour < hour_cutoff:
        return
    current_hash = file_hash(daily_path)
    compiled = trusted_compiled_daily_hashes(state, root=ROOT).get(daily_path.name)
    if compiled == current_hash:
        return

    # Cooldown: on a busy day every session-end after 18:00 mutates the
    # daily log (hash changes) and would otherwise re-spawn a compile
    # process each time. Rate-limit to one spawn per cooldown window.
    # Tune via MEMORY_COMPILE_COOLDOWN_SECONDS (default 900 = 15 min).
    # Set to 0 to disable cooldown entirely.
    try:
        cooldown_s = int(os.environ.get("MEMORY_COMPILE_COOLDOWN_SECONDS", "900"))
    except ValueError:
        cooldown_s = 900
    if cooldown_s > 0:
        last_spawned_raw = state.get("last_compile_spawned_at")
        if last_spawned_raw:
            try:
                last_spawned = datetime.fromisoformat(last_spawned_raw)
                elapsed = (datetime.now() - last_spawned).total_seconds()
                if elapsed < cooldown_s:
                    return
            except (ValueError, TypeError):
                pass

    spawned_at = datetime.now().isoformat(timespec="seconds")
    spawned, reason = spawn_compile_if_idle(force=False)
    if spawned:
        state["last_compile_spawned_at"] = spawned_at
    state["last_compile_spawned_trigger"] = "auto"
    state["last_compile_spawned_daily"] = daily_path.name
    state["last_compile_spawned_tier"] = tier
    state["last_compile_spawned_reason"] = reason
    state.setdefault("compile_triggers", []).append(
        {
            "at": spawned_at,
            "daily": daily_path.name,
            "trigger": "auto",
            "tier": tier,
            "spawned": spawned,
            "reason": reason,
        }
    )
    state["compile_triggers"] = state["compile_triggers"][-20:]


def _is_valid_flush_ok_response(raw: str) -> bool:
    return str(raw or "").strip() == "FLUSH_OK"


def _process_flush(
    args: argparse.Namespace,
    occurred_at: str,
    staged_transcript: str | None,
) -> FlushProcessStatus:
    project_slug = str(getattr(args, "project_slug", ""))
    project_root = str(getattr(args, "project_root", ""))
    project_identity_confirmed = False

    def result(code: int, durable: bool) -> FlushProcessStatus:
        return FlushProcessStatus(
            code,
            durable,
            project_slug,
            project_root,
            project_identity_confirmed,
        )

    try:
        identity = _resolve_project_identity(project_slug, project_root)
    except Exception:
        return result(2, False)
    if identity is None:
        return result(0, False)
    project_slug, resolved_project_root = identity
    project_root = str(resolved_project_root)
    project_identity_confirmed = True

    # Read-only peek for the dedupe short-circuit; the real write happens
    # inside update_state() below so we don't race with compile_memory.
    try:
        if should_skip(
            load_state(),
            args.session_id,
            args.event,
            occurred_at,
            project_slug,
            project_root,
        ):
            return result(0, True)
    except Exception:
        return result(2, False)

    def record_noop() -> FlushProcessStatus:
        def _mutate_noop(state: dict) -> None:
            if should_skip(
                state,
                args.session_id,
                args.event,
                occurred_at,
                project_slug,
                project_root,
            ):
                return
            record_flush(
                state,
                args.session_id,
                args.event,
                occurred_at,
                project_slug,
                project_root,
            )
            state["flush_empty_count"] = int(state.get("flush_empty_count", 0)) + 1
            state["last_flush_empty_at"] = datetime.now().isoformat(timespec="seconds")
            state.setdefault("flush_tier_counts", {})
            state["flush_tier_counts"]["ok"] = int(
                state["flush_tier_counts"].get("ok", 0)
            ) + 1

        try:
            update_state(_mutate_noop)
        except Exception:
            return result(2, False)
        return result(0, True)

    if staged_transcript is not None:
        transcript = staged_transcript
        transcript_read = True
    elif args.transcript_stdin:
        bounded_stdin = read_stdin_bounded(sys.stdin)
        if bounded_stdin is None:
            return result(2, False)
        transcript = _normalize_transcript_excerpt(bounded_stdin)
        transcript_read = True
    elif args.transcript:
        read_result = _read_transcript_tail_result(Path(args.transcript))
        if not read_result.successful:
            return result(2, False)
        transcript = read_result.text
        transcript_read = True
    else:
        transcript = ""
        transcript_read = False

    if not transcript.strip():
        return record_noop() if transcript_read else result(2, False)

    capture_id = build_capture_id(
        transcript,
        args.event,
        session_id=args.session_id,
        trigger=args.trigger,
        project_slug=project_slug,
        project_root=project_root,
    )

    def enqueue_transcript() -> None:
        if not _enqueue_transcript_fallback(
            transcript,
            args.event,
            session_id=args.session_id,
            trigger=args.trigger,
            project_slug=project_slug,
            project_root=project_root,
            occurred_at=occurred_at,
            project_identity_confirmed=project_identity_confirmed,
        ):
            raise RuntimeError("durable flush enqueue failed")

    def defer_transcript() -> FlushProcessStatus:
        deferred_durable = False

        def _mutate_deferred(state: dict) -> None:
            nonlocal deferred_durable
            if should_skip(
                state,
                args.session_id,
                args.event,
                occurred_at,
                project_slug,
                project_root,
            ):
                deferred_durable = True
                return
            enqueue_transcript()
            deferred_durable = True
            record_flush(
                state,
                args.session_id,
                args.event,
                occurred_at,
                project_slug,
                project_root,
            )

        try:
            update_state(_mutate_deferred)
        except Exception:
            return result(2, deferred_durable)
        return result(0, deferred_durable)

    try:
        raw_summary = summarize_with_llm(
            transcript,
            args.event,
            session_id=args.session_id,
            trigger=args.trigger,
            project_slug=project_slug,
            project_root=project_root,
            occurred_at=occurred_at,
            enqueue_on_unavailable=False,
            project_identity_confirmed=project_identity_confirmed,
        )
    except Exception:
        return result(2, False)

    if not raw_summary:
        return defer_transcript()

    try:
        tier, body = _classify_response(raw_summary)
    except ValueError as exc:
        print(f"flush_memory: {exc}", file=sys.stderr)
        return defer_transcript()

    # FLUSH_OK: nothing worth persisting. Still record dedupe so retries
    # don't hammer the SDK. Still consider auto-compile in case the day's
    # log already has prior MAJOR content worth compiling.
    if tier == "ok":
        if not _is_valid_flush_ok_response(raw_summary):
            return result(2, False)
        return record_noop()

    # FLUSH_MAJOR or FLUSH_MINOR: append the structured body to today's
    # daily log with a Tier: marker so the compile pipeline and human
    # readers can see what kind of content this is.
    marker = capture_marker(capture_id)
    try:
        day, block = render_flush_block(
            tier,
            body,
            event=args.event,
            session_id=args.session_id,
            trigger=args.trigger,
            project_slug=project_slug,
            project_root=project_root,
            occurred_at=occurred_at,
            idempotency_marker=marker,
        )
    except Exception:
        return result(2, False)

    deferred_compiles: list[tuple[Path, str]] = []
    direct_durable = False

    def _mutate(state: dict) -> None:
        nonlocal direct_durable
        if should_skip(
            state,
            args.session_id,
            args.event,
            occurred_at,
            project_slug,
            project_root,
        ):
            direct_durable = True
            return
        try:
            daily_path = append_daily_once(day, block, marker)
        except Exception:
            enqueue_transcript()
            direct_durable = True
            record_flush(
                state,
                args.session_id,
                args.event,
                occurred_at,
                project_slug,
                project_root,
            )
            return
        direct_durable = True
        record_flush(
            state,
            args.session_id,
            args.event,
            occurred_at,
            project_slug,
            project_root,
        )
        state.setdefault("flush_tier_counts", {})
        state["flush_tier_counts"][tier] = int(state["flush_tier_counts"].get(tier, 0)) + 1
        if tier == "major":
            deferred_compiles.append((daily_path, tier))

    try:
        update_state(_mutate)
    except Exception:
        return result(2, direct_durable)
    if not direct_durable:
        return result(2, False)

    for daily_path, flush_tier in deferred_compiles:
        def _trigger_and_persist(state: dict, _dp=daily_path, _ft=flush_tier) -> None:
            maybe_trigger_compile(state, _dp, _ft)
        try:
            update_state(_trigger_and_persist)
        except Exception:
            return result(2, True)
    return result(0, True)


def _delete_staged_transcript(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def main() -> int:
    args = parse_args()
    if getattr(args, "capture_id", False):
        return _capture_id_cli(args)
    source_now = datetime.now().astimezone()
    occurred = _occurred_datetime(getattr(args, "occurred_at", ""), source_now)
    occurred_at = occurred.isoformat(timespec="microseconds")
    staged_transcript: str | None = None
    staged_path: Path | None = None
    if args.delete_transcript and args.transcript:
        staged_path = Path(args.transcript)
        read_result = _read_transcript_tail_result(
            staged_path,
            staged=True,
        )
        if not read_result.successful:
            return 2
        staged_transcript = read_result.text
    try:
        status = _process_flush(args, occurred_at, staged_transcript)
    except Exception:
        status = FlushProcessStatus(
            2,
            False,
            str(getattr(args, "project_slug", "")),
            str(getattr(args, "project_root", "")),
        )
    if staged_path is None:
        return status.code if status.durable else status.code or 2
    if status.durable:
        return status.code if _delete_staged_transcript(staged_path) else 2
    if _enqueue_transcript_fallback(
        staged_transcript or "",
        args.event,
        session_id=args.session_id,
        trigger=args.trigger,
        project_slug=status.project_slug,
        project_root=status.project_root,
        occurred_at=occurred_at,
        project_identity_confirmed=status.project_identity_confirmed,
    ):
        return 0 if _delete_staged_transcript(staged_path) else 2
    return status.code or 2


if __name__ == "__main__":
    raise SystemExit(main())
