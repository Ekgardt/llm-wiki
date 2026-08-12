"""Answer a question from memory knowledge.

Usage:
    uv run python scripts/query_memory.py "how do we handle preliminary flagging?"
    uv run python scripts/query_memory.py "..." --file-back

With --file-back, also writes the Q&A as `knowledge/notes/<slug>.md`,
regenerates the memory index, and appends to knowledge/log.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memory_state  # noqa: E402
from memory_state import (  # noqa: E402
    ROOT,
    atomic_write,
    bind_atomic_writes_to_directory,
    bounded_path_inventory,
    knowledge_publication_lock,
    parse_frontmatter_scalar,
    require_absent_atomic_target,
)
from secret_redact import redact_secrets  # noqa: E402
from vault_editorial import (  # noqa: E402
    MAX_ACTIVE_NOTE_BYTES,
    MAX_ACTIVE_NOTE_ENTRIES,
    MAX_ACTIVE_NOTE_TOTAL_BYTES,
    ActiveNote,
    active_note_page_logical_identities,
    is_safe_root_relative_markdown_path,
    read_bounded_note,
    select_active_notes,
)

MEMORY = ROOT / "knowledge"
INDEX = MEMORY / "index.md"
LOG = MEMORY / "log.md"
QA_DIR = MEMORY / "notes"  # flat layout: all notes live directly under knowledge/notes/

MAX_ANSWER_SOURCE_PAGES = 5
# Empty, redacted, or stale-ranked pages consume scan budget, not output slots.
MAX_ANSWER_SOURCE_CANDIDATES = MAX_ANSWER_SOURCE_PAGES * 4
MAX_ANSWER_SOURCE_CHARS = 16 * 1024
MAX_ANSWER_SOURCE_TOTAL_CHARS = 64 * 1024
MAX_ANSWER_TEXT_CHARS = 64 * 1024
MAX_EVIDENCE_QUOTE_CHARS = 16 * 1024
MAX_CITATION_JSON_CHARS = MAX_ANSWER_TEXT_CHARS
MAX_CITATION_JSON_BYTES = MAX_CITATION_JSON_CHARS * 4
MAX_CITATION_JSON_DEPTH = 4
MAX_CITATION_JSON_MEMBERS = 8
PUBLICATION_LOCK_TIMEOUT_SECONDS = 30.0
SOURCE_TRUNCATION_MARKER = "\n[bounded source content truncated]"

_ANSWER_SECTION_RE = re.compile(
    r"^\*\*(Answer|Sources|Confidence):\*\*(?:[ \t]*(.*))?$",
    re.MULTILINE,
)
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)
_REDACTION_MARKER_ONLY_RE = re.compile(r"\[REDACTED(?:_[A-Z0-9_]+)?\]")


@dataclass(frozen=True)
class AnswerSource:
    note: ActiveNote
    exposed_content: str
    raw_exposed_content: str | None = None


@dataclass(frozen=True)
class _BoundedSourceBody:
    content: str
    raw_content: str


@dataclass(frozen=True)
class GeneratedAnswer:
    text: str
    sources: tuple[AnswerSource | ActiveNote, ...]


@dataclass(frozen=True)
class ResolvedCitation:
    note: ActiveNote
    quote: str


@dataclass(frozen=True)
class ParsedAnswer:
    answer: str
    confidence: str
    citations: tuple[ResolvedCitation, ...]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("question", type=str)
    p.add_argument("--file-back", action="store_true")
    return p.parse_args()


def slugify(s: str, max_len: int = 60) -> str:
    """Produce a filesystem-safe slug from a natural-language question.

    Unicode-aware: preserves Cyrillic, Latin, digits, and any other
    alphanumerics (via `\\w`). Previously `[^a-z0-9]+` stripped
    everything non-ASCII, collapsing every Russian question to
    ``"question"`` and causing QA pages to overwrite each other.

    Collision guard: if multiple questions sanitize to the same slug
    (e.g. differing only by punctuation), append a short hash of the
    original question to keep pages distinct. The hash is
    deterministic so the same question maps to the same slug across
    runs.
    """
    s_norm = s.lower().strip()
    # \w = [A-Za-z0-9_] in ASCII mode, but with re.UNICODE (Python 3
    # default for str patterns) it matches any letter/digit in any
    # script. Unsafe chars become hyphens; collapse runs.
    slug = re.sub(r"[^\w]+", "-", s_norm, flags=re.UNICODE).strip("-_")
    # Always append a deterministic hash so that questions differing
    # only by punctuation / emoji / non-word chars still get distinct
    # filenames. Without this, "???" / "!!!" / "💥" would all collapse
    # to `question` and overwrite each other.
    short_hash = hashlib.sha256(s_norm.encode("utf-8")).hexdigest()[:6]
    if not slug:
        # Pure-punctuation / emoji-only input — no usable prefix. Still
        # disambiguate via the hash alone.
        return f"question-{short_hash}"
    head = slug[: max(0, max_len - 7)]  # -7 for "-<6hex>"
    return f"{head}-{short_hash}" if head else f"question-{short_hash}"


def select_answer_sources(question: str) -> tuple[ActiveNote, ...]:
    selection = select_active_notes(QA_DIR, root=ROOT)
    if not selection.notes:
        return ()
    try:
        from search_memory import search

        results = search(question, limit=MAX_ANSWER_SOURCE_CANDIDATES)
    except (OSError, RuntimeError, ValueError):
        return ()
    by_path = {note.relative_path: note for note in selection.notes}
    selected: list[ActiveNote] = []
    seen: set[str] = set()
    for result in results[:MAX_ANSWER_SOURCE_CANDIDATES]:
        path = result.get("path") if isinstance(result, dict) else None
        note = by_path.get(path) if isinstance(path, str) else None
        if note is None or note.relative_path in seen:
            continue
        selected.append(note)
        seen.add(note.relative_path)
    return tuple(selected)


def _bounded_source_body(content: str, remaining: int) -> _BoundedSourceBody:
    raw_body = _FRONTMATTER_RE.sub("", content, count=1).rstrip()
    limit = min(MAX_ANSWER_SOURCE_CHARS, max(0, remaining))
    sanitized_body = redact_secrets(raw_body)
    if len(raw_body) <= limit and len(sanitized_body) <= limit:
        return _BoundedSourceBody(sanitized_body.rstrip(), raw_body)
    if limit <= len(SOURCE_TRUNCATION_MARKER):
        return _BoundedSourceBody(SOURCE_TRUNCATION_MARKER[:limit].rstrip(), "")

    content_limit = limit - len(SOURCE_TRUNCATION_MARKER)
    raw_exposed = raw_body[:content_limit].rstrip()
    sanitized_exposed = redact_secrets(raw_exposed).rstrip()
    for _attempt in range(16):
        if len(sanitized_exposed) <= content_limit:
            break
        excess = len(sanitized_exposed) - content_limit
        next_length = max(0, len(raw_exposed) - max(1, excess))
        raw_exposed = raw_exposed[:next_length].rstrip()
        sanitized_exposed = redact_secrets(raw_exposed).rstrip()
    else:
        raw_exposed = ""
        sanitized_exposed = ""
    rendered = sanitized_exposed + SOURCE_TRUNCATION_MARKER
    return _BoundedSourceBody(rendered.rstrip(), raw_exposed)


def _prepare_answer_sources(
    notes: tuple[ActiveNote, ...],
) -> tuple[tuple[AnswerSource, ...], tuple[str, ...]]:
    remaining = MAX_ANSWER_SOURCE_TOTAL_CHARS
    exposed_sources: list[AnswerSource] = []
    rendered_sources: list[str] = []
    for note in notes[:MAX_ANSWER_SOURCE_CANDIDATES]:
        if redact_secrets(note.relative_path) != note.relative_path:
            continue
        body = _bounded_source_body(note.content, remaining)
        if not body.content:
            continue
        rendered_sources.append(
            f"--- source: {note.relative_path} ---\n{body.content}"
        )
        remaining -= len(body.content)
        exposed_sources.append(
            AnswerSource(
                note=note,
                exposed_content=body.content,
                raw_exposed_content=body.raw_content,
            )
        )
        if len(exposed_sources) >= MAX_ANSWER_SOURCE_PAGES:
            break
    return tuple(exposed_sources), tuple(rendered_sources)


def answer_with_sources(question: str) -> GeneratedAnswer:
    """Retrieve canonical source snapshots and ask the provider for grounded output."""
    try:
        from llm_client import call_llm
    except ImportError:
        return GeneratedAnswer("(llm_client not available)", ())

    sources = select_answer_sources(question)
    exposed_sources, rendered_sources = _prepare_answer_sources(sources)

    source_text = "\n\n".join(rendered_sources) or "(no canonical source page matched)"
    safe_question = redact_secrets(question)
    prompt = f"""Answer the question using ONLY the canonical source pages below.
