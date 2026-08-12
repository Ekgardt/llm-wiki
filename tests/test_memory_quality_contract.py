from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

COMPLETION_MARKER = "<!-- llm-wiki-record-complete -->"


def _operational_record(
    root: Path,
    body: str,
    *,
    tier: str = "major",
    slug: str = "quality-project",
    session: str = "session-quality",
    completed: bool = True,
) -> str:
    marker = f"\n{COMPLETION_MARKER}" if completed else ""
    return (
        f"## [12:00:00] opencode-idle | {session}\n"
        "- Trigger: `opencode-idle`\n"
        f"- Project slug: `{slug}`\n"
        f"- Project root JSON: {json.dumps(str(root.resolve()))}\n"
        f"- Tier: `{tier}`\n"
        f"- Source session: `{session}`\n\n"
        f"{body}{marker}"
    )


@pytest.mark.parametrize(
    ("case", "build_text"),
    (
        (
            "unknown-2026-07-31-record",
            lambda root: _operational_record(
                root,
                "**Gotchas / debugging**\n- Unknown capture provenance is not durable.",
                tier="minor",
                slug="unknown",
                session="unknown",
            ),
        ),
        (
            "missing-completion-marker",
            lambda root: _operational_record(
                root,
                "**Lessons / patterns**\n- A complete record has an atomic boundary.",
                completed=False,
            ),
        ),
        (
            "minor-with-major-body",
            lambda root: _operational_record(
                root,
                "**Lessons / patterns**\n- Minor records cannot claim major lessons.",
                tier="minor",
            ),
        ),
        (
            "major-without-major-body",
            lambda root: _operational_record(
                root,
                "**Commands / snippets**\n- uv run pytest -q",
            ),
        ),
        (
            "section-without-bullet",
            lambda root: _operational_record(
                root,
                "**Lessons / patterns**\nStatus-only prose is not a durable bullet.",
            ),
        ),
        (
            "flush-ok",
            lambda root: _operational_record(
                root,
                "Status: tests passed.",
                tier="ok",
            ),
        ),
        (
            "duplicate-tier-metadata",
            lambda root: _operational_record(
                root,
                "**Lessons / patterns**\n- Duplicate metadata is malformed.",
            ).replace("- Tier: `major`\n", "- Tier: `major`\n- Tier: `major`\n"),
        ),
        (
            "noncanonical-project-root",
            lambda root: _operational_record(
                root,
                "**Lessons / patterns**\n- Lexical traversal is not canonical.",
            ).replace(
                json.dumps(str(root.resolve())),
                json.dumps(str(root.resolve() / "child" / "..")),
            ),
        ),
        (
            "compact-prompt",
            lambda _root: (
                "- `[12:00:00] prompt | session-quality | quality-project` "
                "A compact prompt is context-only."
            ),
        ),
        (
            "unscoped-heading",
            lambda _root: (
                "## [12:00:00] session-end | session-quality\n"
                "**Lessons / patterns**\n- Unscoped material cannot compile.\n"
                f"{COMPLETION_MARKER}"
            ),
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_operational_compile_admission_rejects_incomplete_or_unscoped_records(
    tmp_path,
    case,
    build_text,
):
    import compile_memory

    text = build_text(tmp_path / case)

    assert compile_memory.extract_meaningful_blocks(text) == []


def test_operational_compile_admission_keeps_only_durable_project_sections(tmp_path):
    import compile_memory
    import session_start_context

    root = (tmp_path / "quality-project").resolve()
    status = "Status: implementation complete and seven files changed."
    trailing_status = "Status: the final audit also passed."
    lesson = "Validate the cited source section before accepting model counters."
    text = _operational_record(
        root,
        f"{status}\n\n**Lessons / patterns**\n- {lesson}\n{trailing_status}",
    )

    records = session_start_context.parse_daily_records(text)
    rendered = compile_memory.extract_meaningful_blocks(text)

    assert len(records) == 1
    assert len(rendered) == 1
    assert records[0].slug == "quality-project"
    assert records[0].project_root == str(root)
    assert "Project slug: `quality-project`" in rendered[0]
    assert f"Project root JSON: {json.dumps(str(root))}" in rendered[0]
    assert lesson in rendered[0]
    assert status not in rendered[0]
    assert trailing_status not in rendered[0]


@pytest.mark.parametrize(
    ("heading", "bullet", "tier"),
    (
        (
            "Lessons / patterns",
            "Status: implementation complete; twelve files changed.",
            "major",
        ),
        (
            "Lessons / patterns",
            "Test count: 1966 passed, 40 skipped.",
            "major",
        ),
        (
            "Decisions made",
            "Audit verdict: all checks passed with no findings.",
            "major",
        ),
        (
            "Gotchas / debugging",
            "Review findings: two important issues and one minor issue.",
            "minor",
        ),
        (
            "Lessons / patterns",
            "Changed files: scripts/compile_memory.py and tests/test_compile.py.",
            "major",
        ),
        (
            "Commands / snippets",
            "Path summary: scripts/compile_memory.py line 120.",
            "minor",
        ),
    ),
)
def test_semantic_admission_rejects_disguised_operational_bullets(
    tmp_path,
    heading,
    bullet,
    tier,
):
    import compile_memory

    text = _operational_record(
        tmp_path / heading.replace("/", "-").replace(" ", "-"),
        f"**{heading}**\n- {bullet}",
        tier=tier,
    )

    assert compile_memory.extract_meaningful_blocks(text) == []


@pytest.mark.parametrize(
    "bullet",
    (
        "**Status:** implementation complete; always retain the final summary.",
        "[Status:](https://example.invalid/status) implementation complete; "
        "always retain the final summary.",
        "Status&#58; implementation complete; always retain the final summary.",
        "`Status:` implementation complete; always retain the final summary.",
        "<strong>Status:</strong> implementation complete; always retain the final summary.",
        "<span data-kind=status>Status:</span> implementation complete; "
        "always retain the final summary.",
        "<strong itemscope class='label' aria-hidden=false>Status:</strong> "
        "implementation complete; always retain the final summary.",
        "<a href=/status>Status:</a> implementation complete; "
        "always retain the final summary.",
        "<span data-kind=status/>Status: implementation complete; "
        "always retain the final summary.",
        "> Status: implementation complete; always retain the final summary.",
    ),
    ids=(
        "emphasis",
        "link-label",
        "html-entity",
        "code-span",
        "html-strong",
        "html-span-attributes",
        "html-strong-attributes",
        "html-anchor",
        "html-self-closing-span",
        "blockquote",
    ),
)
def test_semantic_admission_rejects_visible_operational_labels(tmp_path, bullet):
    import compile_memory

    text = _operational_record(
        tmp_path / "visible-operational-label",
        f"**Lessons / patterns**\n- {bullet}",
    )

    assert compile_memory.extract_meaningful_blocks(text) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (
            "<script data-kind=status>Status:</script> retain this literal tag.",
            "Status: retain this literal tag.",
        ),
        (
            "<custom data-kind=status>Status:</custom> retain this literal tag.",
            "Status: retain this literal tag.",
        ),
        (
            "<a href=/status>Status:</a> retain this literal tag.",
            "Status: retain this literal tag.",
        ),
        (
            "<span data-kind=status/>Status: retain this self-closing tag.",
            "Status: retain this self-closing tag.",
        ),
    ),
)
def test_visible_policy_text_removes_bounded_arbitrary_html_tag_lexemes(
    value,
    expected,
):
    import session_start_context

    assert session_start_context._visible_policy_text(value) == expected


@pytest.mark.parametrize(
    "value",
    (
        "Use x < y and y > z because ordering must remain explicit.",
        r"Use \<a href=/status\> literally because escaped markup is source text.",
        "Use `<a href=/status>Status:</a>` literally because code examples are evidence.",
        "Use `<span data-kind=status/>` literally because code examples are evidence.",
        "Use <a href='unterminated>Status:</a because malformed markup is source text.",
    ),
    ids=("comparison", "escaped", "paired-code", "self-closing-code", "malformed"),
)
def test_visible_policy_text_preserves_non_html_and_code_examples(value):
    import session_start_context

    assert session_start_context._visible_policy_text(value) == value


def test_visible_policy_text_keeps_code_after_removed_tag_backtick_decoy():
    import session_start_context

    value = (
        "<span title='`attribute-decoy`'></span>"
        "Use `<a href=/status>Status:</a>` literally because code remains literal."
    )

    assert session_start_context._visible_policy_text(value) == (
        "Use `<a href=/status>Status:</a>` literally because code remains literal."
    )


@pytest.mark.parametrize(
    "value",
    (
        "<custom "
        + " ".join(f"data-field-{index}='{index}'" for index in range(33))
        + ">Status:</custom> retain the visible label.",
        "<custom data-value='"
        + "x" * 1100
        + "'>Status:</custom> retain the long valid tag.",
    ),
    ids=("thirty-three-attributes", "over-1024-characters"),
)
def test_visible_policy_text_removes_valid_tags_without_arbitrary_lexeme_caps(value):
    import session_start_context

    assert session_start_context._visible_policy_text(value) == value[value.index(">") + 1 :].replace(
        "</custom>",
        "",
    )


def test_visible_policy_text_preserves_long_unclosed_quoted_tag_remainder():
    import session_start_context

    value = (
        "<custom data-value='"
        + "x" * 1100
        + ">Status:</custom> remains literal because the quote never closes."
    )

    assert session_start_context._visible_policy_text(value) == value


def test_visible_policy_tag_scanner_character_visits_scale_linearly():
    import session_start_context

    attributes = " ".join(
        f"data-field-{index}='{index}'" for index in range(40)
    )
    unit = f"<custom {attributes} data-long='{'x' * 1100}'>Status:</custom> "
    small = unit * 2 + "Use x < y and y > z because comparisons remain visible."
    large = small * 2
    small_stats = {}
    large_stats = {}

    small_visible = session_start_context._visible_policy_text(
        small,
        scan_stats=small_stats,
    )
    large_visible = session_start_context._visible_policy_text(
        large,
        scan_stats=large_stats,
    )

    assert small_visible == "Status: " * 2 + "Use x < y and y > z because comparisons remain visible."
    assert large_visible == small_visible * 2
    assert small_stats["policy_tag_input_characters"] == len(small)
    assert large_stats["policy_tag_input_characters"] == len(large)
    assert small_stats["policy_tag_character_visits"] <= len(small) * 3
    assert large_stats["policy_tag_character_visits"] <= len(large) * 3
    assert large_stats["policy_tag_character_visits"] <= (
        small_stats["policy_tag_character_visits"] * 2 + 8
    )


@pytest.mark.parametrize(
    "bullet",
    (
        "`git status --short` must remain available because repository checks need exact output.",
        "<strong>Always</strong> validate persisted fields because compatibility is structural.",
        "> Changed schemas require versioned readers because persisted hashes must remain verifiable.",
        "Use x < y and y > z because ordering must remain explicit.",
        "Use `<a href=/status>Status:</a>` literally because code examples are evidence.",
    ),
    ids=("code-command", "html-rule", "blockquote-rule", "comparison", "html-code"),
)
def test_semantic_admission_keeps_formatted_code_and_rules(tmp_path, bullet):
    import compile_memory

    text = _operational_record(
        tmp_path / "formatted-durable-rule",
        f"**Lessons / patterns**\n- {bullet}",
    )

    [rendered] = compile_memory.extract_meaningful_blocks(text)
    assert bullet in rendered


def test_fenced_html_tag_examples_do_not_create_operational_policy_labels(tmp_path):
    import compile_memory

    visible = "Use x < y because comparison syntax must remain visible."
    text = _operational_record(
        tmp_path / "fenced-html-policy",
        "**Lessons / patterns**\n"
        f"- {visible}\n\n"
        "```html\n"
        "<a href=/status>Status:</a> implementation complete.\n"
        "<span data-kind=status/>Status: implementation complete.\n"
        "```",
    )

    [rendered] = compile_memory.extract_meaningful_blocks(text)
    assert visible in rendered
    assert "implementation complete" not in rendered


@pytest.mark.parametrize(
    ("report", "bullet"),
    (
        (
            "completion",
            "The implementation is now complete and should be considered finished.",
        ),
        (
            "progress",
            "Work is halfway through and should resume after the next session.",
        ),
        (
            "test-count",
            "The suite reports 1966 successes and should retain 40 omissions.",
        ),
        (
            "review",
            "Inspection found nothing actionable, so always close this review.",
        ),
        (
            "changed-files",
            "The patch spans two modules and should now be ready for handoff.",
        ),
        (
            "finished",
            "Finished the migration and always preserve the final handoff report.",
        ),
        (
            "completed",
            "Completed the validator work and always retain the closing summary.",
        ),
        (
            "migrated",
            "Migrated the journal schema and always retain the generated artifacts.",
        ),
        (
            "changed",
            "Changed the manifest reader because the compatibility pass required it.",
        ),
        (
            "we-finished",
            "We finished the parser repair and should preserve the final status.",
        ),
        (
            "we-changed-path",
            "We changed scripts/session_start_context.py because the parser review required it.",
        ),
        (
            "updated",
            "Updated the compiler and always preserve the final status report.",
        ),
        (
            "added",
            "Added migration coverage because the review requested another probe.",
        ),
        (
            "removed",
            "Removed the legacy helper because the cleanup pass was finished.",
        ),
        (
            "test-suite-green",
            "The test suite is green, so always publish the final count.",
        ),
        (
            "test-suite-green-terse",
            "Test suite green, so always publish the final count.",
        ),
        (
            "migration-completed-terse",
            "Migration completed, so always publish the final handoff report.",
        ),
        (
            "file-path-because",
            "scripts/compile_memory.py changed because the persisted format moved.",
        ),
    ),
)
def test_semantic_admission_rejects_operational_report_paraphrases(
    tmp_path,
    report,
    bullet,
):
    import compile_memory

    text = _operational_record(
        tmp_path / report,
        f"**Lessons / patterns**\n- {bullet}",
    )

    assert compile_memory.extract_meaningful_blocks(text) == []


@pytest.mark.parametrize(
    ("heading", "bullet", "tier"),
    (
        (
            "Decisions made",
            "Use versioned readers because persisted hashes must remain verifiable.",
            "major",
        ),
        (
            "Lessons / patterns",
            "Changed schemas require versioned readers because persisted hashes must remain verifiable.",
            "major",
        ),
        (
            "Commands / snippets",
            "`uv run --no-sync pytest tests/test_memory_quality_contract.py -q`",
            "minor",
        ),
        (
            "Gotchas / debugging",
            "A stale journal causes replay failure; retire it before preparing again.",
            "minor",
        ),
        (
            "Open questions",
            "How should an interrupted migration retain a verified durable effect?",
            "minor",
        ),
    ),
)
def test_semantic_admission_accepts_section_specific_durable_signals(
    tmp_path,
    heading,
    bullet,
    tier,
):
    import compile_memory

    text = _operational_record(
        tmp_path / heading.replace("/", "-").replace(" ", "-"),
        f"**{heading}**\n- {bullet}",
        tier=tier,
    )

    [rendered] = compile_memory.extract_meaningful_blocks(text)
    assert bullet in rendered


def test_operational_detection_has_bounded_work_for_repeated_audit_tokens():
    import session_start_context

    repetitions = 20_000
    bullet = "audit " * repetitions + "pending"
    stats: dict[str, int] = {}

    assert not session_start_context._operational_bullet_summary(
        bullet,
        scan_stats=stats,
    )
    assert stats["operational_input_characters"] == len(bullet)
    assert stats["operational_character_visits"] <= len(bullet) * 2
    assert stats["operational_token_visits"] <= (repetitions + 1) * 2
    assert stats["operational_prefix_checks"] <= 16
    assert stats["operational_substring_checks"] <= 1


@pytest.mark.parametrize(
    "hidden",
    (
        "```markdown\n**Lessons / patterns**\n- Always trust hidden code.\n```",
        "<!--\n**Lessons / patterns**\n- Always trust hidden comments.\n-->",
        "<script>\n**Lessons / patterns**\n- Always trust hidden scripts.\n</script>",
        "<div>\n**Lessons / patterns**\n- Always trust hidden raw blocks.\n\n",
    ),
    ids=("fence", "comment", "script", "raw-html-block"),
)
def test_markdown_hidden_sections_do_not_become_compile_evidence(tmp_path, hidden):
    import compile_memory

    visible = "When markup hides a section, exclude it before evidence indexing."
    text = _operational_record(
        tmp_path / "hidden-markdown",
        f"{hidden}\n**Lessons / patterns**\n- {visible}",
    )

    [rendered] = compile_memory.extract_meaningful_blocks(text)
    assert visible in rendered
    assert "Always trust hidden" not in rendered


def test_unmatched_comment_cannot_bypass_fence_or_raw_html_admission(tmp_path):
    import compile_memory
    import session_start_context

    body = [
        "<!-- unmatched opener",
        "```markdown",
        "**Lessons / patterns**",
        "- Always reject evidence hidden in a fenced block.",
        "```",
        "<script>",
        "**Lessons / patterns**",
        "- Always reject evidence hidden in a script block.",
        "</script>",
    ]
    text = _operational_record(tmp_path / "unmatched-hidden", "\n".join(body))

    assert compile_memory.extract_meaningful_blocks(text) == []
    assert session_start_context._visible_durable_markdown_lines(body) == [
        "<!-- unmatched opener"
    ]


@pytest.mark.parametrize(
    "body",
    (
        (
            "```markdown\n"
            "```<!-- -->\n"
            "**Lessons / patterns**\n"
            "- Always reject evidence after a commented fence closer.\n"
            "```"
        ),
        (
            "<script>\n"
            "</scr<!-- -->ipt>\n"
            "**Lessons / patterns**\n"
            "- Always reject evidence after a synthesized raw closer.\n"
            "</script>"
        ),
    ),
    ids=("commented-fence-closer", "commented-raw-html-closer"),
)
def test_comment_removal_cannot_synthesize_block_closers(tmp_path, body):
    import compile_memory

    text = _operational_record(tmp_path / "commented-block-closer", body)

    assert compile_memory.extract_meaningful_blocks(text) == []


@pytest.mark.parametrize(
    ("body", "visible", "hidden"),
    (
        (
            "**Lessons / patterns**\n"
            "- Always preserve visible text <!-- remove inline --> when scanning.",
            "Always preserve visible text when scanning.",
            "remove inline",
        ),
        (
            "**Lessons / patterns**\n"
            r"- Always preserve escaped \<!-- visible comment syntax --> literally.",
            r"Always preserve escaped \<!-- visible comment syntax --> literally.",
            None,
        ),
        (
            "**Lessons / patterns**\n"
            "- Always preserve text before <!-- remove\n"
            "  hidden continuation\n"
            "  --> when a multiline inline comment closes.",
            "Always preserve text before when a multiline inline comment closes.",
            "hidden continuation",
        ),
        (
            "<!-- remove this full-line\n"
            "and this line -->\n"
            "**Lessons / patterns**\n"
            "- Always retain the visible section after a block comment.",
            "Always retain the visible section after a block comment.",
            "remove this full-line",
        ),
        (
            "**Lessons / patterns**\n"
            "- Always preserve `<!-- literal -->` inside code spans when scanning.",
            "Always preserve `<!-- literal -->` inside code spans when scanning.",
            None,
        ),
        (
            "**Lessons / patterns**\n"
            "- Always preserve ``<!-- ` literal -->`` inside multi-backtick code spans.",
            "Always preserve ``<!-- ` literal -->`` inside multi-backtick code spans.",
            None,
        ),
        (
            "**Lessons / patterns**\n"
            "- Always preserve `<!--` and `-->` when delimiters are split by code spans.",
            "Always preserve `<!--` and `-->` when delimiters are split by code spans.",
            None,
        ),
        (
            "**Lessons / patterns**\n"
            "- Always preserve `<!-- literal -->` <!-- hide real --> when scanning.",
            "Always preserve `<!-- literal -->` when scanning.",
            "hide real",
        ),
        (
            "**Lessons / patterns**\n"
            "- Always preserve an unmatched <!-- opener as visible text.",
            "Always preserve an unmatched <!-- opener as visible text.",
            None,
        ),
        (
            "<!-- unmatched opener\n"
            "**Lessons / patterns**\n"
            "- Always preserve the visible record after an unmatched opener.",
            "Always preserve the visible record after an unmatched opener.",
            None,
        ),
        (
            "**Lessons / patterns**\n"
            "- Always hide <!-- hide closed --> but preserve unmatched <!-- opener text.",
            "Always hide but preserve unmatched <!-- opener text.",
            "hide closed",
        ),
    ),
    ids=(
        "inline",
        "escaped",
        "multiline-inline",
        "multiline-block",
        "single-code-span",
        "multi-backtick-code-span",
        "split-code-delimiters",
        "code-span-and-real-comment",
        "unclosed-inline",
        "unclosed-block",
        "closed-then-unclosed",
    ),
)
def test_markdown_comments_hide_only_actual_comment_content(
    tmp_path,
    body,
    visible,
    hidden,
):
    import compile_memory

    text = _operational_record(tmp_path / "comment-visibility", body)

    [rendered] = compile_memory.extract_meaningful_blocks(text)
    assert visible in " ".join(rendered.split())
    if hidden is not None:
        assert hidden not in rendered


def test_markdown_comment_scanner_has_linear_visit_bound_for_inline_decoys():
    import session_start_context

    fragment = r"\<!-- escaped --> `<!-- code -->` literal "
    line = f"prefix {fragment * 2_000}<!-- hidden --> suffix"
    stats: dict[str, int] = {}

    visible = session_start_context._visible_durable_markdown_lines(
        [line],
        scan_stats=stats,
    )

    assert stats["input_characters"] == len(line)
    assert stats["character_visits"] <= len(line) * 5 + 128
    assert visible == [line.replace("<!-- hidden -->", "")]


def test_markdown_comment_scanner_has_linear_visit_bound_for_multiline_comment():
    import session_start_context

    body = ["prefix <!--", *(["x" * 1_024] * 256), "--> suffix"]
    stats: dict[str, int] = {}

    visible = session_start_context._visible_durable_markdown_lines(
        body,
        scan_stats=stats,
    )

    input_characters = sum(len(line) for line in body)
    assert stats["input_characters"] == input_characters
    assert stats["character_visits"] <= input_characters * 5 + 128
    assert visible == ["prefix suffix"]


def _grounding_page(
    path: Path,
    *,
    title: str = "Grounding Source",
    status: str | None = None,
    evidence: str = "Atomic create-only publication prevents replacement races.",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    status_line = f"status: {status}\n" if status is not None else ""
    path.write_text(
        "---\n"
        "type: pattern\n"
        f'title: "{title}"\n'
        f"{status_line}"
        "confidence: high\n"
        "source_authority: user\n"
        "---\n\n"
        f"# {title}\n\n"
        f"One-sentence summary: {evidence}\n\n"
        "## Evidence\n"
        f"{evidence}\n",
        encoding="utf-8",
    )
    return path


def _grounded_provider_answer(path: str, quote: str, *, answer: str = "Use create-only publication.") -> str:
    citation = json.dumps(
        {"path": path, "quote": quote},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "**Answer:**\n"
        f"{answer}\n\n"
        "**Sources:**\n"
        f"- {citation}\n\n"
        "**Confidence:** high - exact source evidence\n"
    )


def _configure_query_memory(monkeypatch, root: Path):
    import query_memory

    memory = root / "knowledge"
    notes = memory / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(query_memory, "ROOT", root)
    monkeypatch.setattr(query_memory, "MEMORY", memory)
    monkeypatch.setattr(query_memory, "INDEX", memory / "index.md")
    monkeypatch.setattr(query_memory, "LOG", memory / "log.md")
    monkeypatch.setattr(query_memory, "QA_DIR", notes)
    return query_memory, notes


def test_answer_prompt_contains_only_bounded_canonical_source_bodies(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    evidence = "Exact bounded evidence appears in the provider prompt."
    source = _grounding_page(
        notes / "source.md",
        evidence=evidence + " " + "x" * 300 + " TAIL-MARKER",
    )
    shadow = _grounding_page(
        notes / "patterns" / "source.md",
        evidence="Shadow content must never reach the provider.",
    )
    relative = source.relative_to(tmp_path).as_posix()
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [
            {"path": relative, "title": "Source", "summary": "", "score": 1.0}
        ],
    )
    monkeypatch.setattr(query_memory, "MAX_ANSWER_SOURCE_CHARS", 180)

    def fake_call(prompt, **_kwargs):
        captured["prompt"] = prompt
        return _grounded_provider_answer(relative, evidence)

    monkeypatch.setattr(llm_client, "call_llm", fake_call)

    answer = query_memory.answer("How is publication protected?")

    assert "Use create-only publication" in answer
    assert relative in captured["prompt"]
    assert evidence in captured["prompt"]
    assert "TAIL-MARKER" not in captured["prompt"]
    assert shadow.relative_to(tmp_path).as_posix() not in captured["prompt"]
    assert "Shadow content" not in captured["prompt"]


def test_provider_sources_redact_body_exclude_changed_path_and_keep_raw_mapping(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    safe_quote = "Nonsecret evidence remains usable after source sanitization."
    safe = _grounding_page(
        notes / "safe-source.md",
        evidence=(
            "password=source-only-password\n\n"
            f"{safe_quote}"
        ),
    )
    secret_path = _grounding_page(
        notes / "api_key=source-path-secret.md",
        title="Secret Path Source",
        evidence="A source whose exact path changes cannot be cited.",
    )
    safe_relative = safe.relative_to(tmp_path).as_posix()
    secret_relative = secret_path.relative_to(tmp_path).as_posix()
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [
            {"path": secret_relative},
            {"path": safe_relative},
        ],
    )

    def fake_call(prompt, **_kwargs):
        captured["prompt"] = prompt
        return _grounded_provider_answer(safe_relative, safe_quote)

    monkeypatch.setattr(llm_client, "call_llm", fake_call)

    generated = query_memory.answer_with_sources("What evidence remains safe?")

    assert "source-only-password" not in captured["prompt"]
    assert "source-path-secret" not in captured["prompt"]
    assert secret_relative not in captured["prompt"]
    assert "password=[REDACTED]" in captured["prompt"]
    assert [source.note.relative_path for source in generated.sources] == [safe_relative]
    [source] = generated.sources
    assert "source-only-password" in source.raw_exposed_content
    assert "source-only-password" not in source.exposed_content
    assert safe_quote in source.raw_exposed_content
    assert safe_quote in source.exposed_content

    out = query_memory.file_back(
        "What evidence remains safe?",
        generated.text,
        sources=generated.sources,
    )

    assert safe_quote in out.read_text(encoding="utf-8")


def test_redaction_marker_cannot_be_filed_as_source_evidence(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    source = _grounding_page(
        notes / "safe-source.md",
        evidence=secret,
    )
    relative = source.relative_to(tmp_path).as_posix()
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [{"path": relative}],
    )
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *_args, **_kwargs: _grounded_provider_answer(
            relative,
            "[REDACTED_API_KEY]",
        ),
    )

    generated = query_memory.answer_with_sources("Can a marker prove the answer?")

    assert secret not in generated.sources[0].exposed_content
    with pytest.raises(ValueError, match="exact quote"):
        query_memory.file_back(
            "Can a marker prove the answer?",
            generated.text,
            sources=generated.sources,
        )


def test_redaction_marker_visible_from_secret_cannot_match_literal_beyond_raw_bound(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    marker = "[REDACTED_API_KEY]"
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    source = notes / "source.md"
    source.write_text(
        "---\n"
        "type: pattern\n"
        "confidence: high\n"
        "source_authority: user\n"
        "---\n\n"
        "# Bounded Raw Source\n\n"
        f"{secret}\n"
        + "x" * 300
        + f"\n{marker}\n",
        encoding="utf-8",
    )
    relative = source.relative_to(tmp_path).as_posix()
    monkeypatch.setattr(query_memory, "MAX_ANSWER_SOURCE_CHARS", 180)
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [{"path": relative}],
    )
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *_args, **_kwargs: _grounded_provider_answer(relative, marker),
    )

    generated = query_memory.answer_with_sources("Can synthetic redaction prove this?")
    [provider_source] = generated.sources

    assert marker in provider_source.exposed_content
    assert marker in source.read_text(encoding="utf-8")
    assert marker not in provider_source.raw_exposed_content
    assert len(provider_source.raw_exposed_content) <= 180
    with pytest.raises(ValueError, match="exact quote"):
        query_memory.file_back(
            "Can synthetic redaction prove this?",
            generated.text,
            sources=generated.sources,
        )


