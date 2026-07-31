"""Tests for build_guardrails.py — rule extraction and dedup."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _sensitive_metadata_page(path: Path, metadata: str) -> Path:
    path.write_text(
        f"---\ntype: pattern\n{metadata}\n"
        f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n---\n\n"
        f"# {path.stem}\n\nOne-sentence summary: shared parser test.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_knowledge_dir(tmp_path, monkeypatch):
    """Set up a temporary knowledge directory."""
    import build_guardrails

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    monkeypatch.setattr(build_guardrails, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(build_guardrails, "FEEDBACK_DIR", tmp_path / "feedback")
    monkeypatch.setattr(build_guardrails, "GUARDRAILS_FILE", tmp_path / "guardrails.md")
    monkeypatch.setattr(build_guardrails, "ROOT", tmp_path)
    (tmp_path / "feedback").mkdir()
    return knowledge


def make_page(path: Path, page_type: str, title: str, summary: str):
    """Create a knowledge page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {page_type}\n---\n\n# {title}\n\nOne-sentence summary: {summary}\n\n## Body\nContent.\n",
        encoding="utf-8",
    )


def make_promoted_feedback(path: Path, rule_text: str, **overrides):
    candidate = {
        "status": "promoted",
        "project": "alpha",
        "type": "correction",
        "text": rule_text,
        "promoted_to": "knowledge/notes/valid.md",
        **overrides,
    }
    path.write_text(json.dumps(candidate), encoding="utf-8")


def test_lint_treats_matching_hash_without_receipt_as_uncompiled(
    tmp_path,
    monkeypatch,
):
    import lint_memory

    vault = tmp_path / "vault"
    daily = vault / "knowledge" / "daily" / "2026-07-28.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("compiled bytes without receipt", encoding="utf-8")
    monkeypatch.setattr(lint_memory, "ROOT", vault)
    monkeypatch.setattr(lint_memory, "DAILY_DIR", daily.parent)
    state = {
        "compiled_daily_hashes": {
            daily.name: lint_memory.file_hash(daily),
        }
    }

    assert lint_memory.check_orphan_daily_logs(state) == [
        "knowledge/daily/2026-07-28.md"
    ]


def test_collect_correction_type(fake_knowledge_dir):
    """Pages with type=pattern and imperative language are collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/correction1.md",
              "pattern", "Use JWT", "Always use JWT instead of sessions for auth")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 1


def test_collect_preference_type(fake_knowledge_dir):
    """Pages with type=decision and preference language are collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/pref1.md",
              "decision", "Short answers", "Must always prefer concise responses")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 1
    assert "concise" in corrections[0]["summary"]


def test_collect_pattern_with_imperative(fake_knowledge_dir):
    """Patterns with 'do not' / 'always' in summary are collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/rule1.md",
              "pattern", "Backlink rule", "Always add reciprocal backlinks when creating new pages")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 1


def test_collect_ignores_plain_patterns(fake_knowledge_dir):
    """Patterns without imperative language are NOT collected."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/info.md",
              "pattern", "Mirror pipelines", "This pattern describes reusing existing infrastructure shapes")
    corrections = build_guardrails._collect_corrections()
    assert len(corrections) == 0


