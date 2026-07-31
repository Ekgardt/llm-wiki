"""Phase 0 regression tests: new compile_memory audit + snapshot features.

Three guarantees these tests lock in:

1. `parse_compile_audit` correctly extracts structured counts from a
   well-formed COMPILE_AUDIT line.
2. `parse_compile_audit` tolerates partial / malformed / missing audits
   without crashing (returns empty dict, not an exception).
3. `existing_knowledge_snapshot` includes Title + Summary lines so the
   LLM can satisfy the DEDUP-BEFORE-CREATE rule — not just bare filenames.

These guard against re-regression when the prompt is later edited: if
someone strips the COMPILE_AUDIT sentinel or reverts the snapshot to
filenames-only, the suite fails loudly.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


def test_completed_metadata_only_codex_stop_record_is_not_compile_evidence(tmp_path):
    import compile_memory

    completion = "<!-- llm-wiki-record-complete -->"
    project_root = str((tmp_path / "alpha").resolve())
    text = (
        "## [09:00:00] Codex Stop | stop-session\n"
        "- Trigger: `codex-stop`\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(project_root)}\n"
        "- Transcript: `session.jsonl`\n"
        f"{completion}\n\n"
        "## [10:00:00] session-end | durable-session\n"
        "- Trigger: `hook`\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(project_root)}\n\n"
        "DURABLE_COMPILE_EVIDENCE\n"
        f"{completion}\n"
    )

    blocks = compile_memory.extract_meaningful_blocks(text)

    assert len(blocks) == 1
    assert "durable-session" in blocks[0]
    assert "DURABLE_COMPILE_EVIDENCE" in blocks[0]
    assert "stop-session" not in blocks[0]


def test_completed_flush_keeps_scope_but_removes_capture_metadata_and_markers(tmp_path):
    import compile_memory

    project_root = str((tmp_path / "alpha").resolve())
    completion = "<!-- llm-wiki-record-complete -->"
    idempotency = f"<!-- llm-wiki-direct-flush: {'a' * 64} -->"
    text = (
        "## [11:00:00] deferred-session-end | flush-session\n"
        "- Trigger: `codex-stop`\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(project_root)}\n"
        "- Transcript: `session.jsonl`\n"
        "- Tier: `major`\n"
        "- Source session: `flush-session`\n\n"
        "DURABLE_FLUSH_EVIDENCE\n"
        f"{idempotency}\n"
        f"{completion}\n"
        "- `[11:01:00] prompt | flush-session | alpha` COMPACT_PROMPT_NOISE\n"
        "- `[11:02:00] tool | flush-session | alpha | Edit` COMPACT_TOOL_NOISE\n"
    )

    blocks = compile_memory.extract_meaningful_blocks(text)

    assert len(blocks) == 1
    block = blocks[0]
    assert "DURABLE_FLUSH_EVIDENCE" in block
    assert "- Project slug: `alpha`" in block
    assert f"- Project root JSON: {json.dumps(project_root)}" in block
    for noise in (
        "- Trigger:",
        "- Transcript:",
        "- Tier:",
        "- Source session:",
        idempotency,
        completion,
        "COMPACT_PROMPT_NOISE",
        "COMPACT_TOOL_NOISE",
    ):
        assert noise not in block


def test_malformed_heading_metadata_isolated_from_valid_legacy_record():
    import compile_memory

    text = (
        "## [12:00:00] session-end | malformed\n"
        "- Project slug = `forged`\n"
        "MALFORMED_SCOPE_MUST_NOT_COMPILE\n"
        "## [12:01:00] deferred-pre-compact\n"
        "LEGACY_DURABLE_EVIDENCE\n"
    )

    blocks = compile_memory.extract_meaningful_blocks(text)

    assert len(blocks) == 1
    assert "LEGACY_DURABLE_EVIDENCE" in blocks[0]
    assert "MALFORMED_SCOPE_MUST_NOT_COMPILE" not in blocks[0]


def test_compiler_sees_all_legitimate_markerless_scoped_heading_records(
    tmp_path,
):
    import compile_memory

    beta_root = str((tmp_path / "beta").resolve())
    alpha_root = str((tmp_path / "alpha").resolve())
    text = (
        "## [13:00:00] session-end | beta-session\n"
        "- Project slug: `beta`\n"
        f"- Project root JSON: {json.dumps(beta_root)}\n\n"
        "BETA_DURABLE_EVIDENCE\n"
        "## [13:01:00] session-end | alpha-session\n"
        "- Project slug: `alpha`\n"
        f"- Project root JSON: {json.dumps(alpha_root)}\n\n"
        "ALPHA_DURABLE_EVIDENCE\n"
    )

    blocks = compile_memory.extract_meaningful_blocks(text)

    assert len(blocks) == 2
    assert "BETA_DURABLE_EVIDENCE" in blocks[0]
    assert f"- Project root JSON: {json.dumps(beta_root)}" in blocks[0]
    assert "ALPHA_DURABLE_EVIDENCE" not in blocks[0]
    assert "ALPHA_DURABLE_EVIDENCE" in blocks[1]
    assert f"- Project root JSON: {json.dumps(alpha_root)}" in blocks[1]
    assert "BETA_DURABLE_EVIDENCE" not in blocks[1]


# ---------------------------------------------------------------------------
# parse_compile_audit
# ---------------------------------------------------------------------------


def test_parse_compile_audit_extracts_all_counts():
    import compile_memory  # noqa: WPS433

    raw = """Some preamble text.