def test_empty_ranked_source_does_not_suppress_later_valid_source(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    empty_sources = []
    for index in range(query_memory.MAX_ANSWER_SOURCE_PAGES):
        empty = notes / f"empty-{index}.md"
        empty.write_text(
            "---\ntype: pattern\nconfidence: high\nsource_authority: user\n---\n",
            encoding="utf-8",
        )
        empty_sources.append(empty)
    evidence = "A later nonempty source remains available for grounding."
    valid = _grounding_page(
        notes / "valid.md",
        title="Valid Later Source",
        evidence=evidence,
    )
    valid_relative = valid.relative_to(tmp_path).as_posix()
    ranked = [
        {"path": source.relative_to(tmp_path).as_posix()}
        for source in empty_sources
    ] + [{"path": valid_relative}]
    captured: dict[str, object] = {}

    def fake_search(*_args, **kwargs):
        captured["limit"] = kwargs["limit"]
        return ranked

    monkeypatch.setattr(search_memory, "search", fake_search)

    def fake_call(prompt, **_kwargs):
        captured["prompt"] = prompt
        return _grounded_provider_answer(valid_relative, evidence)

    monkeypatch.setattr(llm_client, "call_llm", fake_call)

    generated = query_memory.answer_with_sources("What follows an empty source?")

    assert captured["limit"] > query_memory.MAX_ANSWER_SOURCE_PAGES
    assert valid_relative in captured["prompt"]
    assert [source.note.relative_path for source in generated.sources] == [
        valid_relative
    ]


def test_file_back_rejects_trailing_newlines_not_rendered_in_source_body(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    source = notes / "source.md"
    source.write_bytes(
        b"---\n"
        b"type: pattern\n"
        b"confidence: high\n"
        b"source_authority: user\n"
        b"---\n\n"
        b"# Trailing Source\n\n"
        b"Visible tail evidence.\n\n\n"
    )
    relative = source.relative_to(tmp_path).as_posix()
    tail_quote = "Visible tail evidence.\n\n"
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [
            {"path": relative, "title": "Source", "summary": "", "score": 1.0}
        ],
    )

    def fake_call(prompt, **_kwargs):
        captured["prompt"] = prompt
        return _grounded_provider_answer(relative, tail_quote)

    monkeypatch.setattr(llm_client, "call_llm", fake_call)
    generated = query_memory.answer_with_sources("What was visibly rendered?")
    expected = notes / f"{query_memory.slugify('What was visibly rendered?')}.md"

    with pytest.raises(ValueError, match="exact quote"):
        query_memory.file_back(
            "What was visibly rendered?",
            generated.text,
            sources=generated.sources,
        )

    source_header = f"--- source: {relative} ---\n"
    rendered_body = captured["prompt"].split(source_header, 1)[1].split(
        "\n\n--- question ---",
        1,
    )[0]
    assert generated.sources[0].exposed_content == rendered_body
    assert not expected.exists()


def test_file_back_rejects_quote_outside_bounded_provider_source_body(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    hidden_quote = "UNEXPOSED-TAIL-EVIDENCE"
    source = _grounding_page(
        notes / "source.md",
        evidence="Visible evidence. " + "x" * 300 + hidden_quote,
    )
    relative = source.relative_to(tmp_path).as_posix()
    monkeypatch.setattr(query_memory, "MAX_ANSWER_SOURCE_CHARS", 120)
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [
            {"path": relative, "title": "Source", "summary": "", "score": 1.0}
        ],
    )
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *_args, **_kwargs: _grounded_provider_answer(relative, hidden_quote),
    )
    generated = query_memory.answer_with_sources("What is hidden?")

    with pytest.raises(ValueError, match="exact quote"):
        query_memory.file_back(
            "What is hidden?",
            generated.text,
            sources=generated.sources,
        )