Do not cite a page that is not included. Every factual answer must include at
least one exact verbatim quote copied from its cited page. If the sources do
not answer the question, say so and leave Sources empty; such an answer cannot
be filed back.

Respond in this shape:

**Answer:**
...

**Sources:**
- {{"path":"knowledge/notes/exact.md","quote":"exact verbatim text"}}

**Confidence:** high | medium | low - why.

Each Sources bullet must contain exactly one JSON object with exactly `path`
and `quote`. Paths must be the exact ROOT-relative POSIX paths shown below.

--- canonical source pages ---
{source_text}

--- question ---
{safe_question}
"""
    text = call_llm(
        prompt,
        system_prompt=(
            "Answer only from the supplied canonical memory pages. "
            "Return exact path and verbatim quote evidence."
        ),
        max_tokens=1500,
    )
    rendered = text.strip() if text else "(no LLM response)"
    return GeneratedAnswer(rendered, exposed_sources)


def answer(question: str) -> str:
    """Compatibility wrapper returning the grounded provider text."""
    return answer_with_sources(question).text


def _strict_json_object(value: str) -> dict:
    try:
        return memory_state.decode_json_object_strict(
            value,
            max_bytes=MAX_CITATION_JSON_BYTES,
            max_chars=MAX_CITATION_JSON_CHARS,
            max_depth=MAX_CITATION_JSON_DEPTH,
            max_members=MAX_CITATION_JSON_MEMBERS,
        )
    except ValueError:
        raise
    except (MemoryError, RecursionError, OverflowError, UnicodeError, TypeError) as exc:
        raise ValueError("citation JSON parser resource failure") from exc


def _valid_root_relative_posix_path(value: str) -> bool:
    return is_safe_root_relative_markdown_path(value)


def _parse_grounded_answer(
    answer_text: str,
    sources: tuple[AnswerSource | ActiveNote, ...],
) -> ParsedAnswer:
    if not isinstance(answer_text, str) or len(answer_text) > MAX_ANSWER_TEXT_CHARS:
        raise ValueError("answer contract is missing or oversized")
    matches = list(_ANSWER_SECTION_RE.finditer(answer_text))
    if [match.group(1) for match in matches] != ["Answer", "Sources", "Confidence"]:
        raise ValueError("answer contract sections are missing, duplicated, or out of order")
    if answer_text[: matches[0].start()].strip():
        raise ValueError("answer contract has content before the Answer section")

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(answer_text)
        inline = (match.group(2) or "").strip()
        following = answer_text[match.end() : end].strip()
        sections[match.group(1)] = "\n".join(
            part for part in (inline, following) if part
        ).strip()

    answer_body = sections["Answer"]
    confidence_text = sections["Confidence"]
    if not answer_body:
        raise ValueError("answer contract has an empty Answer section")
    confidence_match = re.match(r"^(high|medium|low)(?:\s+-\s+.*)?$", confidence_text)
    if confidence_match is None:
        raise ValueError("answer confidence is malformed")

    by_path: dict[str, tuple[ActiveNote, str, str]] = {}
    for source in sources:
        if not isinstance(source, AnswerSource) or source.raw_exposed_content is None:
            raise ValueError("source lacks exact bounded provider-visible spans")
        note = source.note
        exposed_content = source.exposed_content
        raw_exposed_content = source.raw_exposed_content
        by_path[note.relative_path] = (note, exposed_content, raw_exposed_content)
    citations: list[ResolvedCitation] = []
    seen_paths: set[str] = set()
    seen_quotes: set[str] = set()
    for line in sections["Sources"].splitlines():
        if not line.strip():
            continue
        if not line.startswith("- "):
            raise ValueError("source citation is malformed")
        try:
            citation = _strict_json_object(line[2:].strip())
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("source citation is malformed") from exc
        if set(citation) != {"path", "quote"}:
            raise ValueError("source citation must contain exactly path and quote")
        path = citation["path"]
        quote = citation["quote"]
        if not isinstance(path, str) or not _valid_root_relative_posix_path(path):
            raise ValueError("source citation path is not exact ROOT-relative POSIX syntax")
        resolved = by_path.get(path)
        if resolved is None:
            raise ValueError("source citation is not an active canonical selected page")
        note, exposed_content, raw_exposed_content = resolved
        if (
            not isinstance(quote, str)
            or not quote
            or not any(not char.isspace() for char in quote)
            or len(quote) > MAX_EVIDENCE_QUOTE_CHARS
            or any(ord(char) < 32 and char not in "\n\t" for char in quote)
            or _REDACTION_MARKER_ONLY_RE.fullmatch(quote) is not None
            or quote not in exposed_content
            or quote not in raw_exposed_content
        ):
            raise ValueError("source evidence is not an exact quote from the cited page")
        if path in seen_paths or quote in seen_quotes:
            raise ValueError("source citation is duplicated")
        seen_paths.add(path)
        seen_quotes.add(quote)
        citations.append(ResolvedCitation(note, quote))
    if not citations:
        raise ValueError("at least one resolved source citation is required")
    return ParsedAnswer(
        answer=answer_body,
        confidence=confidence_match.group(1),
        citations=tuple(citations),
    )


def file_back(
    question: str,
    answer_text: str,
    *,
    sources: tuple[AnswerSource | ActiveNote, ...] | None = None,
) -> Path:
    with knowledge_publication_lock(timeout=PUBLICATION_LOCK_TIMEOUT_SECONDS):
        return _file_back_locked(question, answer_text, sources=sources)


def _file_back_locked(
    question: str,
    answer_text: str,
    *,
    sources: tuple[AnswerSource | ActiveNote, ...] | None,
) -> Path:
    question = redact_secrets(question)
    answer_text = redact_secrets(answer_text)
    reserved_entry_limit = MAX_ACTIVE_NOTE_ENTRIES - 1
    fresh_selection = select_active_notes(
        QA_DIR,
        root=ROOT,
        max_entries=reserved_entry_limit,
        max_page_bytes=MAX_ACTIVE_NOTE_BYTES,
        max_total_bytes=MAX_ACTIVE_NOTE_TOTAL_BYTES,
    )
    fresh_by_path = {note.relative_path: note for note in fresh_selection.notes}
    if sources is None:
        selected_sources, _rendered = _prepare_answer_sources(fresh_selection.notes)
    else:
        fresh_notes: list[ActiveNote] = []
        for supplied in sources:
            source = supplied.note if isinstance(supplied, AnswerSource) else supplied
            if not is_safe_root_relative_markdown_path(source.relative_path):
                raise ValueError("supplied source path is not exact ROOT-relative POSIX syntax")
            fresh = fresh_by_path.get(source.relative_path)
            expected_path = ROOT.joinpath(*PurePosixPath(source.relative_path).parts)
            if (
                fresh is None
                or os.path.normcase(os.path.abspath(source.path))
                != os.path.normcase(os.path.abspath(expected_path))
                or os.path.normcase(os.path.abspath(fresh.path))
                != os.path.normcase(os.path.abspath(expected_path))
            ):
                raise ValueError("cited source is no longer the active canonical page")
            if source != fresh:
                if (
                    source.file_identity != fresh.file_identity
                    or source.content_sha256 != fresh.content_sha256
                    or source.content != fresh.content
                ):
                    raise ValueError("cited source changed after answer generation")
                raise ValueError("cited source metadata does not match the canonical page")
            fresh_notes.append(fresh)

        derived_sources, _rendered = _prepare_answer_sources(tuple(fresh_notes))
        if len(derived_sources) != len(sources):
            raise ValueError("supplied source was not visible in the bounded provider prompt")
        for supplied, derived in zip(sources, derived_sources, strict=True):
            if (
                isinstance(supplied, AnswerSource)
                and (
                    supplied.exposed_content != derived.exposed_content
                    or (
                        supplied.raw_exposed_content is not None
                        and supplied.raw_exposed_content != derived.raw_exposed_content
                    )
                )
            ):
                raise ValueError(
                    "supplied provider-visible source exposure does not match canonical bytes"
                )
        selected_sources = derived_sources

    parsed = _parse_grounded_answer(answer_text, selected_sources)
    for citation in parsed.citations:
        source = citation.note
        fresh = fresh_by_path.get(source.relative_path)
        expected_path = ROOT.joinpath(*PurePosixPath(source.relative_path).parts)
        if (
            fresh is None
            or os.path.normcase(os.path.abspath(source.path))
            != os.path.normcase(os.path.abspath(expected_path))
            or os.path.normcase(os.path.abspath(fresh.path))
            != os.path.normcase(os.path.abspath(expected_path))
        ):
            raise ValueError("cited source is no longer the active canonical page")
        if (
            source.file_identity != fresh.file_identity
            or source.content_sha256 != fresh.content_sha256
            or source.content != fresh.content
        ):
            raise ValueError("cited source changed after answer generation")
        if citation.quote not in fresh.content:
            raise ValueError("source evidence is not an exact quote from the cited page")
    slug = slugify(question)
    out = QA_DIR / f"{slug}.md"
    inventory = bounded_path_inventory(
        QA_DIR,
        "*.md",
        reserved_entry_limit,
        recursive=True,
        kind="file",
        required_root=True,
    )
    if inventory.incomplete:
        raise OSError("Q&A publication inventory is incomplete or unsafe")
    for existing in inventory.paths:
        if existing.name.casefold() != out.name.casefold():
            continue
        relative = existing.relative_to(QA_DIR)
        if len(relative.parts) == 1:
            raise FileExistsError(out)
        if any(part.casefold() == "archive" for part in relative.parts[:-1]):
            continue
        status = parse_frontmatter_scalar(read_bounded_note(existing), "status")
        if status.value is not None and status.value.casefold() in {
            "archived",
            "superseded",
        }:
            continue
        raise FileExistsError(existing)
    today = datetime.now().strftime("%Y-%m-%d")
    summary_line = question.strip().rstrip("?").strip()
    title = " ".join(question.strip().rstrip("?").splitlines())
    summary = " ".join(summary_line.splitlines())
    evidence_lines: list[str] = []
    for citation in parsed.citations:
        evidence_lines.extend(
            (
                f"- Source: `{citation.note.relative_path}`",
                f"  Source SHA-256: `{citation.note.content_sha256}`",
                f"  Verbatim evidence: {json.dumps(citation.quote, ensure_ascii=False)}",
            )
        )
    page = (
        "---\n"
        "type: qa\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f'description: "Settled answer captured on {today}"\n'
        f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
        f"confidence: {parsed.confidence}\n"
        "source_authority: ai-derived\n"
        "---\n\n"
        f"# {title}?\n\n"
        f"One-sentence summary: Settled answer to {json.dumps(summary, ensure_ascii=False)} captured on {today}.\n\n"
        "## Question\n"
        f"{question.strip()}\n\n"
        "## Answer\n"
        f"{parsed.answer}\n\n"
        "## Evidence\n"
        + "\n".join(evidence_lines)
        + "\n\n"
        "## Related\n"
        "-\n"
    )
    try:
        page_size = len(page.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("Q&A candidate is not valid UTF-8") from exc
    if page_size > MAX_ACTIVE_NOTE_BYTES:
        raise ValueError("Q&A candidate exceeds the active-note UTF-8 byte limit")
    admitted_selection = select_active_notes(
        QA_DIR,
        root=ROOT,
        max_entries=reserved_entry_limit,
        max_page_bytes=MAX_ACTIVE_NOTE_BYTES,
        max_total_bytes=MAX_ACTIVE_NOTE_TOTAL_BYTES - page_size,
    )
    if admitted_selection.generation != fresh_selection.generation:
        raise OSError("Q&A publication inventory changed during capacity admission")
    prospective_identities = active_note_page_logical_identities(out.stem, page)
    if prospective_identities & admitted_selection.candidate_identities:
        raise FileExistsError("Q&A candidate collides with an active canonical identity")
    with bind_atomic_writes_to_directory(out.parent):
        with require_absent_atomic_target():
            atomic_write(out, page)
    return out


def rebuild_index() -> bool:
    """Run the memory index rebuild. Returns True on success.

    Callers should surface a warning if False — the page was written
    correctly, but `knowledge/index.md` is now stale until the next
    successful rebuild.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rebuild_memory_index.py")],
        check=False,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        print(f"query_memory: rebuild_memory_index FAILED (rc={result.returncode}): {err}")
        return False
    return True


def append_log(entry: str) -> None:
    from daily_log_append import locked_append

    if not LOG.exists():
        atomic_write(LOG, "# Session Memory Log\n\n")
    locked_append(LOG, entry)


def main() -> int:
    args = parse_args()
    generated = answer_with_sources(args.question)
    print(generated.text)

    if generated.text.startswith("("):
        return 1

    if args.file_back:
        try:
            out = file_back(args.question, generated.text, sources=generated.sources)
        except (OSError, ValueError) as exc:
            print(f"query_memory: publication failed: {exc}", file=sys.stderr)
            return 1
        index_ok = rebuild_index()
        suffix = "" if index_ok else " (WARN: knowledge/index.md rebuild failed — page written, index stale)"
        append_log(
            f"- {datetime.now().strftime('%Y-%m-%d')} — Filed Q&A `{out.relative_to(ROOT).as_posix()}` via `query_memory.py --file-back`.{suffix}"
        )
        print(f"\n[filed] {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