COMPILE_DONE: 3 page(s) touched: knowledge/notes/patterns/foo.md, knowledge/notes/decisions/bar.md
COMPILE_AUDIT: verified 7 evidence citations; 12 dedup checks performed; 2 stubs skipped; 1 contradictions handled; 0 pages rejected as below-threshold
"""
    audit = compile_memory.parse_compile_audit(raw)
    assert audit == {
        "verified": 7,
        "dedup": 12,
        "stubs": 2,
        "contradictions": 1,
        "rejected": 0,
    }


def test_parse_compile_audit_tolerates_missing_line():
    """Legacy compiles (pre-Phase-0) don't emit COMPILE_AUDIT.

    Must return empty dict, not raise.
    """
    import compile_memory  # noqa: WPS433

    legacy = "COMPILE_DONE: 1 page(s) touched: knowledge/notes/patterns/foo.md\n"
    assert compile_memory.parse_compile_audit(legacy) == {}


def test_parse_compile_audit_tolerates_partial_line():
    """LLM may emit a partial audit (skipped a field).

    Whatever is present is extracted; missing fields are absent from
    the dict (callers use .get with default 0).
    """
    import compile_memory  # noqa: WPS433

    partial = (
        "COMPILE_DONE: 2 page(s) touched: a.md, b.md\n"
        "COMPILE_AUDIT: verified 4 evidence citations; 1 stubs skipped\n"
    )
    audit = compile_memory.parse_compile_audit(partial)
    assert audit.get("verified") == 4
    assert audit.get("stubs") == 1
    assert "dedup" not in audit
    assert "contradictions" not in audit
    assert "rejected" not in audit


def test_parse_compile_audit_handles_empty_and_none():
    import compile_memory  # noqa: WPS433

    assert compile_memory.parse_compile_audit("") == {}
    assert compile_memory.parse_compile_audit(None) == {}  # type: ignore[arg-type]


def test_parse_compile_audit_finds_last_when_multiple():
    """If the LLM accidentally emits two COMPILE_AUDIT lines (e.g. one
    mid-thinking, one final), the LAST one wins — matches COMPILE_DONE
    semantics.
    """
    import compile_memory  # noqa: WPS433

    raw = """COMPILE_AUDIT: verified 1 evidence citations; 0 dedup checks performed; 0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold

more text