def test_file_back_rejects_synthetic_source_truncation_marker(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import llm_client
    import search_memory

    source = _grounding_page(
        notes / "source.md",
        evidence="Visible evidence. " + "x" * 300,
    )
    relative = source.relative_to(tmp_path).as_posix()
    captured: dict[str, str] = {}
    monkeypatch.setattr(query_memory, "MAX_ANSWER_SOURCE_CHARS", 120)
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *_args, **_kwargs: [
            {"path": relative, "title": "Source", "summary": "", "score": 1.0}
        ],
    )

    def fake_call(prompt, **_kwargs):
        captured["prompt"] = prompt
        return _grounded_provider_answer(
            relative,
            query_memory.SOURCE_TRUNCATION_MARKER,
        )

    monkeypatch.setattr(llm_client, "call_llm", fake_call)
    generated = query_memory.answer_with_sources("What was truncated?")

    assert query_memory.SOURCE_TRUNCATION_MARKER in captured["prompt"]
    with pytest.raises(ValueError, match="exact quote"):
        query_memory.file_back(
            "What was truncated?",
            generated.text,
            sources=generated.sources,
        )


def test_grounded_file_back_records_validated_source_and_verbatim_evidence(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "A target path is never replaced after it already exists."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    relative = source.relative_to(tmp_path).as_posix()
    response = _grounded_provider_answer(relative, evidence)

    out = query_memory.file_back("How do publications avoid overwrite?", response)

    filed = out.read_text(encoding="utf-8")
    assert out.parent == notes
    assert f"Source: `{relative}`" in filed
    assert f"Source SHA-256: `{hashlib.sha256(source.read_bytes()).hexdigest()}`" in filed
    assert json.dumps(evidence, ensure_ascii=False) in filed
    assert "## Evidence" in filed
    assert "Use create-only publication." in filed


def test_file_back_holds_publication_lock_during_validation_and_write(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)

    evidence = "Fresh validation and publication share one lock boundary."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        evidence,
    )
    real_select = query_memory.select_active_notes
    real_atomic_write = query_memory.atomic_write
    events: list[str] = []
    lock_active = False

    @contextmanager
    def fake_publication_lock(*_args, **_kwargs):
        nonlocal lock_active
        events.append("enter")
        lock_active = True
        try:
            yield
        finally:
            lock_active = False
            events.append("exit")

    def guarded_select(*args, **kwargs):
        assert lock_active
        events.append("validate")
        return real_select(*args, **kwargs)

    def guarded_write(*args, **kwargs):
        assert lock_active
        events.append("write")
        return real_atomic_write(*args, **kwargs)

    monkeypatch.setattr(
        query_memory,
        "knowledge_publication_lock",
        fake_publication_lock,
        raising=False,
    )
    monkeypatch.setattr(query_memory, "select_active_notes", guarded_select)
    monkeypatch.setattr(query_memory, "atomic_write", guarded_write)

    query_memory.file_back("Where is the publication boundary?", response)

    assert events == ["enter", "validate", "validate", "write", "exit"]