def test_collect_filters_by_project(fake_knowledge_dir):
    """Project filter works."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/c1.md",
              "pattern", "Rule A", "Always do X",
              )
    # Add project to frontmatter
    path = fake_knowledge_dir / "patterns/c1.md"
    content = path.read_text()
    content = content.replace("---\n", "---\nproject: project-a\n", 1)
    path.write_text(content)

    # Should find with project filter
    assert len(build_guardrails._collect_corrections("project-a")) == 1
    # Should NOT find with different project filter
    assert len(build_guardrails._collect_corrections("project-b")) == 0


def test_quoted_project_keys_scope_and_invalid_key_forms_fail_closed(
    fake_knowledge_dir,
):
    import build_guardrails

    pages = {
        "plain-key.md": ("projectile: decoy\nproject: beta", "Plain key"),
        "single-key.md": ("'project': beta", "Single quoted key"),
        "double-key.md": ('"project": beta', "Double quoted key"),
        "malformed-key.md": ('"project" = beta', "Malformed quoted key"),
        "closing-double-key.md": ('project": beta', "Closing double quote"),
        "closing-single-key.md": ("project': beta", "Closing single quote"),
        "mismatched-double-key.md": ("\"project': beta", "Mismatched double quote"),
        "mismatched-single-key.md": ("'project\": beta", "Mismatched single quote"),
        "duplicate-key.md": ("'project': beta\nproject: beta", "Duplicate key"),
    }
    for filename, (scope, title) in pages.items():
        (fake_knowledge_dir / filename).write_text(
            "---\n"
            "type: pattern\n"
            f"{scope}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"One-sentence summary: Always enforce {title}.\n",
            encoding="utf-8",
        )

    beta_titles = {
        item["title"] for item in build_guardrails._collect_corrections("beta")
    }
    alpha_titles = {
        item["title"] for item in build_guardrails._collect_corrections("alpha")
    }
    global_titles = {
        item["title"] for item in build_guardrails._collect_corrections(None)
    }

    assert beta_titles == {"Plain key", "Single quoted key", "Double quoted key"}
    assert alpha_titles == set()
    assert global_titles == {"Plain key", "Single quoted key", "Double quoted key"}


def test_guardrail_project_scope_accepts_quoted_scalar_with_inline_comment(
    fake_knowledge_dir,
):
    import build_guardrails

    path = fake_knowledge_dir / "quoted-project.md"
    path.write_text(
        "---\n"
        "type: pattern\n"
        'project: "beta" # owned by beta\n'
        "---\n\n"
        "# Scoped rule\n\n"
        "One-sentence summary: Always keep beta data isolated.\n",
        encoding="utf-8",
    )

    assert len(build_guardrails._collect_corrections("beta")) == 1
    assert build_guardrails._collect_corrections("alpha") == []


def test_indented_block_scalar_marker_does_not_end_project_frontmatter(
    fake_knowledge_dir,
):
    import build_guardrails

    (fake_knowledge_dir / "block-scalar.md").write_text(
        "---\n"
        "type: pattern\n"
        "description: |\n"
        "  scalar text\n"
        "  ---\n"
        "project: beta\n"
        "---\n\n"
        "# Beta block scalar\n\n"
        "One-sentence summary: Always keep this rule scoped to beta.\n",
        encoding="utf-8",
    )

    assert len(build_guardrails._collect_corrections("beta")) == 1
    assert build_guardrails._collect_corrections("alpha") == []


def test_advisory_decision_scope_accepts_quoted_scalar_with_inline_comment(
    monkeypatch,
    tmp_path: Path,
):
    import build_advisory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    (knowledge / "beta-decision.md").write_text(
        "---\n"
        "type: decision\n"
        "timestamp: 2026-07-28\n"
        'project: "beta" # owned by beta\n'
        "---\n\n"
        "# Beta-only decision\n\n"
        "One-sentence summary: Keep this decision private to beta.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)

    assert build_advisory._find_last_decision("beta") is not None
    assert build_advisory._find_last_decision("alpha") is None


def test_frontmatter_scalar_supports_yaml_single_quote_escaping():
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        "---\nproject: 'be''ta' # quoted apostrophe\n---\n",
        "project",
    )

    assert parsed == memory_state.FrontmatterScalar(True, "be'ta")


@pytest.mark.parametrize("style", ("|", ">"))
def test_frontmatter_scalar_ignores_keys_inside_block_scalars(style):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        "---\n"
        f"description: {style}\n"
        "  Historical metadata example:\n"
        "  status: archived\n"
        "status: active\n"
        "---\n",
        "status",
    )

    assert parsed == memory_state.FrontmatterScalar(True, "active")


def test_frontmatter_scalar_parser_is_available_from_tracked_memory_state():
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        "---\n'project': 'beta'\n---\n",
        "project",
    )

    assert parsed == memory_state.FrontmatterScalar(True, "beta")


@pytest.mark.parametrize(
    ("field", "key_token", "value"),
    (
        ("project", '"pro\\u006aect"', "beta"),
        ("project", '"pro\\x6aect"', "beta"),
        ("project", '"pro\\U0000006aect"', "beta"),
        ("status", '"sta\\u0074us"', "active"),
        ("status", '"sta\\x74us"', "active"),
        ("status", '"sta\\U00000074us"', "active"),
        ("project", "'project'", "beta"),
    ),
)
def test_frontmatter_scalar_decodes_quoted_top_level_keys(field, key_token, value):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{key_token}: {value}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, value)


@pytest.mark.parametrize(
    ("escape", "decoded"),
    (
        ("0", "\0"),
        ("a", "\a"),
        ("b", "\b"),
        ("t", "\t"),
        ("n", "\n"),
        ("v", "\v"),
        ("f", "\f"),
        ("r", "\r"),
        ("e", "\x1b"),
        (" ", " "),
        ('"', '"'),
        ("/", "/"),
        ("\\", "\\"),
        ("N", "\x85"),
        ("_", "\xa0"),
        ("L", "\u2028"),
        ("P", "\u2029"),
    ),
)
def test_frontmatter_scalar_decodes_complete_yaml_simple_key_escapes(
    escape,
    decoded,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f'---\n"pre\\{escape}post": value\n---\n',
        f"pre{decoded}post",
    )

    assert parsed == memory_state.FrontmatterScalar(True, "value")


def test_frontmatter_scalar_decodes_yaml_single_quoted_key_escaping():
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        "---\n'pro''ject': beta\n---\n",
        "pro'ject",
    )

    assert parsed == memory_state.FrontmatterScalar(True, "beta")


@pytest.mark.parametrize(
    ("field", "value_token", "expected"),
    (
        ("project", '"be\\x74a"', "beta"),
        ("project", '"be\\U00000074a"', "beta"),
        ("status", '"super\\x73eded"', "superseded"),
        ("status", '"super\\U00000073eded"', "superseded"),
    ),
)
def test_frontmatter_scalar_decodes_complete_yaml_double_quoted_values(
    field,
    value_token,
    expected,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{field}: {value_token}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, expected)


def test_frontmatter_scalar_rejects_duplicate_decoded_key_spellings():
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        '---\nproject: beta\n"pro\\u006aect": beta\n---\n',
        "project",
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


@pytest.mark.parametrize(
    "key_line",
    (
        '"pro\\u006aect: beta',
        '"pro\\u006aect" = beta',
        '"project\\q": beta',
        '"pro\\xG0ject": beta',
        '"pro\\uD800ject": beta',
        '"pro\\U0000D800ject": beta',
        '"pro\\U00110000ject": beta',
        '"\\qproject": beta',
        '"\\xG0project": beta',
        '"\\U00110000project": beta',
        '"project: beta',
        '"project" = beta',
        "'project: beta",
    ),
)
def test_frontmatter_scalar_fails_closed_on_malformed_quoted_target_key(key_line):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{key_line}\n---\n",
        "project",
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


@pytest.mark.parametrize(
    ("field", "valid_line"),
    (
        ("project", "project: beta"),
        ("status", "status: active"),
    ),
)
def test_malformed_unrelated_quoted_mapping_key_invalidates_sensitive_metadata(
    field,
    valid_line,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f'---\n"description\\q": ignored\n{valid_line}\n---\n',
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


@pytest.mark.parametrize(
    ("field", "valid_line"),
    (
        ("project", "project: beta"),
        ("status", "status: active"),
        ("type", "type: pattern"),
    ),
)
@pytest.mark.parametrize("malformed_line", ("description = ignored", "description:value"))
def test_malformed_unrelated_plain_root_line_invalidates_sensitive_metadata(
    field,
    valid_line,
    malformed_line,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{malformed_line}\n{valid_line}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


def test_frontmatter_scalar_ignores_escaped_key_inside_block_scalar():
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        '---\ndescription: |\n  "sta\\u0074us": archived\nstatus: active\n---\n',
        "status",
    )

    assert parsed == memory_state.FrontmatterScalar(True, "active")


@pytest.mark.parametrize(
    ("field", "frontmatter", "expected"),
    (
        ("status", "? status\n: superseded", "superseded"),
        ("project", '? "pro\\u006aect"\n: "be\\x74a"', "beta"),
        ("type", "? 'type'\n: 'decision'", "decision"),
    ),
)
def test_frontmatter_scalar_supports_top_level_simple_explicit_mapping_entries(
    field,
    frontmatter,
    expected,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{frontmatter}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, expected)


@pytest.mark.parametrize(
    ("field", "frontmatter"),
    (
        ("status", "status: active\n? status\n: superseded"),
        ("project", '? "pro\\u006aect"\n: beta\nproject: beta'),
        ("type", "? type\n: decision\n? 'type'\n: pattern"),
        ("status", "? status\nproject: beta"),
        ("project", "? project\n: "),
        ("type", '? "type\n: decision'),
        ("status", "? [status]\n: superseded"),
    ),
)
def test_frontmatter_scalar_fails_closed_on_duplicate_or_malformed_explicit_entries(
    field,
    frontmatter,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{frontmatter}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


def test_explicit_sensitive_metadata_is_shared_by_search_index_graph_and_archive(
    tmp_path,
):
    import archive_stale
    import graph_neighbors
    import rebuild_memory_index
    import search_memory

    content = (
        "---\n"
        "? project\n"
        ": beta\n"
        "? status\n"
        ": superseded\n"
        "---\n\n# Hidden\n"
    )
    page = tmp_path / "explicit.md"
    page.write_text(content, encoding="utf-8")
    old_time = datetime.now().timestamp() - (999 * 86400)
    os.utime(page, (old_time, old_time))

    assert search_memory._active_search_metadata(content) == (False, "")
    assert rebuild_memory_index._page_is_active(content) is False
    assert graph_neighbors._is_inactive(content) is True
    assert archive_stale._is_stale(page, old_time + 1, 180) is False


@pytest.mark.parametrize(
    "root_form",
    (
        "{status: superseded, type: decision}",
        "!!map\nstatus: superseded\ntype: decision",
        "&metadata\nstatus: superseded\ntype: decision",
        "*metadata",
    ),
    ids=("flow-mapping", "tag", "anchor", "alias"),
)
@pytest.mark.parametrize("field", ("status", "project", "type"))
def test_frontmatter_scalar_fails_closed_on_unsupported_yaml_root_forms(
    root_form,
    field,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{root_form}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


@pytest.mark.parametrize(
    "opening_line",
    (
        "--- {status: superseded}",
        "--- !!map",
        "--- &metadata",
        "--- *metadata",
    ),
)
def test_frontmatter_scalar_fails_closed_on_root_forms_after_document_marker(
    opening_line,
):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"{opening_line}\n---\n",
        "status",
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


@pytest.mark.parametrize(
    "opening_line",
    (
        "--- {status: active, type: pattern}",
        "--- !!map",
        "--- &metadata",
        "--- *metadata",
    ),
)
def test_document_marker_root_forms_are_unarchivable_and_lint_invalid(
    tmp_path,
    monkeypatch,
    opening_line,
):
    import archive_stale
    import lint_memory

    content = f"{opening_line}\n---\n\n# Unsupported root\n"
    page = tmp_path / "marker-root.md"
    page.write_text(content, encoding="utf-8")
    old_time = datetime.now().timestamp() - (999 * 86400)
    os.utime(page, (old_time, old_time))
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    assert archive_stale._is_stale(page, old_time + 1, 1) is False
    assert archive_stale._with_archived_status(content) is None
    assert lint_memory.check_missing_frontmatter([page]) == []
    assert lint_memory.check_missing_required_type([page]) == []
    assert lint_memory.check_invalid_type_value([page]) == [
        f"{page.name} (type metadata is invalid)"
    ]


@pytest.mark.parametrize(
    "root_form",
    (
        "{status: active, type: pattern}",
        "!!map\nstatus: active\ntype: pattern",
        "&metadata\nstatus: active\ntype: pattern",
        "*metadata",
    ),
)
def test_unsupported_yaml_root_forms_are_inactive_unarchivable_and_lint_invalid(
    tmp_path,
    monkeypatch,
    root_form,
):
    import archive_stale
    import graph_neighbors
    import lint_memory
    import rebuild_memory_index
    import search_memory

    content = f"---\n{root_form}\n---\n\n# Unsupported root\n"
    page = tmp_path / "unsupported-root.md"
    page.write_text(content, encoding="utf-8")
    old_time = datetime.now().timestamp() - (999 * 86400)
    os.utime(page, (old_time, old_time))
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    assert search_memory._active_search_metadata(content) == (False, "")
    assert rebuild_memory_index._page_is_active(content) is False
    assert graph_neighbors._is_inactive(content) is True
    assert archive_stale._is_stale(page, old_time + 1, 1) is False
    assert lint_memory.check_missing_required_type([page]) == []
    assert lint_memory.check_invalid_type_value([page]) == [
        f"{lint_memory._rel(page)} (type metadata is invalid)"
    ]


def test_frontmatter_scalar_strips_utf8_bom_and_dedents_uniform_root():
    import memory_state

    content = (
        "\ufeff---\n"
        "  type: pattern\n"
        "  status: active\n"
        "  project: sample-project\n"
        "---\n"
    )

    assert memory_state.parse_frontmatter_scalar(content, "type") == (
        memory_state.FrontmatterScalar(True, "pattern")
    )
    assert memory_state.parse_frontmatter_scalar(content, "status") == (
        memory_state.FrontmatterScalar(True, "active")
    )
    assert memory_state.parse_frontmatter_scalar(content, "project") == (
        memory_state.FrontmatterScalar(True, "sample-project")
    )


@pytest.mark.parametrize(
    "root_form",
    (
        "  {status: active, type: pattern}",
        "  !!map\n  status: active\n  type: pattern",
        "  &metadata\n  status: active\n  type: pattern",
        "  *metadata",
    ),
    ids=("flow", "tag", "anchor", "alias"),
)
@pytest.mark.parametrize("field", ("status", "project", "type"))
def test_indented_unsupported_yaml_roots_are_present_invalid(root_form, field):
    import memory_state

    parsed = memory_state.parse_frontmatter_scalar(
        f"---\n{root_form}\n---\n",
        field,
    )

    assert parsed == memory_state.FrontmatterScalar(True, None)


@pytest.mark.parametrize("field", ("status", "project", "type"))
def test_yaml_merge_anchor_alias_metadata_is_present_invalid(field):
    import memory_state

    content = (
        "---\n"
        "defaults: &defaults\n"
        "  type: pattern\n"
        "  status: active\n"
        "  project: sample-project\n"
        "<<: *defaults\n"
        "---\n"
    )

    assert memory_state.parse_frontmatter_scalar(content, field) == (
        memory_state.FrontmatterScalar(True, None)
    )


def test_yaml_merge_metadata_is_inactive_unarchivable_and_lint_invalid(
    tmp_path,
    monkeypatch,
):
    import archive_stale
    import lint_memory
    import rebuild_memory_index
    import search_memory

    content = (
        "---\n"
        "defaults: &defaults\n"
        "  type: pattern\n"
        "  status: active\n"
        "<<: *defaults\n"
        "---\n\n# Merged metadata\n"
    )
    page = tmp_path / "merged-metadata.md"
    page.write_text(content, encoding="utf-8")
    old_time = datetime.now().timestamp() - 999 * 86400
    os.utime(page, (old_time, old_time))
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    assert search_memory._active_search_metadata(content) == (False, "")
    assert rebuild_memory_index._page_is_active(content) is False
    assert archive_stale._is_stale(page, old_time + 1, 1) is False
    assert archive_stale._with_archived_status(content) is None
    assert lint_memory.check_missing_required_type([page]) == []
    assert lint_memory.check_invalid_type_value([page]) == [
        f"{lint_memory._rel(page)} (type metadata is invalid)"
    ]


def test_lint_type_checks_decode_quoted_type_and_fail_closed_on_invalid_type(
    tmp_path,
    monkeypatch,
):
    import lint_memory

    valid = tmp_path / "quoted-decision.md"
    valid.write_text(
        "---\n"
        'type: "deci\\x73ion"\n'
        "---\n\n"
        "# Quoted decision\n\n"
        + " ".join(f"claim{i}" for i in range(60))
        + "\n",
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid-type.md"
    invalid.write_text('---\ntype: "deci\\qision"\n---\n\n# Invalid\n', encoding="utf-8")
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)

    assert lint_memory._page_type(valid) == "decision"
    assert lint_memory.check_missing_required_type([valid]) == []
    assert lint_memory.check_invalid_type_value([valid]) == []
    assert lint_memory.check_missing_sources_section([valid]) == [valid.name]
    assert lint_memory._page_type(invalid) is None
    assert lint_memory.check_missing_required_type([invalid]) == []
    assert lint_memory.check_invalid_type_value([invalid]) == [
        f"{invalid.name} (type metadata is invalid)"
    ]


def test_active_frontmatter_type_consumers_have_no_independent_type_regex():
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    active_consumers = (
        "agent_timeline.py",
        "archive_stale.py",
        "build_advisory.py",
        "build_context.py",
        "build_guardrails.py",
        "lint_memory.py",
        "migrate_to_okf.py",
        "rebuild_memory_index.py",
    )

    for name in active_consumers:
        source = (scripts / name).read_text(encoding="utf-8")
        assert r"^type:\s*" not in source, name


def test_markdown_index_uses_shared_sensitive_frontmatter_parser(
    tmp_path,
    monkeypatch,
):
    import rebuild_memory_index

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    active = _sensitive_metadata_page(
        notes / "active.md",
        'project: "be\\x74a"\nstatus: active',
    )
    _sensitive_metadata_page(
        notes / "inactive.md",
        'project: beta\nstatus: "super\\x73eded"',
    )
    _sensitive_metadata_page(
        notes / "invalid-status.md",
        'project: beta\nstatus: "\\qactive"',
    )
    _sensitive_metadata_page(
        notes / "invalid-project.md",
        'project: "\\qbeta"\nstatus: active',
    )
    monkeypatch.setattr(rebuild_memory_index, "ROOT", tmp_path)
    monkeypatch.setattr(rebuild_memory_index, "memory", tmp_path / "knowledge")
    monkeypatch.setattr(rebuild_memory_index, "knowledge", notes)
    monkeypatch.setattr(
        rebuild_memory_index,
        "SUBDIR_SECTIONS",
        {name: notes / path.name for name, path in rebuild_memory_index.SUBDIR_SECTIONS.items()},
    )

    buckets = rebuild_memory_index.collect_pages()
    indexed = {page for pages in buckets.values() for page in pages}

    assert indexed == {active}


def test_agent_timeline_uses_shared_project_scope_parser(tmp_path, monkeypatch):
    import agent_timeline

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    active = _sensitive_metadata_page(
        notes / "active.md",
        'project: "be\\x74a"\nstatus: active',
    )
    _sensitive_metadata_page(
        notes / "invalid.md",
        'project: "\\qbeta"\nstatus: active',
    )
    monkeypatch.setattr(agent_timeline, "ROOT", tmp_path)
    monkeypatch.setattr(agent_timeline, "KNOWLEDGE", notes)

    results = agent_timeline._extract_knowledge_timeline("beta", 1)

    assert [item["path"] for item in results] == [active.relative_to(tmp_path).as_posix()]


def test_temporal_lint_uses_shared_status_parser_fail_closed(tmp_path, monkeypatch):
    import lint_memory

    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    inactive = _sensitive_metadata_page(
        tmp_path / "inactive.md",
        'valid_to: 2000-01-01\nstatus: "super\\x73eded"',
    )
    invalid = _sensitive_metadata_page(
        tmp_path / "invalid.md",
        'valid_to: 2000-01-01\nstatus: "\\qactive"',
    )
    active = _sensitive_metadata_page(
        tmp_path / "active.md",
        'valid_to: 2000-01-01\nstatus: active',
    )
    quoted_active = _sensitive_metadata_page(
        tmp_path / "quoted-active.md",
        'valid_to: 2000-01-01\nstatus: "ac\\x74ive"',
    )

    findings = lint_memory.check_temporal_validity(
        [inactive, invalid, active, quoted_active]
    )

    assert len(findings) == 2
    assert {Path(finding.split(" ", 1)[0]).name for finding in findings} == {
        "active.md",
        "quoted-active.md",
    }


@pytest.mark.parametrize(
    "project_key",
    ('"pro\\u006aect"', '"pro\\x6aect"', '"pro\\U0000006aect"'),
)
def test_escaped_project_key_scopes_guardrail_advisory_and_project_context(
    fake_knowledge_dir,
    monkeypatch,
    tmp_path: Path,
    project_key,
):
    import build_advisory
    import build_context
    import build_guardrails

    guardrail = fake_knowledge_dir / "escaped-project-rule.md"
    guardrail.write_text(
        "---\n"
        "type: pattern\n"
        f"{project_key}: beta\n"
        "---\n\n"
        "# Escaped project rule\n\n"
        "One-sentence summary: Always keep ESCAPED_PROJECT_RULE scoped.\n",
        encoding="utf-8",
    )
    decision = fake_knowledge_dir / "escaped-project-decision.md"
    decision.write_text(
        "---\n"
        "type: decision\n"
        "timestamp: 2026-07-30\n"
        f"{project_key}: beta\n"
        "---\n\n"
        "# Escaped project decision\n\n"
        "One-sentence summary: ESCAPED_PROJECT_DECISION stays scoped.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", fake_knowledge_dir)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)
    monkeypatch.setattr(build_context, "KNOWLEDGE", fake_knowledge_dir)
    monkeypatch.setattr(build_context, "ROOT", tmp_path)

    beta_rules = build_guardrails.build_guardrails("beta") or ""
    alpha_rules = build_guardrails.build_guardrails("alpha") or ""
    beta_pages = build_context._find_project_pages("beta")

    assert "ESCAPED_PROJECT_RULE" in beta_rules
    assert "ESCAPED_PROJECT_RULE" not in alpha_rules
    assert build_advisory._find_last_decision("beta")["title"] == (
        "Escaped project decision"
    )
    assert build_advisory._find_last_decision("alpha") is None
    assert {page["title"] for page in beta_pages} == {
        "Escaped project rule",
        "Escaped project decision",
    }


def test_malformed_escaped_project_key_is_never_global_or_project_scoped(
    fake_knowledge_dir,
    monkeypatch,
    tmp_path: Path,
):
    import build_advisory
    import build_context
    import build_guardrails

    malformed = fake_knowledge_dir / "malformed-escaped-project.md"
    malformed.write_text(
        "---\n"
        "type: decision\n"
        "timestamp: 2026-07-30\n"
        '"pro\\u006aect: beta\n'
        "---\n\n"
        "# Malformed escaped project\n\n"
        "One-sentence summary: Always hide MALFORMED_ESCAPED_PROJECT.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", fake_knowledge_dir)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)
    monkeypatch.setattr(build_context, "KNOWLEDGE", fake_knowledge_dir)
    monkeypatch.setattr(build_context, "ROOT", tmp_path)

    assert build_guardrails._collect_corrections("beta") == []
    assert build_guardrails._collect_corrections(None) == []
    assert build_advisory._find_last_decision("beta") is None
    assert build_advisory._find_last_decision(None) is None
    assert build_context._find_project_pages("beta") == []


@pytest.mark.parametrize(
    "status_line",
    (
        '"sta\\u0074us": superseded',
        '"sta\\x74us": superseded',
        '"sta\\U00000074us": superseded',
        '"sta\\u0074us: superseded',
    ),
)
def test_escaped_or_malformed_inactive_status_hides_guardrail_and_advisory(
    fake_knowledge_dir,
    monkeypatch,
    tmp_path: Path,
    status_line,
):
    import build_advisory
    import build_guardrails

    page = fake_knowledge_dir / "escaped-status-decision.md"
    page.write_text(
        "---\n"
        "type: decision\n"
        "timestamp: 2026-07-30\n"
        f"{status_line}\n"
        "---\n\n"
        "# Hidden escaped status\n\n"
        "One-sentence summary: Always hide ESCAPED_STATUS_SENTINEL.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", fake_knowledge_dir)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)

    assert build_guardrails._collect_corrections(None) == []
    assert build_advisory._find_last_decision(None) is None


@pytest.mark.parametrize(
    "scope_line",
    (
        'project: "beta',
        "project: [beta]",
        "project = beta",
        "project: beta\nproject: alpha",
    ),
)
def test_present_invalid_guardrail_scope_is_never_global(
    fake_knowledge_dir,
    scope_line: str,
):
    import build_guardrails

    (fake_knowledge_dir / "invalid-scope.md").write_text(
        "---\n"
        "type: pattern\n"
        f"{scope_line}\n"
        "---\n\n"
        "# Invalid scope\n\n"
        "One-sentence summary: Always hide INVALID_SCOPE_SENTINEL.\n",
        encoding="utf-8",
    )

    assert build_guardrails._collect_corrections("beta") == []
    assert build_guardrails._collect_corrections(None) == []


def test_present_invalid_advisory_scope_is_never_global(monkeypatch, tmp_path: Path):
    import build_advisory

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    (knowledge / "invalid-decision.md").write_text(
        "---\n"
        "type: decision\n"
        "timestamp: 2026-07-28\n"
        'project: "beta\n'
        "---\n\n"
        "# Invalid scoped decision\n\n"
        "One-sentence summary: INVALID_DECISION_SENTINEL.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)

    assert build_advisory._find_last_decision("beta") is None
    assert build_advisory._find_last_decision(None) is None


def test_invalid_utf8_cannot_forge_guardrail_or_advisory_scope(
    fake_knowledge_dir,
    monkeypatch,
    tmp_path: Path,
):
    import build_advisory
    import build_guardrails

    guardrail = fake_knowledge_dir / "forged-rule.md"
    guardrail_bytes = (
        b"---\n"
        b"type: pattern\n"
        b"pro\xffject: alpha\n"
        b"---\n\n"
        b"# Forged rule\n\n"
        b"One-sentence summary: Always expose FORGED_GUARDRAIL_SENTINEL.\n"
    )
    guardrail.write_bytes(guardrail_bytes)
    decision = fake_knowledge_dir / "forged-decision.md"
    decision_bytes = (
        b"---\n"
        b"type: decision\n"
        b"timestamp: 2026-07-29\n"
        b"pro\xffject: alpha\n"
        b"---\n\n"
        b"# Forged decision\n\n"
        b"One-sentence summary: FORGED_ADVISORY_SENTINEL.\n"
    )
    decision.write_bytes(decision_bytes)
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", fake_knowledge_dir)
    monkeypatch.setattr(build_advisory, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(build_advisory, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)

    assert "project: alpha" in guardrail_bytes.decode("utf-8", errors="ignore")
    assert "project: alpha" in decision_bytes.decode("utf-8", errors="ignore")
    guardrails = build_guardrails.build_guardrails("alpha") or ""
    advisory = build_advisory.build_advisory("alpha")

    assert "FORGED_GUARDRAIL_SENTINEL" not in guardrails
    assert "FORGED_ADVISORY_SENTINEL" not in advisory
    assert build_guardrails._read_text_bounded(
        guardrail,
        build_guardrails.MAX_NOTE_BYTES,
    ) is None
    assert build_advisory._read_text_bounded(
        decision,
        build_advisory.MAX_NOTE_BYTES,
    ) is None


def test_guardrail_note_reads_request_an_explicit_byte_bound(
    fake_knowledge_dir,
    monkeypatch,
):
    import build_guardrails

    note = fake_knowledge_dir / "bounded-rule.md"
    make_page(note, "pattern", "Bounded", "Always keep BOUNDED_RULE_SENTINEL")
    real_open = Path.open
    real_read_text = Path.read_text
    read_sizes: list[int] = []

    class TrackingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def reject_read_text(path, *args, **kwargs):
        if path == note:
            raise AssertionError("guardrail note read must be byte-bounded")
        return real_read_text(path, *args, **kwargs)

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == note and "r" in mode:
            assert "b" in mode
            return TrackingFile(handle)
        return handle

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    monkeypatch.setattr(Path, "open", tracking_open)

    corrections = build_guardrails._collect_corrections()

    assert corrections[0]["summary"] == "Always keep BOUNDED_RULE_SENTINEL"
    assert read_sizes and all(size > 0 for size in read_sizes)


def test_guardrail_entry_cap_counts_nonmatching_files_and_reports_unavailable(
    fake_knowledge_dir,
    monkeypatch,
):
    import build_guardrails
    import session_start_context

    for index in range(3):
        (fake_knowledge_dir / f"ignored-{index}.txt").write_text(
            "not a note\n",
            encoding="utf-8",
        )
    make_page(
        fake_knowledge_dir / "rule.md",
        "pattern",
        "Rule",
        "Always keep this rule",
    )
    monkeypatch.setattr(build_guardrails, "MAX_NOTE_FILES_SCANNED", 2, raising=False)

    assert build_guardrails._collect_corrections() is None
    assert build_guardrails.build_guardrails() is None
    block = session_start_context.guardrails_block()
    assert "inventory unavailable" in block.lower()
    assert "no learned guardrails" not in block.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("project", ["alpha"], id="list-project"),
        pytest.param("text", ["TYPE_CONFUSED_TEXT"], id="list-text"),
        pytest.param("status", ["promoted"], id="list-status"),
        pytest.param("type", ["correction"], id="list-type"),
        pytest.param("promoted_to", ["knowledge/notes/bad.md"], id="list-path"),
    ),
)
def test_type_confused_feedback_is_skipped_without_hiding_valid_neighbor(
    fake_knowledge_dir,
    field,
    value,
):
    import build_guardrails

    feedback = build_guardrails.FEEDBACK_DIR
    make_promoted_feedback(feedback / "00-malformed.json", "MALFORMED_SENTINEL", **{field: value})
    make_promoted_feedback(feedback / "01-valid.json", "Always keep VALID_NEIGHBOR_VISIBLE")

    guardrails = build_guardrails.build_guardrails("alpha")

    assert "VALID_NEIGHBOR_VISIBLE" in guardrails
    assert "MALFORMED_SENTINEL" not in guardrails
    assert "TYPE_CONFUSED_TEXT" not in guardrails


def test_deep_two_kilobyte_feedback_is_isolated_from_sessionstart_sources(
    fake_knowledge_dir,
    monkeypatch,
    tmp_path,
):
    import build_guardrails
    import session_start_context

    feedback = build_guardrails.FEEDBACK_DIR
    depth = 1_050
    deep = (
        '{"status":"promoted","project":"alpha","type":"correction",'
        '"text":"DEEP_MALFORMED_SENTINEL","promoted_to":"bad.md","nested":'
        + "[" * depth
        + "0"
        + "]" * depth
        + "}"
    )
    assert 2_000 <= len(deep.encode("utf-8")) <= 2_500
    (feedback / "00-deep.json").write_text(deep, encoding="utf-8")
    make_promoted_feedback(feedback / "01-valid.json", "Always retain VALID_GUARDRAIL")
    make_page(
        fake_knowledge_dir / "advisory.md",
        "decision",
        "Valid advisory",
        "Always retain VALID_ADVISORY",
    )
    state_path = tmp_path / "state.md"
    state_path.write_text("# State\n", encoding="utf-8")
    monkeypatch.setattr(
        session_start_context,
        "_resolve_project",
        lambda _active: ("alpha", state_path),
    )
    monkeypatch.setattr(session_start_context, "_recent_daily_paths", lambda: [])
    monkeypatch.setattr(session_start_context, "metacognitive_block", lambda: "## Health\n\nVALID_GLOBAL_HEALTH")
    monkeypatch.setattr(session_start_context, "advisory_block", lambda *_args: "## Advisory\n\nVALID_ADVISORY")
    monkeypatch.setattr(session_start_context, "MEMORY_INDEX", tmp_path / "missing-index.md")
    monkeypatch.setattr(session_start_context, "MEMORY_LOG", tmp_path / "missing-log.md")

    context = session_start_context.build_context(tmp_path / "alpha")

    assert "VALID_GUARDRAIL" in context
    assert "VALID_ADVISORY" in context
    assert "VALID_GLOBAL_HEALTH" in context
    assert "DEEP_MALFORMED_SENTINEL" not in context
    assert not build_guardrails.GUARDRAILS_FILE.exists()


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param(b'{"status":"promoted",', id="malformed-json"),
        pytest.param(b"\xff{\"status\":\"promoted\"}", id="invalid-utf8"),
        pytest.param(b'["promoted"]', id="top-level-array"),
        pytest.param(
            b'{"status":"promoted","project":"alpha","type":"correction",'
            b'"text":"SURROGATE_\\ud800_SENTINEL","promoted_to":"bad.md"}',
            id="surrogate",
        ),
    ),
)
def test_malformed_feedback_file_has_no_effect_on_valid_neighbor(
    fake_knowledge_dir,
    raw,
):
    import build_guardrails

    feedback = build_guardrails.FEEDBACK_DIR
    malformed = feedback / "00-malformed.json"
    malformed.write_bytes(raw)
    valid = feedback / "01-valid.json"
    make_promoted_feedback(valid, "Always preserve VALID_NEIGHBOR_RULE")
    before = {path.name: path.read_bytes() for path in feedback.iterdir()}

    guardrails = build_guardrails.build_guardrails("alpha")

    assert "VALID_NEIGHBOR_RULE" in guardrails
    assert "SURROGATE_" not in guardrails
    assert {path.name: path.read_bytes() for path in feedback.iterdir()} == before
    assert not build_guardrails.GUARDRAILS_FILE.exists()


def test_guardrails_isolate_feedback_file_memory_error(
    fake_knowledge_dir,
    monkeypatch,
):
    import build_guardrails

    feedback = build_guardrails.FEEDBACK_DIR
    hostile = feedback / "00-hostile.json"
    make_promoted_feedback(hostile, "HOSTILE_READ_MUST_NOT_ESCAPE")
    make_promoted_feedback(
        feedback / "01-valid.json",
        "Always preserve VALID_MEMORY_ERROR_NEIGHBOR",
    )
    real_open = Path.open

    def fail_hostile_read(path, *args, **kwargs):
        if path == hostile:
            raise MemoryError("injected feedback read exhaustion")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_hostile_read)

    guardrails = build_guardrails.build_guardrails("alpha")

    assert "VALID_MEMORY_ERROR_NEIGHBOR" in guardrails
    assert "HOSTILE_READ_MUST_NOT_ESCAPE" not in guardrails


def test_advisory_sources_use_explicit_byte_bounds(monkeypatch, tmp_path: Path):
    import build_advisory

    knowledge = tmp_path / "knowledge"
    reports = tmp_path / "reports"
    projects = tmp_path / "projects"
    knowledge.mkdir()
    reports.mkdir()
    state = projects / "legacy folder" / "state.md"
    state.parent.mkdir(parents=True)
    note = knowledge / "decision.md"
    report = reports / "lint-2026-07-28.md"
    note.write_text(
        "---\ntype: decision\ntimestamp: 2026-07-28\nproject: alpha\n---\n\n"
        "# Bounded decision\n\nOne-sentence summary: BOUNDED_DECISION\n",
        encoding="utf-8",
    )
    report.write_text(
        "## Broken Wikilinks\n- BOUNDED_LINT_ALERT\n",
        encoding="utf-8",
    )
    state.write_text(
        '- Project root JSON: "D:/alpha"\n'
        '- Runtime slug JSON: "alpha"\n'
        "## Open threads\n- BOUNDED_STATE_THREAD\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(build_advisory, "REPORTS_DIR", reports)
    monkeypatch.setattr(build_advisory, "PROJECTS_DIR", projects)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)
    real_open = Path.open
    real_read_text = Path.read_text
    bounded_paths = {note, report, state}
    read_sizes: dict[Path, list[int]] = {path: [] for path in bounded_paths}

    class TrackingFile:
        def __init__(self, path, handle):
            self.path = path
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            read_sizes[self.path].append(size)
            return self.handle.read(size)

    def reject_read_text(path, *args, **kwargs):
        if path in bounded_paths:
            raise AssertionError(f"unbounded advisory read: {path.name}")
        return real_read_text(path, *args, **kwargs)

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path in bounded_paths and "r" in mode:
            assert "b" in mode
            return TrackingFile(path, handle)
        return handle

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    monkeypatch.setattr(Path, "open", tracking_open)

    assert build_advisory._find_last_decision("alpha")["title"] == "Bounded decision"
    assert build_advisory._find_contradictions() == ["BOUNDED_LINT_ALERT"]
    assert build_advisory._read_open_threads("alpha", state) == ["BOUNDED_STATE_THREAD"]
    assert all(sizes and all(size > 0 for size in sizes) for sizes in read_sizes.values())


@pytest.mark.parametrize(
    ("finder_name", "args", "safe_result"),
    (
        ("_find_last_decision", ("alpha",), None),
        ("_find_cross_project_insights", ("alpha",), []),
        ("_find_stale_pages", (), 0),
    ),
)
def test_advisory_note_scan_overflow_fails_closed(
    monkeypatch,
    tmp_path: Path,
    finder_name,
    args,
    safe_result,
):
    import build_advisory

    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    for index in range(3):
        (knowledge / f"ignored-{index}.txt").write_text(
            "not a note\n",
            encoding="utf-8",
        )
    (knowledge / "decision.md").write_text(
        "---\ntype: decision\ntimestamp: 2026-07-28\nproject: alpha\n---\n\n"
        "# Decision\n\nOne-sentence summary: Decision\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(build_advisory, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)
    monkeypatch.setattr(build_advisory, "MAX_NOTE_FILES_SCANNED", 2, raising=False)

    assert getattr(build_advisory, finder_name)(*args) == safe_result
    advisory = build_advisory.build_advisory("alpha")
    assert "knowledge inventory unavailable" in advisory.lower()


def test_advisory_lint_report_scan_overflow_fails_closed(monkeypatch, tmp_path: Path):
    import build_advisory

    reports = tmp_path / "reports"
    reports.mkdir()
    for index in range(3):
        (reports / f"ignored-{index}.txt").write_text(
            "not a report\n",
            encoding="utf-8",
        )
    (reports / "lint-2026-07-28.md").write_text(
        "## Broken Wikilinks\n- alert\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "REPORTS_DIR", reports)
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", tmp_path / "knowledge")
    monkeypatch.setattr(build_advisory, "MAX_LINT_REPORTS_SCANNED", 2, raising=False)

    assert build_advisory._find_contradictions() == []
    advisory = build_advisory.build_advisory("alpha")
    assert "lint report inventory unavailable" in advisory.lower()


def test_build_guardrails_formats_output(fake_knowledge_dir):
    """build_guardrails produces formatted markdown."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/c1.md",
              "pattern", "Use JWT", "Always use JWT instead of sessions for auth")
    make_page(fake_knowledge_dir / "patterns/p1.md",
              "decision", "Short answers", "I prefer concise responses")

    result = build_guardrails.build_guardrails()
    assert "Guard rails" in result


def test_build_guardrails_empty_returns_empty(fake_knowledge_dir):
    """No corrections → empty string."""
    import build_guardrails

    assert build_guardrails.build_guardrails() == ""


def test_build_guardrails_dedup(fake_knowledge_dir):
    """Duplicate summaries are deduplicated."""
    import build_guardrails

    make_page(fake_knowledge_dir / "patterns/c1.md",
              "pattern", "A", "Always use JWT instead of sessions for auth")
    make_page(fake_knowledge_dir / "patterns/c2.md",
              "pattern", "B", "Always use JWT instead of sessions for auth")  # same summary

    result = build_guardrails.build_guardrails()
    # Should appear only once after dedup
    assert result.count("Always use JWT") == 1