COMPILE_DONE: 1 page(s) touched: foo.md
COMPILE_AUDIT: verified 5 evidence citations; 3 dedup checks performed; 1 stubs skipped; 0 contradictions handled; 1 pages rejected as below-threshold
"""
    audit = compile_memory.parse_compile_audit(raw)
    assert audit["verified"] == 5
    assert audit["dedup"] == 3
    assert audit["rejected"] == 1


# ---------------------------------------------------------------------------
# existing_knowledge_snapshot — title + summary enrichment
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_knowledge_tree(tmp_path: Path):
    """Build a minimal knowledge/notes/ tree with realistic pages."""
    knowledge = tmp_path / "knowledge" / "notes"
    for cat in ("patterns", "decisions", "debugging"):
        (knowledge / cat).mkdir(parents=True)

    (knowledge / "patterns" / "hook-defense.md").write_text(
        textwrap.dedent(
            """
            # Hook scripts defense in depth

            One-sentence summary: always fail closed and log to hook-errors.log even when SDK is missing.

            ## Lesson
            Body.
            """
        ).strip(),
        encoding="utf-8",
    )
    (knowledge / "decisions" / "use-sha256.md").write_text(
        textwrap.dedent(
            """
            # Use SHA-256 for compile incrementalism

            One-sentence summary: SHA-256 detects real content change regardless of mtime churn.

            ## Decision
            Body.
            """
        ).strip(),
        encoding="utf-8",
    )
    # Page without the conventional headers — fallback path.
    (knowledge / "debugging" / "stub.md").write_text(
        "Just a body with no H1 or summary line.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_knowledge_snapshot_includes_titles_and_summaries(temp_knowledge_tree: Path):
    """Snapshot must carry title + summary so the LLM can detect
    semantic overlap, not just slug collisions.
    """
    import compile_memory  # noqa: WPS433

    with patch.object(compile_memory, "KNOWLEDGE", temp_knowledge_tree / "knowledge" / "notes"):
        snapshot = compile_memory.existing_knowledge_snapshot()

    # Title + summary present for well-formed pages (new format:
    # «title»: summary, so both are in the snapshot)
    assert "Hook scripts defense in depth" in snapshot
    assert "always fail closed and log to hook-errors.log" in snapshot
    assert "Use SHA-256 for compile incrementalism" in snapshot
    assert "SHA-256 detects real content change" in snapshot
    # Slug still in the path prefix (for file-level reference)
    assert "patterns/hook-defense.md" in snapshot
    assert "decisions/use-sha256.md" in snapshot
    # Title is wrapped in «» to give LLM a clear anchor
    assert "«Hook scripts defense in depth»" in snapshot
    assert "«Use SHA-256 for compile incrementalism»" in snapshot


def test_knowledge_snapshot_uses_search_body_parser(tmp_path, monkeypatch):
    import compile_memory
    import search_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    content = textwrap.dedent(
        """\
        ---
        type: concept
        # Frontmatter decoy title
        One-sentence summary: Frontmatter decoy summary.
        ---
        # Canonical body title

        One-sentence summary: Canonical body summary.
        """
    )
    page = knowledge / "canonical.md"
    page.write_text(content, encoding="utf-8")
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
    expected_title, expected_summary = search_memory._extract_title_and_summary(
        content,
        page.stem,
    )

    snapshot = compile_memory.existing_knowledge_snapshot()

    assert f"«{expected_title}»: {expected_summary}" in snapshot
    assert "Frontmatter decoy title" not in snapshot
    assert "Frontmatter decoy summary" not in snapshot


def test_knowledge_snapshot_ignores_status_text_in_body(tmp_path, monkeypatch):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    page = knowledge / "body-status.md"
    page.write_text(
        "---\ntype: concept\nstatus: active\n---\n\n"
        "# Active body status page\n\n"
        "One-sentence summary: Body status text is ordinary evidence.\n\n"
        "## Evidence\n"
        "The prior document contained `status: superseded`.\n"
        "status: archived is also body prose, not metadata.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)

    snapshot = compile_memory.existing_knowledge_snapshot()

    assert "body-status.md" in snapshot
    assert "Active body status page" in snapshot


@pytest.mark.parametrize("status", ("superseded", "archived"))
def test_knowledge_snapshot_skips_valid_inactive_frontmatter_status(
    tmp_path,
    monkeypatch,
    status,
):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    (knowledge / f"{status}.md").write_text(
        f"---\ntype: concept\nstatus: {status}\n---\n\n# Hidden {status}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)

    assert compile_memory.existing_knowledge_snapshot() == "(no pages yet)"


@pytest.mark.parametrize(
    "status_field",
    (
        pytest.param("status: active\nstatus: active", id="duplicate"),
        pytest.param("status = active", id="malformed"),
        pytest.param("status: [active]", id="present-invalid"),
    ),
)
def test_knowledge_snapshot_fails_closed_on_ambiguous_status(
    tmp_path,
    monkeypatch,
    status_field,
):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    (knowledge / "ambiguous.md").write_text(
        f"---\ntype: concept\n{status_field}\n---\n\n# Must stay hidden\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)

    assert compile_memory.existing_knowledge_snapshot() == "(no pages yet)"


def test_knowledge_snapshot_skips_invalid_utf8_without_lossy_metadata(
    tmp_path,
    monkeypatch,
):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    page = knowledge / "invalid-utf8.md"
    page.write_bytes(
        b"---\ntype: concept\n---\n\n"
        b"# LOSSY_\xff_METADATA_MUST_NOT_APPEAR\n\n"
        b"One-sentence summary: INVALID_UTF8_SUMMARY\n"
    )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)

    assert compile_memory._extract_title_and_summary(page) == (page.stem, "")
    snapshot = compile_memory.existing_knowledge_snapshot()
    assert snapshot == "(no pages yet)"
    assert "LOSSY_METADATA" not in snapshot
    assert "INVALID_UTF8_SUMMARY" not in snapshot


def test_knowledge_snapshot_skips_page_above_note_byte_limit(tmp_path, monkeypatch):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    page = knowledge / "oversized.md"
    page.write_text(
        "# OVERSIZED_METADATA_MUST_NOT_APPEAR\n" + "x" * 256,
        encoding="utf-8",
    )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(
        compile_memory,
        "MAX_KNOWLEDGE_PAGE_BYTES",
        64,
        raising=False,
    )

    assert compile_memory._extract_title_and_summary(page) == (page.stem, "")
    assert compile_memory.existing_knowledge_snapshot() == "(no pages yet)"


def test_knowledge_page_reader_rejects_identity_swap_before_open(
    tmp_path,
    monkeypatch,
):
    import compile_memory

    page = tmp_path / "knowledge" / "notes" / "original.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Original identity\n", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("# Replacement identity\n", encoding="utf-8")
    real_open = Path.open
    swapped = False

    def swapping_open(path, *args, **kwargs):
        nonlocal swapped
        if path == page and not swapped:
            swapped = True
            replacement.replace(page)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapping_open)

    assert compile_memory._read_knowledge_page(page) is None
    assert swapped is True


def test_knowledge_page_reader_rechecks_lexical_identity_after_read(
    tmp_path,
    monkeypatch,
):
    import compile_memory

    page = tmp_path / "knowledge" / "notes" / "changing.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Original identity\n", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("# Replacement identity\n", encoding="utf-8")
    replacement_metadata = replacement.lstat()
    real_lstat = Path.lstat
    real_open = Path.open
    state = {"read": False}

    class ChangingHandle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            raw = self.handle.read(size)
            state["read"] = True
            return raw

    def changing_lstat(path, *args, **kwargs):
        if path == page and state["read"]:
            return replacement_metadata
        return real_lstat(path, *args, **kwargs)

    def changing_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return ChangingHandle(handle) if path == page else handle

    monkeypatch.setattr(Path, "lstat", changing_lstat)
    monkeypatch.setattr(Path, "open", changing_open)

    assert compile_memory._read_knowledge_page(page) is None
    assert state["read"] is True


def test_knowledge_page_reader_rejects_reparse_point(tmp_path, monkeypatch):
    import compile_memory

    page = tmp_path / "knowledge" / "notes" / "reparse.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Reparse content must not be read\n", encoding="utf-8")
    real_lstat = Path.lstat

    class ReparseMetadata:
        def __init__(self, metadata):
            self.metadata = metadata
            self.st_mode = metadata.st_mode
            self.st_size = metadata.st_size
            self.st_file_attributes = 0x400

        def __getattr__(self, name):
            return getattr(self.metadata, name)

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path, *args, **kwargs: ReparseMetadata(
            real_lstat(path, *args, **kwargs)
        )
        if path == page
        else real_lstat(path, *args, **kwargs),
    )

    assert compile_memory._read_knowledge_page(page) is None


def test_knowledge_snapshot_handles_missing_convention(temp_knowledge_tree: Path):
    """Pages without H1 or One-sentence summary fall back to filename
    stem, no crash.
    """
    import compile_memory  # noqa: WPS433

    with patch.object(compile_memory, "KNOWLEDGE", temp_knowledge_tree / "knowledge" / "notes"):
        snapshot = compile_memory.existing_knowledge_snapshot()

    # The stub file still appears (filename used as fallback)
    assert "debugging/stub.md" in snapshot


def test_knowledge_snapshot_empty_when_no_pages(tmp_path: Path):
    import compile_memory  # noqa: WPS433

    empty_knowledge = tmp_path / "knowledge" / "notes"
    empty_knowledge.mkdir(parents=True)
    with patch.object(compile_memory, "KNOWLEDGE", empty_knowledge):
        snapshot = compile_memory.existing_knowledge_snapshot()
    assert snapshot == "(no pages yet)"


def test_knowledge_snapshot_fails_closed_when_inventory_limit_is_exceeded(
    tmp_path,
    monkeypatch,
):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    for name in ("one", "two"):
        (knowledge / f"{name}.md").write_text(
            f"---\ntype: concept\n---\n\n# {name.title()}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(compile_memory, "MAX_KNOWLEDGE_INVENTORY_ENTRIES", 1)

    with pytest.raises(compile_memory.CompilePreparationError, match="inventory"):
        compile_memory.existing_knowledge_snapshot()


def test_compile_context_uses_bounded_agents_prefix_and_log_tail(
    tmp_path,
    monkeypatch,
):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    agents = tmp_path / "AGENTS.md"
    log = tmp_path / "knowledge" / "log.md"
    agents.write_text("AGENT_PREFIX\n" + "a" * 512, encoding="utf-8")
    log.parent.mkdir(exist_ok=True)
    log.write_text(
        "".join(f"old line {index:03d}\n" for index in range(100))
        + "LOG_TAIL_MARKER\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(compile_memory, "AGENTS", agents)
    monkeypatch.setattr(compile_memory, "LOG", log)
    monkeypatch.setattr(
        compile_memory,
        "MAX_COMPILE_CONTEXT_FILE_BYTES",
        128,
        raising=False,
    )
    real_read_text = Path.read_text

    def reject_unbounded_context_read(path, *args, **kwargs):
        if path in {agents, log}:
            raise AssertionError("context source used Path.read_text")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_unbounded_context_read)

    context = compile_memory._compile_context_snapshot(30_000)

    assert context["agents_md"].startswith("AGENT_PREFIX")
    assert len(context["agents_md"].encode("utf-8")) <= 128
    assert "LOG_TAIL_MARKER" in context["log_tail"]
    assert len(context["log_tail"].encode("utf-8")) <= 128


def test_knowledge_snapshot_stops_formatting_at_output_budget(
    tmp_path,
    monkeypatch,
):
    import compile_memory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    for index in range(3):
        (knowledge / f"page-{index}.md").write_text(
            "---\ntype: concept\n---\n\n"
            f"# Page {index}\n\n"
            f"One-sentence summary: {'x' * 80}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
    real_read = compile_memory._read_knowledge_page
    reads: list[Path] = []

    def counting_read(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(compile_memory, "_read_knowledge_page", counting_read)

    snapshot = compile_memory.existing_knowledge_snapshot(max_chars=80)

    assert len(snapshot) <= 80
    assert len(reads) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("  **Ｆail—Closed**\tAdmission!  ", "fail closed admission"),
        ("Straße / STRASSE", "strasse strasse"),
        ("Résumé... 2026", "résumé 2026"),
        ("`command_name` + [Option]", "command name option"),
        ("`**Fail Closed Admission**`", "fail closed admission"),
        ("[Fail closed](https://example.test/policy)", "fail closed"),
        ("![Fail closed policy](diagram.png)", "fail closed policy"),
        ("[Fail closed][admission-policy]", "fail closed admission policy"),
        (
            "[![Fail Closed](diagram.png)](https://example.test/policy)",
            "fail closed",
        ),
        ("[[policy|Fail Closed Admission]]", "fail closed admission"),
        ("[[Fail Closed Admission]]", "fail closed admission"),
        ("![[diagram|Fail Closed Admission]]", "fail closed admission"),
        ("![[Fail Closed](label.html)](diagram.png)", "fail closed"),
        (r"\[Fail Closed](diagram.png)", "fail closed diagram png"),
        ("[Fail Closed](diagram.png", "fail closed diagram png"),
    ),
)
def test_compile_exact_key_normalizes_compatibility_case_and_markdown(
    value,
    expected,
):
    import compile_memory

    assert compile_memory._normalize_exact_key(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("[link](/my uri)", "link my uri"),
        ("[link](foo\nbar)", "link foo bar"),
        ("[a](<b>c)", "a c"),
        ('[link](/url "title "and" title")', "link url title and title"),
    ),
    ids=("space", "line-ending", "bad-angle", "bad-title"),
)
def test_invalid_commonmark_inline_link_suffix_remains_visible(value, expected):
    import compile_memory

    assert compile_memory._normalize_exact_key(value) == expected


@pytest.mark.parametrize("comment", ("<!-->", "<!--->"))
def test_bogus_empty_html_comment_does_not_hide_following_exact_key_text(comment):
    import compile_memory

    assert compile_memory._normalize_exact_key(f"Fail {comment} Closed") == "fail closed"


def test_compile_exact_key_handles_deep_nested_markdown_without_recursion():
    import compile_memory

    value = "Fail Closed"
    for depth in range(64):
        image_marker = "!" if depth % 2 else ""
        value = f"{image_marker}[{value}](destination-{depth}.png)"

    assert compile_memory._normalize_exact_key(value) == "fail closed"


def test_compile_exact_key_keeps_unresolved_reference_link_labels_visible():
    import compile_memory

    assert compile_memory._normalize_exact_key(
        "[Fail ][first][Closed][second]"
    ) == compile_memory._normalize_exact_key("Fail first Closed second")


def test_compile_exact_key_hides_only_resolved_reference_link_labels():
    import compile_memory

    assert compile_memory._normalize_exact_key(
        "[Fail ][first][Closed][second]\n\n[first]: /one\n[second]: /two\n"
    ) == compile_memory._normalize_exact_key("Fail Closed")


@pytest.mark.parametrize(
    ("value", "rendered_words"),
    (
        (
            "`[[policy|Fail Closed Admission]]`",
            "policy Fail Closed Admission",
        ),
        (
            "`` [Fail](Closed) <i>Admission</i> <!-- literal --> ``",
            "Fail Closed i Admission i literal",
        ),
    ),
)
def test_compile_exact_key_keeps_code_span_markup_literal(
    value,
    rendered_words,
):
    import compile_memory

    assert compile_memory._normalize_exact_key(
        value
    ) == compile_memory._normalize_exact_key(rendered_words)


def test_compile_exact_key_handles_unclosed_code_span_without_hiding_markup():
    import compile_memory

    assert compile_memory._normalize_exact_key(
        "`[[policy|Fail Closed Admission]]"
    ) == compile_memory._normalize_exact_key("Fail Closed Admission")


@pytest.mark.parametrize(
    "value",
    (
        "Fail&nbsp;Closed",
        "Fail&#32;Closed",
        "Fail&#x20;Closed",
        "Fail&#X20;Closed",
    ),
)
def test_compile_exact_key_decodes_html_character_references(value):
    import compile_memory

    assert compile_memory._normalize_exact_key(
        value
    ) == compile_memory._normalize_exact_key("Fail Closed")


@pytest.mark.parametrize(
    ("value", "literal_words"),
    (
        ("Fail&nbsp", "Fail nbsp"),
        ("Fail&#32", "Fail 32"),
    ),
)
def test_compile_exact_key_keeps_semicolonless_entity_text_literal(
    value,
    literal_words,
):
    import compile_memory

    assert compile_memory._normalize_exact_key(
        value
    ) == compile_memory._normalize_exact_key(literal_words)


def test_compile_exact_key_keeps_unknown_named_reference_literal():
    import compile_memory

    assert compile_memory._normalize_exact_key(
        "Fail&notit;Closed"
    ) == compile_memory._normalize_exact_key("Fail notit Closed")


@pytest.mark.parametrize(
    ("value", "literal_words"),
    (
        (r"Fail\&nbsp;Closed", "Fail nbsp Closed"),
        (r"Fail\&#32;Closed", "Fail 32 Closed"),
    ),
)
def test_compile_exact_key_keeps_escaped_entities_literal(
    value,
    literal_words,
):
    import compile_memory

    key = compile_memory._normalize_exact_key(value)

    assert key == compile_memory._normalize_exact_key(literal_words)
    assert key != compile_memory._normalize_exact_key("Fail Closed")


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("`Fail&nbsp;Closed`", "fail nbsp closed"),
        ("`Fail&#32;Closed`", "fail 32 closed"),
    ),
)
def test_compile_exact_key_keeps_code_span_entities_literal(value, expected):
    import compile_memory

    assert compile_memory._normalize_exact_key(value) == expected


# ---------------------------------------------------------------------------
# Backward compat: existing test_compile_failure.py still passes
# ---------------------------------------------------------------------------


def test_failed_compile_does_not_mark_hash_still_holds():
    """_compile_succeeded must reject sentinel-style failure strings.

    This is a focused smoke test — the full end-to-end version lives in
    test_compile_failure.py. Here we verify the gating function itself.
    """
    import compile_memory  # noqa: WPS433

    # Simulate the kind of output run_compile produces on failure.
    failure_output = "(compile failed: RuntimeError: phase0-regression-check)"
    assert compile_memory._compile_succeeded(failure_output) is False
    assert compile_memory._compile_succeeded("") is False
    assert compile_memory._compile_succeeded(None) is False
    # And a success marker must still be accepted.
    assert compile_memory._compile_succeeded("COMPILE_DONE: 1 page(s) touched: x") is True


def test_sdk_request_contains_prompt_and_source_hash(tmp_path, monkeypatch):
    import compile_memory

    daily = tmp_path / "knowledge" / "daily" / "2026-07-18.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("# Daily\n\ndurable fact", encoding="utf-8")
    monkeypatch.setattr(compile_memory, "ROOT", tmp_path)
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", tmp_path / "knowledge" / "notes")
    monkeypatch.setattr(compile_memory, "AGENTS", tmp_path / "AGENTS.md")
    monkeypatch.setattr(compile_memory, "LOG", tmp_path / "knowledge" / "log.md")

    request = compile_memory.build_compile_request([daily])

    assert "DAILY LOGS TO COMPILE" in request["prompt"]
    assert request["max_tokens"] == 4000
    assert request["dailies"][0]["path"] == "knowledge/daily/2026-07-18.md"
    assert len(request["dailies"][0]["sha256"]) == 64


def test_sdk_request_rejects_changed_daily(tmp_path, monkeypatch):
    import compile_memory

    daily = tmp_path / "knowledge" / "daily" / "2026-07-18.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("before", encoding="utf-8")
    monkeypatch.setattr(compile_memory, "ROOT", tmp_path)
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", tmp_path / "knowledge" / "notes")
    monkeypatch.setattr(compile_memory, "AGENTS", tmp_path / "AGENTS.md")
    monkeypatch.setattr(compile_memory, "LOG", tmp_path / "knowledge" / "log.md")
    request = compile_memory.build_compile_request([daily])
    daily.write_text("after", encoding="utf-8")

    assert compile_memory.validate_sdk_request(request) is False