@pytest.mark.parametrize("duplicate", ("path", "quote"))
def test_file_back_requires_independently_unique_citation_paths_and_quotes(
    tmp_path, monkeypatch, duplicate
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    first_quote = "Every citation quote has one evidence identity."
    second_quote = "Every citation path also appears only once."
    first = _grounding_page(
        notes / "first.md",
        title="First Source",
        evidence=f"{first_quote} {second_quote}",
    )
    second = _grounding_page(
        notes / "second.md",
        title="Second Source",
        evidence=first_quote,
    )
    first_path = first.relative_to(tmp_path).as_posix()
    second_path = second.relative_to(tmp_path).as_posix()
    citations = (
        ((first_path, first_quote), (first_path, second_quote))
        if duplicate == "path"
        else ((first_path, first_quote), (second_path, first_quote))
    )
    source_lines = "\n".join(
        "- "
        + json.dumps(
            {"path": path, "quote": quote},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for path, quote in citations
    )
    response = (
        "**Answer:**\nUse unique evidence.\n\n"
        f"**Sources:**\n{source_lines}\n\n"
        "**Confidence:** high - exact source evidence\n"
    )
    question = f"Can a duplicate citation {duplicate} be filed?"
    expected = notes / f"{query_memory.slugify(question)}.md"

    with pytest.raises(ValueError, match="duplicated"):
        query_memory.file_back(question, response)

    assert not expected.exists()


@pytest.mark.parametrize(
    "quote",
    ("\n", "   ", "\t"),
    ids=("newline", "spaces", "tab"),
)
def test_file_back_rejects_whitespace_only_evidence_quote(
    tmp_path, monkeypatch, quote
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    source = _grounding_page(
        notes / "source.md",
        evidence="Whitespace   source includes a tab\tbetween tokens.",
    )
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        quote,
    )
    question = "Can whitespace alone prove an answer?"
    expected = notes / f"{query_memory.slugify(question)}.md"

    with pytest.raises(ValueError, match="exact quote"):
        query_memory.file_back(question, response)

    assert not expected.exists()


def test_file_back_rejects_outside_vault_source_masquerading_as_canonical(
    tmp_path, monkeypatch
):
    root = tmp_path / "vault"
    query_memory, notes = _configure_query_memory(monkeypatch, root)
    import vault_editorial

    _grounding_page(
        notes / "source.md",
        evidence="Canonical evidence remains inside the vault.",
    )
    outside_evidence = "Outside-vault content must not become cited evidence."
    outside = _grounding_page(
        tmp_path / "outside.md",
        evidence=outside_evidence,
    )
    [canonical] = vault_editorial.select_active_notes(notes, root=root).notes
    masquerading = replace(
        canonical,
        path=outside,
        content=query_memory.read_bounded_note(outside),
    )
    response = _grounded_provider_answer(canonical.relative_path, outside_evidence)
    expected = notes / f"{query_memory.slugify('Can outside evidence be filed?')}.md"

    with pytest.raises(ValueError, match="canonical"):
        query_memory.file_back(
            "Can outside evidence be filed?",
            response,
            sources=(
                query_memory.AnswerSource(
                    note=masquerading,
                    exposed_content=masquerading.content,
                ),
            ),
        )

    assert not expected.exists()


def test_file_back_rejects_fabricated_exposure_for_real_canonical_snapshot(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    _grounding_page(
        notes / "source.md",
        evidence="Only canonical bytes can become evidence.",
    )
    [canonical] = vault_editorial.select_active_notes(notes, root=tmp_path).notes
    fabricated = "Fabricated provider exposure is not canonical evidence."
    response = _grounded_provider_answer(canonical.relative_path, fabricated)
    expected = notes / f"{query_memory.slugify('Can exposure be forged?')}.md"

    with pytest.raises(ValueError, match="provider-visible"):
        query_memory.file_back(
            "Can exposure be forged?",
            response,
            sources=(
                query_memory.AnswerSource(
                    note=canonical,
                    exposed_content=fabricated,
                ),
            ),
        )

    assert not expected.exists()


def test_file_back_recomputes_120_char_provider_exposure_before_tail_quote(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    tail_quote = "UNEXPOSED-120-CHAR-TAIL-EVIDENCE"
    _grounding_page(
        notes / "source.md",
        evidence="Visible prefix. " + "x" * 300 + tail_quote,
    )
    [canonical] = vault_editorial.select_active_notes(notes, root=tmp_path).notes
    monkeypatch.setattr(query_memory, "MAX_ANSWER_SOURCE_CHARS", 120)
    response = _grounded_provider_answer(canonical.relative_path, tail_quote)
    expected = notes / f"{query_memory.slugify('Can hidden tail evidence be filed?')}.md"

    with pytest.raises(ValueError, match="provider-visible"):
        query_memory.file_back(
            "Can hidden tail evidence be filed?",
            response,
            sources=(
                query_memory.AnswerSource(
                    note=canonical,
                    exposed_content=canonical.content,
                ),
            ),
        )

    assert not expected.exists()


def test_file_back_rejects_provider_source_that_became_a_shadow(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    typed_evidence = "The typed page was canonical when the provider saw it."
    _grounding_page(
        notes / "patterns" / "source.md",
        evidence=typed_evidence,
    )
    [typed] = vault_editorial.select_active_notes(notes, root=tmp_path).notes
    provider_source = query_memory.AnswerSource(
        note=typed,
        exposed_content=typed.content,
    )
    _grounding_page(
        notes / "source.md",
        evidence="A new flat page now owns the logical identity.",
    )
    response = _grounded_provider_answer(typed.relative_path, typed_evidence)
    expected = notes / f"{query_memory.slugify('Can a stale winner be filed?')}.md"

    with pytest.raises(ValueError, match="canonical"):
        query_memory.file_back(
            "Can a stale winner be filed?",
            response,
            sources=(provider_source,),
        )

    assert not expected.exists()


def test_file_back_rejects_identical_source_replacement_after_generation(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    evidence = "Publication pins the exact source file seen during generation."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    [snapshot] = vault_editorial.select_active_notes(notes, root=tmp_path).notes
    replacement = notes / "source.tmp"
    replacement.write_bytes(source.read_bytes())
    os.replace(replacement, source)
    response = _grounded_provider_answer(snapshot.relative_path, evidence)
    expected = notes / f"{query_memory.slugify('Can a replaced source be filed?')}.md"

    with pytest.raises(ValueError, match="changed"):
        query_memory.file_back(
            "Can a replaced source be filed?",
            response,
            sources=(
                query_memory.AnswerSource(
                    note=snapshot,
                    exposed_content=snapshot.content,
                ),
            ),
        )

    assert not expected.exists()


def test_file_back_rejects_hardlinked_source_before_publication(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)

    evidence = "A multiply linked source is not a stable publication snapshot."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    os.link(source, tmp_path / "source-alias.md")
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        evidence,
    )
    question = "Can hardlinked evidence be filed?"
    expected = notes / f"{query_memory.slugify(question)}.md"

    with pytest.raises(OSError, match="hard-linked"):
        query_memory.file_back(question, response)

    assert not expected.exists()


@pytest.mark.parametrize(
    "response",
    (
        "**Answer:**\nUnsupported answer.\n\n**Confidence:** low\n",
        "**Answer:**\nUnsupported answer.\n\n**Sources:**\n\n**Confidence:** low\n",
        "**Answer:**\nUnsupported answer.\n\n**Sources:**\n- not-json\n\n**Confidence:** low\n",
        "**Answer:**\nFirst.\n\n**Answer:**\nSecond.\n\n**Sources:**\n- {}\n\n**Confidence:** low\n",
        "**Answer:**\nUnsupported answer.\n\n**Sources:**\n- {}\n\n**Sources:**\n- {}\n\n**Confidence:** low\n",
        "**Answer:**\nUnsupported answer.\n\n**Sources:**\n- {\"path\":\"knowledge/notes/source.md\"}\n\n**Confidence:** low\n",
    ),
    ids=(
        "missing-sources",
        "empty-sources",
        "malformed-source",
        "duplicate-answer",
        "duplicate-sources",
        "missing-quote",
    ),
)
def test_file_back_rejects_malformed_or_ungrounded_contract_before_write(
    tmp_path, monkeypatch, response
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    _grounding_page(notes / "source.md")
    expected = notes / f"{query_memory.slugify('Unsupported answer?')}.md"

    with pytest.raises(ValueError):
        query_memory.file_back("Unsupported answer?", response)

    assert not expected.exists()


def test_strict_citation_json_calls_shared_decoder_with_explicit_bounds(monkeypatch):
    import query_memory

    real_decode = query_memory.memory_state.decode_json_object_strict
    calls: list[dict[str, int]] = []

    def tracking_decode(raw, **kwargs):
        calls.append(kwargs)
        return real_decode(raw, **kwargs)

    monkeypatch.setattr(
        query_memory.memory_state,
        "decode_json_object_strict",
        tracking_decode,
    )

    citation = query_memory._strict_json_object(
        '{"path":"knowledge/notes/source.md","quote":"evidence"}'
    )

    assert citation == {
        "path": "knowledge/notes/source.md",
        "quote": "evidence",
    }
    assert calls == [
        {
            "max_bytes": query_memory.MAX_CITATION_JSON_BYTES,
            "max_chars": query_memory.MAX_CITATION_JSON_CHARS,
            "max_depth": query_memory.MAX_CITATION_JSON_DEPTH,
            "max_members": query_memory.MAX_CITATION_JSON_MEMBERS,
        }
    ]


def test_strict_citation_json_rejects_deeply_nested_value():
    import query_memory

    depth = 64
    citation = '{"nested":' + "[" * depth + "0" + "]" * depth + "}"

    with pytest.raises(ValueError, match="depth limit"):
        query_memory._strict_json_object(citation)


def test_main_normalizes_citation_parser_resource_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import memory_state

    citation = '{"path":"knowledge/notes/source.md","quote":"evidence"}'
    response = (
        "**Answer:**\nNo unbounded parser work.\n\n"
        f"**Sources:**\n- {citation}\n\n"
        "**Confidence:** low - malformed citation\n"
    )
    question = "Can citation parser exhaustion escape controlled failure?"
    target = notes / f"{query_memory.slugify(question)}.md"

    def fail_parse(*_args, **_kwargs):
        raise MemoryError("injected citation parser exhaustion")

    monkeypatch.setattr(memory_state.json, "loads", fail_parse)
    monkeypatch.setattr(
        query_memory,
        "answer_with_sources",
        lambda _question: query_memory.GeneratedAnswer(response, ()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_memory.py", question, "--file-back"],
    )

    assert query_memory.main() == 1
    captured = capsys.readouterr()
    assert "publication failed" in captured.err.lower()
    assert "traceback" not in captured.err.lower()
    assert not target.exists()


@pytest.mark.parametrize(
    ("citation", "quote"),
    (
        ("source.md", "Canonical exact evidence."),
        ("knowledge/notes/source", "Canonical exact evidence."),
        ("knowledge/notes/SOURCE.md", "Canonical exact evidence."),
        ("knowledge/notes/../notes/source.md", "Canonical exact evidence."),
        ("/knowledge/notes/source.md", "Canonical exact evidence."),
        ("C:/vault/knowledge/notes/source.md", "Canonical exact evidence."),
        ("knowledge/notes/patterns/source.md", "Shadow exact evidence."),
        ("knowledge/notes/ArChIvE/old.md", "Archived exact evidence."),
        ("knowledge/notes/source.md", "Quote exists only on another page."),
    ),
    ids=(
        "basename",
        "substring",
        "case-change",
        "traversal",
        "posix-absolute",
        "windows-absolute",
        "shadow",
        "archive",
        "wrong-page",
    ),
)
def test_file_back_rejects_noncanonical_citation_or_mismatched_quote(
    tmp_path, monkeypatch, citation, quote
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    _grounding_page(notes / "source.md", evidence="Canonical exact evidence.")
    _grounding_page(
        notes / "patterns" / "source.md", evidence="Shadow exact evidence."
    )
    _grounding_page(
        notes / "ArChIvE" / "old.md", evidence="Archived exact evidence."
    )
    _grounding_page(
        notes / "other.md", title="Other", evidence="Quote exists only on another page."
    )
    response = _grounded_provider_answer(citation, quote)
    expected = notes / f"{query_memory.slugify('Reject bad citation?')}.md"

    with pytest.raises(ValueError):
        query_memory.file_back("Reject bad citation?", response)

    assert not expected.exists()


@pytest.mark.parametrize(
    "citation",
    (
        "knowledge/notes/c0-\x01.md",
        "knowledge/notes/c1-\u0085.md",
        "knowledge/notes/carriage-\rreturn.md",
        "knowledge/notes/line-\nbreak.md",
        "knowledge/notes/line-\u2028separator.md",
        "knowledge/notes/paragraph-\u2029separator.md",
        "knowledge/notes/surrogate-\ud800.md",
        "knowledge/notes/noncharacter-\ufdd0.md",
        "knowledge/notes/noncharacter-\ufffe.md",
        "knowledge/notes/tick`name.md",
        "knowledge/notes/topic#fragment.md",
        "knowledge/notes/topic|alias.md",
        "knowledge/notes/topic^block.md",
        "knowledge/notes/topic[open.md",
        "knowledge/notes/topic]close.md",
        "/knowledge/notes/rooted.md",
        "knowledge/notes/../notes/traversal.md",
    ),
    ids=(
        "c0",
        "c1",
        "carriage-return",
        "line-feed",
        "line-separator",
        "paragraph-separator",
        "surrogate",
        "noncharacter-range",
        "noncharacter-plane-end",
        "backtick",
        "wikilink-fragment",
        "wikilink-alias",
        "wikilink-block",
        "wikilink-open",
        "wikilink-close",
        "rooted",
        "traversal",
    ),
)
def test_file_back_rejects_unsafe_root_relative_citation_syntax(
    tmp_path, monkeypatch, citation
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    _grounding_page(notes / "source.md", evidence="Canonical exact evidence.")
    citation_json = json.dumps(
        {"path": citation, "quote": "Canonical exact evidence."},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    response = (
        "**Answer:**\nReject unsafe paths.\n\n"
        f"**Sources:**\n- {citation_json}\n\n"
        "**Confidence:** high - exact source evidence\n"
    )
    question = "Can unsafe citation syntax be filed?"
    expected = notes / f"{query_memory.slugify(question)}.md"

    with pytest.raises(
        ValueError,
        match="path is not exact ROOT-relative POSIX|invalid Unicode scalar",
    ):
        query_memory.file_back(question, response)

    assert not expected.exists()


@pytest.mark.parametrize(
    "case",
    (
        "nonexistent",
        "archived",
        "superseded",
        "empty-evidence",
        "non-string-evidence",
        "oversized-evidence",
        "control-evidence",
        "duplicate-json-key",
    ),
)
def test_invalid_grounding_contract_fails_before_publication_side_effects(
    tmp_path,
    monkeypatch,
    capsys,
    case,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    evidence = "Canonical exact evidence."
    if case == "nonexistent":
        relative = "knowledge/notes/well-formed-missing.md"
        sources = ()
    elif case in {"archived", "superseded"}:
        source = _grounding_page(
            notes / f"{case}.md",
            status=case,
            evidence=evidence,
        )
        relative = source.relative_to(tmp_path).as_posix()
        sources = ()
    else:
        source = _grounding_page(notes / "source.md", evidence=evidence)
        relative = source.relative_to(tmp_path).as_posix()
        selected = vault_editorial.select_active_notes(notes, root=tmp_path).notes
        sources, _rendered = query_memory._prepare_answer_sources(selected)

    quote: object = evidence
    if case == "empty-evidence":
        quote = ""
    elif case == "non-string-evidence":
        quote = 7
    elif case == "oversized-evidence":
        quote = "x" * (query_memory.MAX_EVIDENCE_QUOTE_CHARS + 1)
    elif case == "control-evidence":
        quote = "Canonical\x01exact evidence."

    if case == "duplicate-json-key":
        encoded_path = json.dumps(relative, ensure_ascii=True)
        encoded_quote = json.dumps(evidence, ensure_ascii=True)
        citation_json = (
            f'{{"path":{encoded_path},"path":{encoded_path},'
            f'"quote":{encoded_quote}}}'
        )
    else:
        citation_json = json.dumps(
            {"path": relative, "quote": quote},
            ensure_ascii=True,
            separators=(",", ":"),
        )
    response = (
        "**Answer:**\nReject invalid evidence.\n\n"
        f"**Sources:**\n- {citation_json}\n\n"
        "**Confidence:** high - exact source evidence\n"
    )
    generated = query_memory.GeneratedAnswer(response, sources)
    question = f"Reject invalid grounding {case}?"
    expected = notes / f"{query_memory.slugify(question)}.md"
    writes: list[Path] = []
    side_effects: list[str] = []

    def reject_write(path, *_args, **_kwargs):
        writes.append(path)
        raise AssertionError("invalid grounding reached publication write")

    monkeypatch.setattr(query_memory, "answer_with_sources", lambda _question: generated)
    monkeypatch.setattr(query_memory, "atomic_write", reject_write)
    monkeypatch.setattr(
        query_memory,
        "rebuild_index",
        lambda: side_effects.append("rebuild") or True,
    )
    monkeypatch.setattr(
        query_memory,
        "append_log",
        lambda _entry: side_effects.append("log"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_memory.py", question, "--file-back"],
    )

    assert query_memory.main() == 1
    stderr = capsys.readouterr().err
    assert "publication failed" in stderr.lower()
    if case == "duplicate-json-key":
        assert "duplicate key" in stderr.lower()
    assert writes == []
    assert side_effects == []
    assert not expected.exists()


def test_generated_source_line_is_unambiguous_for_accepted_path_name(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "Accepted punctuation remains unambiguous inside one code span."
    source = _grounding_page(
        notes / "accepted (v1) name.md",
        title="Accepted Source Name",
        evidence=evidence,
    )
    relative = source.relative_to(tmp_path).as_posix()
    response = _grounded_provider_answer(relative, evidence)

    out = query_memory.file_back("How are accepted source names serialized?", response)

    source_lines = [
        line
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.startswith("- Source:")
    ]
    assert source_lines == [f"- Source: `{relative}`"]
    assert source_lines[0].count("`") == 2


@pytest.mark.parametrize("status", (None, "archived", "superseded"))
def test_file_back_never_replaces_existing_target_even_after_inactive_status(
    tmp_path, monkeypatch, status
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "Existing targets require an absent filename before publication."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    question = "Can an existing Q and A be replaced?"
    target = notes / f"{query_memory.slugify(question)}.md"
    _grounding_page(target, title="Existing Target", status=status, evidence="sentinel")
    before = target.read_bytes()
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(), evidence
    )

    with pytest.raises(FileExistsError):
        query_memory.file_back(question, response)

    assert target.read_bytes() == before


def test_file_back_refuses_active_typed_page_with_same_slug(tmp_path, monkeypatch):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "An active typed slug must not be shadowed by a new flat page."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    question = "Can an active typed slug be shadowed?"
    filename = f"{query_memory.slugify(question)}.md"
    typed = _grounding_page(
        notes / "qa" / filename,
        title="Existing Typed Q and A",
        evidence="typed sentinel",
    )
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(), evidence
    )

    with pytest.raises(FileExistsError):
        query_memory.file_back(question, response)

    assert typed.read_text(encoding="utf-8").endswith("typed sentinel\n")
    assert not (notes / filename).exists()


def test_file_back_refuses_prospective_title_that_would_displace_typed_winner(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    question = "How is durable publication selected?"
    typed = _grounding_page(
        notes / "patterns" / "durable-publication.md",
        title=question,
        evidence="The typed page must remain the canonical winner.",
    )
    source_evidence = "Prospective publication preserves every current winner."
    source = _grounding_page(
        notes / "source.md",
        title="Independent Source",
        evidence=source_evidence,
    )
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        source_evidence,
    )
    target = notes / f"{query_memory.slugify(question)}.md"

    before = vault_editorial.select_active_notes(notes, root=tmp_path)
    assert typed in before.paths

    with pytest.raises(FileExistsError, match="canonical identity"):
        query_memory.file_back(question, response)

    after = vault_editorial.select_active_notes(notes, root=tmp_path)
    assert typed in after.paths
    assert target not in after.paths
    assert not target.exists()


def test_file_back_parses_rendered_hidden_comment_h1_before_collision_check(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    question = "<!--hidden-->Dangerous Identity?"
    typed = _grounding_page(
        notes / "patterns" / "typed-dangerous.md",
        title="Dangerous Identity?",
        evidence="The visible typed identity remains canonical.",
    )
    source_evidence = "Rendered candidates use the canonical identity parser."
    source = _grounding_page(
        notes / "source.md",
        title="Independent Source",
        evidence=source_evidence,
    )
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        source_evidence,
    )
    target = notes / f"{query_memory.slugify(question)}.md"

    before = vault_editorial.select_active_notes(notes, root=tmp_path)
    assert typed in before.paths

    with pytest.raises(FileExistsError, match="canonical identity"):
        query_memory.file_back(question, response)

    assert typed in vault_editorial.select_active_notes(notes, root=tmp_path).paths
    assert not target.exists()


def test_file_back_rechecks_rendered_hidden_comment_h1_under_publication_lock(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)

    question = "<!--hidden-->Dangerous Identity?"
    source_evidence = "Locked publication rechecks every prospective identity."
    source = _grounding_page(
        notes / "source.md",
        title="Independent Source",
        evidence=source_evidence,
    )
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        source_evidence,
    )
    target = notes / f"{query_memory.slugify(question)}.md"
    typed = notes / "patterns" / "typed-dangerous.md"
    lock_active = False

    @contextmanager
    def racing_publication_lock(*_args, **_kwargs):
        nonlocal lock_active
        lock_active = True
        _grounding_page(
            typed,
            title="Dangerous Identity?",
            evidence="The locked typed identity remains canonical.",
        )
        try:
            yield
        finally:
            lock_active = False

    real_select = query_memory.select_active_notes

    def guarded_select(*args, **kwargs):
        assert lock_active
        return real_select(*args, **kwargs)

    monkeypatch.setattr(
        query_memory,
        "knowledge_publication_lock",
        racing_publication_lock,
    )
    monkeypatch.setattr(query_memory, "select_active_notes", guarded_select)

    with pytest.raises(FileExistsError, match="canonical identity"):
        query_memory.file_back(question, response)

    assert typed.exists()
    assert not target.exists()


def test_file_back_rejects_multibyte_candidate_over_exact_utf8_byte_limit(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)

    evidence = "Candidate sizing uses exact encoded artifact bytes."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    question = "Can a multibyte oversized answer be filed?"
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        evidence,
        answer="é" * 64_000,
    )
    target = notes / f"{query_memory.slugify(question)}.md"
    writes: list[Path] = []
    monkeypatch.setattr(
        query_memory,
        "atomic_write",
        lambda path, *_args, **_kwargs: writes.append(path),
    )
    assert len(response) <= query_memory.MAX_ANSWER_TEXT_CHARS

    with pytest.raises(ValueError, match="UTF-8 byte limit"):
        query_memory.file_back(question, response)

    assert writes == []
    assert not target.exists()


def test_file_back_reserves_one_inventory_entry_before_any_side_effect(
    tmp_path,
    monkeypatch,
    capsys,
):
    root = tmp_path / "entry-cap"
    query_memory, notes = _configure_query_memory(monkeypatch, root)
    import vault_editorial

    evidence = "Candidate-inclusive inventory admission reserves the new page."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    _grounding_page(
        notes / "existing.md",
        title="Existing Capacity Consumer",
        evidence="The existing page consumes the final inventory slot.",
    )
    relative = source.relative_to(root).as_posix()
    response = _grounded_provider_answer(relative, evidence)
    selection = vault_editorial.select_active_notes(notes, root=root)
    sources, _rendered = query_memory._prepare_answer_sources(selection.notes)
    generated = query_memory.GeneratedAnswer(response, sources)
    target = notes / f"{query_memory.slugify('Can a full inventory accept another page?')}.md"
    side_effects: list[str] = []
    monkeypatch.setattr(query_memory, "MAX_ACTIVE_NOTE_ENTRIES", 2)
    monkeypatch.setattr(query_memory, "answer_with_sources", lambda _question: generated)
    monkeypatch.setattr(
        query_memory, "rebuild_index", lambda: side_effects.append("rebuild") or True
    )
    monkeypatch.setattr(
        query_memory, "append_log", lambda _entry: side_effects.append("log")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_memory.py", "Can a full inventory accept another page?", "--file-back"],
    )

    assert query_memory.main() == 1
    assert "inventory" in capsys.readouterr().err.lower()
    assert side_effects == []
    assert not target.exists()


def test_file_back_accepts_exact_candidate_inclusive_inventory_boundary(
    tmp_path,
    monkeypatch,
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "The exact candidate-inclusive entry boundary remains valid."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(),
        evidence,
    )
    monkeypatch.setattr(query_memory, "MAX_ACTIVE_NOTE_ENTRIES", 2)

    out = query_memory.file_back("Can the exact inventory boundary publish?", response)

    assert out.exists()
    assert len(list(notes.glob("*.md"))) == 2


@pytest.mark.parametrize(("extra_bytes", "accepted"), ((0, True), (1, False)))
def test_file_back_enforces_candidate_inclusive_aggregate_byte_boundary(
    tmp_path,
    monkeypatch,
    capsys,
    extra_bytes,
    accepted,
):
    question = "Can the aggregate byte boundary publish?"
    evidence = "Candidate-inclusive aggregate admission uses encoded bytes."
    probe_root = tmp_path / f"probe-{extra_bytes}"
    query_memory, probe_notes = _configure_query_memory(monkeypatch, probe_root)
    probe_source = _grounding_page(probe_notes / "source.md", evidence=evidence)
    probe_response = _grounded_provider_answer(
        probe_source.relative_to(probe_root).as_posix(),
        evidence,
    )
    probe_out = query_memory.file_back(question, probe_response)
    candidate_size = probe_out.stat().st_size

    root = tmp_path / f"aggregate-{extra_bytes}"
    query_memory, notes = _configure_query_memory(monkeypatch, root)
    source = _grounding_page(notes / "source.md", evidence=evidence)
    aggregate_cap = 4 * 1024
    existing_size = aggregate_cap - candidate_size + extra_bytes
    source_bytes = source.read_bytes()
    assert len(source_bytes) < existing_size
    source.write_bytes(source_bytes + b"x" * (existing_size - len(source_bytes)))
    relative = source.relative_to(root).as_posix()
    response = _grounded_provider_answer(relative, evidence)
    target = notes / f"{query_memory.slugify(question)}.md"
    monkeypatch.setattr(
        query_memory,
        "MAX_ACTIVE_NOTE_TOTAL_BYTES",
        aggregate_cap,
        raising=False,
    )

    if accepted:
        out = query_memory.file_back(question, response)
        assert out == target
        assert sum(path.stat().st_size for path in notes.glob("*.md")) == aggregate_cap
        return

    import vault_editorial

    selection = vault_editorial.select_active_notes(notes, root=root)
    sources, _rendered = query_memory._prepare_answer_sources(selection.notes)
    generated = query_memory.GeneratedAnswer(response, sources)
    side_effects: list[str] = []
    monkeypatch.setattr(query_memory, "answer_with_sources", lambda _question: generated)
    monkeypatch.setattr(
        query_memory, "rebuild_index", lambda: side_effects.append("rebuild") or True
    )
    monkeypatch.setattr(
        query_memory, "append_log", lambda _entry: side_effects.append("log")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_memory.py", question, "--file-back"],
    )

    assert query_memory.main() == 1
    assert "aggregate" in capsys.readouterr().err.lower()
    assert side_effects == []
    assert not target.exists()


def test_concurrent_file_back_same_slug_creates_once_without_replacement(
    tmp_path, monkeypatch
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "Concurrent publication has one create-only winner."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    relative = source.relative_to(tmp_path).as_posix()
    response = _grounded_provider_answer(relative, evidence)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def publish():
        try:
            barrier.wait(timeout=5)
            outcomes.append(query_memory.file_back("Concurrent question?", response))
        except Exception as exc:  # noqa: BLE001 - asserted below
            outcomes.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    target = notes / f"{query_memory.slugify('Concurrent question?')}.md"
    assert not any(thread.is_alive() for thread in threads)
    assert sum(isinstance(item, Path) for item in outcomes) == 1
    assert sum(isinstance(item, FileExistsError) for item in outcomes) == 1
    assert target.read_text(encoding="utf-8").count("## Question") == 1


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_file_back_never_follows_existing_symlink_target(tmp_path, monkeypatch):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    evidence = "Symlink targets must never be followed by publication."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    question = "Can a symlink target be replaced?"
    target = notes / f"{query_memory.slugify(question)}.md"
    sentinel = tmp_path / "sentinel.md"
    sentinel.write_text("sentinel", encoding="utf-8")
    try:
        target.symlink_to(sentinel)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    response = _grounded_provider_answer(
        source.relative_to(tmp_path).as_posix(), evidence
    )

    with pytest.raises((FileExistsError, OSError)):
        query_memory.file_back(question, response)

    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert target.is_symlink()


def test_publication_failure_does_not_rebuild_or_append_log(
    tmp_path, monkeypatch, capsys
):
    query_memory, notes = _configure_query_memory(monkeypatch, tmp_path)
    import vault_editorial

    evidence = "Failed publication has no index or log side effects."
    source = _grounding_page(notes / "source.md", evidence=evidence)
    relative = source.relative_to(tmp_path).as_posix()
    generated = query_memory.GeneratedAnswer(
        text=_grounded_provider_answer(relative, evidence),
        sources=vault_editorial.select_active_notes(notes, root=tmp_path).notes,
    )
    side_effects: list[str] = []
    monkeypatch.setattr(query_memory, "answer_with_sources", lambda _question: generated)
    monkeypatch.setattr(
        query_memory,
        "atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError("race")),
    )
    monkeypatch.setattr(
        query_memory, "rebuild_index", lambda: side_effects.append("rebuild") or True
    )
    monkeypatch.setattr(
        query_memory, "append_log", lambda _entry: side_effects.append("log")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_memory.py", "question", "--file-back"],
    )

    assert query_memory.main() == 1
    assert side_effects == []
    assert "publication failed" in capsys.readouterr().err.lower()
