"""Tests for build_guardrails.py — rule extraction and dedup."""
from __future__ import annotations

import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def fake_knowledge_dir(tmp_path, monkeypatch):
    """Set up a temporary knowledge directory."""
    import build_guardrails

    knowledge = tmp_path / "knowledge" / "notes"
    knowledge.mkdir(parents=True)
    monkeypatch.setattr(build_guardrails, "KNOWLEDGE", knowledge)
    monkeypatch.setattr(
        build_guardrails, "FEEDBACK_DIR", tmp_path / "knowledge" / "feedback"
    )
    monkeypatch.setattr(
        build_guardrails, "GUARDRAILS_FILE", tmp_path / "knowledge" / "guardrails.md"
    )
    monkeypatch.setattr(build_guardrails, "ROOT", tmp_path)
    (tmp_path / "knowledge" / "feedback").mkdir()
    return knowledge


def make_page(path: Path, page_type: str, title: str, summary: str):
    """Create a knowledge page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {page_type}\n---\n\n# {title}\n\nOne-sentence summary: {summary}\n\n## Body\nContent.\n",
        encoding="utf-8",
    )


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


def test_apply_delegates_guardrails_replacement_with_stable_precondition(
    fake_knowledge_dir, monkeypatch
):
    import build_guardrails
    from markdown_transaction import stable_operation_id
    from reliable_memory import sha256_bytes

    make_page(
        fake_knowledge_dir / "patterns/c1.md",
        "pattern",
        "Use JWT",
        "Always use JWT instead of sessions for auth",
    )
    before = b"old guardrails\n"
    build_guardrails.GUARDRAILS_FILE.write_bytes(before)
    fixed_now = datetime(2026, 7, 16, 12, 0, 0)
    monkeypatch.setattr(
        build_guardrails,
        "datetime",
        SimpleNamespace(now=lambda: fixed_now),
    )
    calls = []

    def boundary(operation_id, changes, **kwargs):
        calls.append((operation_id, changes, kwargs))
        return SimpleNamespace(id="tx", state="committed")

    monkeypatch.setattr(build_guardrails, "mutate_knowledge", boundary, raising=False)
    monkeypatch.setattr(sys, "argv", ["build_guardrails.py", "--apply"])

    assert build_guardrails.main() == 0

    assert len(calls) == 1
    operation_id, changes, kwargs = calls[0]
    content = changes[build_guardrails.GUARDRAILS_FILE]
    before_hash = sha256_bytes(before)
    source_manifest = kwargs["preconditions"]["guardrails_source_manifest"]
    assert source_manifest["entries"] == [
        {
            "path": "knowledge/notes/patterns/c1.md",
            "sha256": sha256_bytes(
                (fake_knowledge_dir / "patterns/c1.md").read_bytes()
            ),
        }
    ]
    assert operation_id == stable_operation_id(
        "build-guardrails",
        f"{before_hash}:{source_manifest['source_manifest_sha256']}",
        content,
    )
    assert kwargs["preconditions"]["knowledge/guardrails.md"] == before_hash


def test_without_apply_does_not_mutate_guardrails(fake_knowledge_dir, monkeypatch):
    import build_guardrails

    make_page(
        fake_knowledge_dir / "patterns/c1.md",
        "pattern",
        "Use JWT",
        "Always use JWT instead of sessions for auth",
    )
    before = b"existing guardrails\n"
    build_guardrails.GUARDRAILS_FILE.write_bytes(before)

    def unexpected_mutation(*args, **kwargs):
        raise AssertionError("non-apply generation must be read-only")

    monkeypatch.setattr(build_guardrails, "mutate_knowledge", unexpected_mutation)
    monkeypatch.setattr(sys, "argv", ["build_guardrails.py"])

    assert build_guardrails.main() == 0
    assert build_guardrails.GUARDRAILS_FILE.read_bytes() == before


def test_apply_rejects_source_mutation_after_guardrails_snapshot(tmp_path, monkeypatch):
    import build_guardrails
    import markdown_transaction

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    feedback = vault / "knowledge" / "feedback"
    notes.mkdir(parents=True)
    feedback.mkdir()
    source = notes / "rule.md"
    make_page(source, "pattern", "Safe storage", "Always use safe storage")
    target = vault / "knowledge" / "guardrails.md"
    before = b"existing guardrails\n"
    target.write_bytes(before)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(build_guardrails, "ROOT", vault)
    monkeypatch.setattr(build_guardrails, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_guardrails, "FEEDBACK_DIR", feedback)
    monkeypatch.setattr(build_guardrails, "GUARDRAILS_FILE", target)
    original_mutate = markdown_transaction.mutate_knowledge

    def race(operation_id, changes, **kwargs):
        source.write_text(
            "---\ntype: pattern\n---\n# Changed\n\n"
            "One-sentence summary: Never use stale guardrails\n",
            encoding="utf-8",
        )
        return original_mutate(operation_id, changes, **kwargs)

    monkeypatch.setattr(build_guardrails, "mutate_knowledge", race)
    monkeypatch.setattr(sys, "argv", ["build_guardrails.py", "--apply"])

    with pytest.raises(
        markdown_transaction.TransactionFailure, match="source manifest precondition"
    ):
        build_guardrails.main()

    assert target.read_bytes() == before


def test_apply_rejects_oversized_guardrails_before_read(tmp_path, monkeypatch):
    import build_guardrails

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    feedback = vault / "knowledge" / "feedback"
    notes.mkdir(parents=True)
    feedback.mkdir()
    make_page(notes / "rule.md", "pattern", "Safe storage", "Always use safe storage")
    target = vault / "knowledge" / "guardrails.md"
    target.write_bytes(b"12345")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(build_guardrails, "ROOT", vault)
    monkeypatch.setattr(build_guardrails, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_guardrails, "FEEDBACK_DIR", feedback)
    monkeypatch.setattr(build_guardrails, "GUARDRAILS_FILE", target)
    monkeypatch.setattr(build_guardrails, "MAX_GUARDRAILS_BYTES", 4, raising=False)
    monkeypatch.setattr(sys, "argv", ["build_guardrails.py", "--apply"])

    with pytest.raises(ValueError, match="guardrails target exceeds 4 bytes"):
        build_guardrails.main()


def test_guardrail_manifest_rejects_canonically_colliding_paths():
    from claim_tree_manifest import validate_guardrail_source_manifest
    from reliable_memory import canonical_json_bytes, sha256_bytes

    composed = "knowledge/notes/caf\u00e9.md"
    decomposed = "knowledge/notes/cafe\u0301.md"
    entries = sorted(
        [
            {"path": composed, "sha256": "a" * 64},
            {"path": decomposed, "sha256": "b" * 64},
        ],
        key=lambda item: item["path"],
    )
    manifest = {
        "schema_version": "guardrails-source-manifest/v1",
        "entries": entries,
        "source_manifest_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }

    with pytest.raises(ValueError, match="normalization collision"):
        validate_guardrail_source_manifest(manifest)


def test_decomposed_guardrail_source_is_normalized_across_recovery(
    tmp_path, monkeypatch
):
    from claim_tree_manifest import snapshot_guardrail_sources_with_content
    from markdown_transaction import MarkdownChange, MarkdownCoordinator

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    notes.mkdir()
    decomposed_name = unicodedata.normalize("NFD", "caf\u00e9.md")
    source = notes / decomposed_name
    source.write_bytes(b"---\ntype: pattern\n---\n# Rule\n")
    target = vault / "knowledge" / "guardrails.md"
    target.write_bytes(b"before")
    manifest, contents = snapshot_guardrail_sources_with_content(vault)
    expected_path = "knowledge/notes/caf\u00e9.md"

    assert manifest["entries"][0]["path"] == expected_path
    assert list(contents) == [expected_path]

    coordinator = MarkdownCoordinator(vault, tmp_path / "state")
    transaction = coordinator.prepare(
        [MarkdownChange.replace("knowledge/guardrails.md", b"after")],
        operation_id="normalized-guardrail-source",
        preconditions={"guardrails_source_manifest": manifest},
    )

    recovered = coordinator.recover()[0]
    assert recovered.id == transaction.id
    assert recovered.state == "committed"


def test_guardrail_source_limit_stops_discovery_early(tmp_path, monkeypatch):
    import claim_tree_manifest

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    feedback = vault / "knowledge" / "feedback"
    notes.mkdir(parents=True)
    feedback.mkdir()
    files = []
    for index in range(4):
        path = notes / f"{index}.md"
        path.write_bytes(b"page")
        files.append(path)
    real_glob = Path.glob

    def guarded_glob(self, pattern):
        if self != notes:
            yield from real_glob(self, pattern)
            return
        for index, path in enumerate(files):
            if index == 3:
                raise AssertionError("source discovery consumed beyond the limit")
            yield path

    monkeypatch.setattr(claim_tree_manifest, "MAX_GUARDRAIL_SOURCE_FILES", 2)
    monkeypatch.setattr(Path, "glob", guarded_glob)

    with pytest.raises(ValueError, match="guardrails sources exceed the file limit"):
        claim_tree_manifest.snapshot_guardrail_sources_with_content(vault)


def test_guardrail_discovery_bounds_nonmatching_entries(tmp_path, monkeypatch):
    import claim_tree_manifest

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    notes.mkdir()
    for index in range(4):
        (notes / f"ignored-{index}.txt").write_bytes(b"ignored")
    monkeypatch.setattr(
        claim_tree_manifest, "MAX_GUARDRAIL_INSPECTED_ENTRIES", 2, raising=False
    )

    with pytest.raises(ValueError, match="inspected entry limit"):
        claim_tree_manifest.snapshot_guardrail_sources_with_content(vault)


def test_guardrail_discovery_bounds_shallow_directories(tmp_path, monkeypatch):
    import claim_tree_manifest

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    notes.mkdir()
    for index in range(4):
        (notes / f"type-{index}").mkdir()
    monkeypatch.setattr(
        claim_tree_manifest, "MAX_GUARDRAIL_SOURCE_DIRECTORIES", 2, raising=False
    )

    with pytest.raises(ValueError, match="directory limit"):
        claim_tree_manifest.snapshot_guardrail_sources_with_content(vault)


def test_guardrail_discovery_rejects_excess_depth_but_supports_typed_subdirectories(
    tmp_path, monkeypatch
):
    import claim_tree_manifest

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    typed = notes / "patterns"
    typed.mkdir(parents=True)
    (typed / "rule.md").write_bytes(b"rule")

    manifest = claim_tree_manifest.snapshot_guardrail_sources(vault)
    assert [entry["path"] for entry in manifest["entries"]] == [
        "knowledge/notes/patterns/rule.md"
    ]

    deep = typed / "too-deep"
    deep.mkdir()
    (deep / "other.md").write_bytes(b"other")
    monkeypatch.setattr(
        claim_tree_manifest, "MAX_GUARDRAIL_SOURCE_DEPTH", 1, raising=False
    )

    with pytest.raises(ValueError, match="depth limit"):
        claim_tree_manifest.snapshot_guardrail_sources_with_content(vault)


def test_guardrail_discovery_does_not_follow_symlink_directories(tmp_path):
    import claim_tree_manifest

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    (vault / "knowledge" / "feedback").mkdir(parents=True)
    notes.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.md").write_bytes(b"escaped")
    link = notes / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    manifest = claim_tree_manifest.snapshot_guardrail_sources(vault)

    assert manifest["entries"] == []
