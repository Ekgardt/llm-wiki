"""Tests for contextual_retrieval.py."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from contextual_retrieval import generate_context, get_context  # noqa: E402
from corpus_snapshot import (  # noqa: E402
    CapturedSource,
    CorpusSnapshot,
    SnapshotPolicy,
    SourceMetadata,
    SourceRecord,
)


def _source(
    relative_path: str,
    content: bytes,
    *,
    type_name: str = "concept",
    project: str | None = None,
    logical_id: str | None = None,
) -> CapturedSource:
    digest = hashlib.sha256(content).hexdigest()
    return CapturedSource(
        SourceRecord(
            logical_id=logical_id or f"source:{relative_path}",
            relative_path=relative_path,
            sha256=digest,
            size=len(content),
            media_type="text/markdown",
            language="en",
            git_oid=None,
        ),
        SourceMetadata(type=type_name, project=project, authority="user", confidence="high"),
        content,
    )


def _snapshot(*sources: CapturedSource) -> CorpusSnapshot:
    policy = SnapshotPolicy((), (), False, None, 100, 1024 * 1024, 1024 * 1024, 100, 100, 8)
    return CorpusSnapshot(tuple(sources), (), "a" * 64, policy)


class TestGenerateContext:
    """Test context generation."""

    def test_deterministic_context(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "auth.md"
        page.write_text(
            "---\ntype: decision\nproject: llm-wiki\n---\n\n"
            "# Auth Decision\n\n"
            "One-sentence summary: We chose JWT over sessions.\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)

        ctx = generate_context("auth", use_llm=False)
        assert "Auth Decision" in ctx
        assert "JWT" in ctx
        assert "llm-wiki" in ctx

    def test_legacy_generation_defaults_to_deterministic_and_rejects_llm_opt_in(
        self, tmp_path, monkeypatch
    ):
        import contextual_retrieval
        import llm_client

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "auth.md").write_text("# Auth\n\nLocal context.\n", encoding="utf-8")
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(
            llm_client,
            "call_llm",
            lambda *_args, **_kwargs: pytest.fail("contextual LLM reached before ablation"),
        )

        assert contextual_retrieval.generate_context("auth") == "Topic: Auth."
        with pytest.raises(ValueError, match="ablation"):
            contextual_retrieval.generate_context("auth", use_llm=True)

    def test_context_for_missing_page(self, tmp_path, monkeypatch):
        import contextual_retrieval

        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", tmp_path / "notes")
        ctx = generate_context("nonexistent", use_llm=False)
        assert ctx == ""

    def test_context_includes_type(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "x.md"
        page.write_text(
            "---\ntype: pattern\n---\n\n# X\n\nOne-sentence summary: Test.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)

        ctx = generate_context("x", use_llm=False)
        assert "pattern" in ctx.lower()

    @pytest.mark.parametrize(
        "slug",
        ["", ".", "..", "../secret", "a/b", r"a\b", "/absolute", "C:drive", "Ａ"],
    )
    def test_legacy_generation_rejects_unsafe_or_non_normalized_slug(self, slug):
        with pytest.raises(ValueError, match="normalized safe component"):
            generate_context(slug, use_llm=False)

    def test_legacy_generation_rejects_reparse_knowledge_ancestor(
        self, tmp_path, monkeypatch
    ):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "page.md").write_text("# Page\n", encoding="utf-8")
        original_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.st_file_attributes = 0x400

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            metadata = original_lstat(path)
            return ReparseMetadata(metadata) if path == notes else metadata

        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(Path, "lstat", fake_lstat)

        with pytest.raises(PermissionError, match="unsafe.*ancestor"):
            generate_context("page", use_llm=False)


class TestGetContext:
    """Test cached context retrieval."""

    def test_get_existing_context(self, tmp_path, monkeypatch):
        import contextual_retrieval

        ctx_dir = tmp_path / "ctx"
        ctx_dir.mkdir()
        (ctx_dir / "test.ctx").write_text("Context for test page.", encoding="utf-8")
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", ctx_dir)

        result = get_context("test")
        assert result == "Context for test page."

    def test_get_missing_context(self, tmp_path, monkeypatch):
        import contextual_retrieval

        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", tmp_path / "ctx")
        result = get_context("nonexistent")
        assert result is None

    def test_legacy_mode_rejects_captured_source(self):
        source = _source("knowledge/notes/page.md", b"# Page\n")

        with pytest.raises(TypeError, match="legacy.*string slug"):
            get_context(source)

    def test_generation_mode_rejects_string_slug(self, tmp_path):
        with pytest.raises(TypeError, match="CapturedSource"):
            get_context("page", generation_dir=tmp_path / "generation")

    @pytest.mark.parametrize(
        "slug",
        ["", ".", "..", "../secret", "a/b", r"a\b", "/absolute", "C:drive", "Ａ"],
    )
    def test_legacy_lookup_rejects_unsafe_or_non_normalized_slug(self, slug):
        with pytest.raises(ValueError, match="normalized safe component"):
            get_context(slug)


class TestBuildAll:
    """Test batch context generation."""

    def test_build_all_deterministic(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text(
            "---\ntype: concept\n---\n\n# A\n\nOne-sentence summary: Page A.\n",
            encoding="utf-8",
        )
        (notes / "b.md").write_text(
            "---\ntype: decision\n---\n\n# B\n\nOne-sentence summary: Page B.\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", tmp_path / "ctx")

        stats = contextual_retrieval.build_all_contexts(use_llm=False, verbose=False)
        assert stats["generated"] == 2
        # Task 15: cache files are hash-suffixed by source SHA-256.
        ctx_files = sorted((tmp_path / "ctx").glob("*.ctx"))
        assert len(ctx_files) == 2
        assert all(not path.name.endswith("a.ctx") for path in ctx_files)
        assert all("." in path.stem for path in ctx_files)

    def test_build_all_defaults_to_deterministic_and_rejects_llm_opt_in(
        self, tmp_path, monkeypatch
    ):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text("# A\n", encoding="utf-8")
        cache = tmp_path / "ctx"
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", cache)

        assert contextual_retrieval.build_all_contexts(verbose=False)["generated"] == 1
        with pytest.raises(ValueError, match="ablation"):
            contextual_retrieval.build_all_contexts(use_llm=True, verbose=False)

    def test_duplicate_stems_write_distinct_hash_qualified_contexts(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        (notes / "one").mkdir(parents=True)
        (notes / "two").mkdir(parents=True)
        (notes / "one/shared.md").write_text("# One\n\nFirst body.\n", encoding="utf-8")
        (notes / "two/shared.md").write_text("# Two\n\nSecond body.\n", encoding="utf-8")
        cache = tmp_path / "ctx"
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", cache)

        stats = contextual_retrieval.build_all_contexts(use_llm=False, verbose=False)

        assert stats == {"generated": 2, "skipped": 0, "errors": 0}
        outputs = sorted(cache.glob("*.ctx"))
        assert len(outputs) == 2
        assert {path.read_text(encoding="utf-8") for path in outputs} == {
            "Topic: One.",
            "Topic: Two.",
        }

    def test_hash_qualified_reader_consumes_newly_written_context(self, tmp_path, monkeypatch):
        import hashlib

        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "auth.md"
        page.write_text("# Auth\n\nAuth body.\n", encoding="utf-8")
        digest = hashlib.sha256(page.read_bytes()).hexdigest()
        cache = tmp_path / "ctx"
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", cache)
        contextual_retrieval.build_all_contexts(use_llm=False, verbose=False)

        assert contextual_retrieval.get_context(
            "auth", source_sha256=digest, logical_path="auth.md"
        ) == "Topic: Auth."

    def test_hash_qualified_read_never_falls_back_to_stale_legacy(self, tmp_path, monkeypatch):
        import contextual_retrieval

        cache = tmp_path / "ctx"
        cache.mkdir()
        (cache / "auth.ctx").write_text("STALE", encoding="utf-8")
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", cache)

        assert contextual_retrieval.get_context(
            "auth", source_sha256="a" * 64, logical_path="auth.md"
        ) is None

    def test_legacy_cache_rejects_unsafe_logical_paths(self, tmp_path, monkeypatch):
        import contextual_retrieval
        import pytest

        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", tmp_path)
        for path in ("../auth.md", "C:/auth.md", "con.md", "dir/NUL.md"):
            with pytest.raises(ValueError):
                contextual_retrieval.legacy_context_cache_path(
                    "auth", source_sha256="a" * 64, logical_path=path
                )

    def test_build_all_rejects_reparse_cache_before_write(self, tmp_path, monkeypatch):
        import contextual_retrieval

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text("# A\n", encoding="utf-8")
        context_dir = tmp_path / "contextual"
        context_dir.mkdir()
        original_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.st_file_attributes = 0x400

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            metadata = original_lstat(path)
            return ReparseMetadata(metadata) if path == context_dir else metadata

        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", context_dir)
        monkeypatch.setattr(Path, "lstat", fake_lstat)

        with pytest.raises(PermissionError, match="unsafe.*ancestor"):
            contextual_retrieval.build_all_contexts(use_llm=False, verbose=False)

        assert list(context_dir.iterdir()) == []


class TestSnapshotContexts:
    def test_source_generation_uses_captured_bytes_without_live_reread(self, tmp_path, monkeypatch):
        import contextual_retrieval

        live = tmp_path / "knowledge/notes/page.md"
        live.parent.mkdir(parents=True)
        captured = b"---\ntype: concept\n---\n# Captured\nOne-sentence summary: Old bytes.\n"
        live.write_bytes(captured)
        source = _source("knowledge/notes/page.md", captured)
        live.write_text("# Changed live content", encoding="utf-8")
        monkeypatch.setattr(contextual_retrieval, "KNOWLEDGE_DIR", live.parent)
        monkeypatch.setattr(
            Path,
            "read_text",
            lambda *_args, **_kwargs: pytest.fail("snapshot consumer reread a live file"),
        )

        context = contextual_retrieval.generate_context_for_source(source, use_llm=False)

        assert "Captured" in context
        assert "Old bytes" in context
        assert "Changed" not in context

    def test_context_key_uses_logical_id_hash_and_extractor_version(self):
        import contextual_retrieval

        first = _source("knowledge/notes/concepts/shared.md", b"# First\n")
        duplicate_stem = _source("knowledge/notes/decisions/shared.md", b"# First\n")
        changed = _source("knowledge/notes/concepts/shared.md", b"# Other\n")

        first_key = contextual_retrieval.context_artifact_key(first)

        assert first_key != contextual_retrieval.context_artifact_key(duplicate_stem)
        assert first_key != contextual_retrieval.context_artifact_key(changed)
        assert first_key != contextual_retrieval.context_artifact_key(
            first, extractor_version="context-extractor/v-next"
        )
        assert first_key == contextual_retrieval.context_artifact_key(first)

    def test_batch_uses_exact_snapshot_membership_and_distinguishes_duplicate_stems(self, tmp_path):
        import contextual_retrieval

        sources = (
            _source("knowledge/notes/concepts/shared.md", b"# Concept Shared\n"),
            _source("knowledge/notes/decisions/shared.md", b"# Decision Shared\n"),
        )
        generation = tmp_path / "generation-1"
        generation.mkdir()

        descriptors = contextual_retrieval.build_snapshot_contexts(
            _snapshot(*sources), generation, use_llm=False
        )

        assert len(descriptors) == 2
        assert descriptors == sorted(descriptors, key=lambda item: item["path"])
        assert all(set(item) == {"path", "size", "sha256"} for item in descriptors)
        payloads = [json.loads((generation / item["path"]).read_bytes()) for item in descriptors]
        assert {item["source"]["logical_id"] for item in payloads} == {
            source.record.logical_id for source in sources
        }
        assert len({item["key"] for item in payloads}) == 2
        assert not (generation / "shared.ctx").exists()

    @pytest.mark.parametrize(
        ("first_id", "second_id"),
        [
            ("source:same", "source:same"),
            ("source:Page", "source:page"),
            ("source:Ａ", "source:A"),
        ],
    )
    def test_batch_rejects_duplicate_or_normalized_logical_ids(
        self, tmp_path, first_id, second_id
    ):
        import contextual_retrieval

        sources = (
            _source("knowledge/notes/a.md", b"# A\n", logical_id=first_id),
            _source("knowledge/notes/b.md", b"# B\n", logical_id=second_id),
        )
        generation = tmp_path / "generation-1"
        generation.mkdir()

        with pytest.raises(ValueError, match="logical ID collision"):
            contextual_retrieval.build_snapshot_contexts(_snapshot(*sources), generation)

        assert list(generation.iterdir()) == []

    def test_artifact_records_captured_metadata_provenance(self, tmp_path):
        import contextual_retrieval

        source = _source(
            "knowledge/notes/page.md",
            b"---\ntype: decision\nproject: alpha\n---\n# Page\n",
            type_name="decision",
            project="alpha",
        )
        generation = tmp_path / "generation-1"
        generation.mkdir()

        [descriptor] = contextual_retrieval.build_snapshot_contexts(
            _snapshot(source), generation, use_llm=False
        )

        raw = (generation / descriptor["path"]).read_bytes()
        artifact = json.loads(raw)
        assert descriptor == {
            "path": descriptor["path"],
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        assert artifact["source"] == {
            "authority": "user",
            "confidence": "high",
            "git_oid": None,
            "language": "en",
            "logical_id": "source:knowledge/notes/page.md",
            "media_type": "text/markdown",
            "project": "alpha",
            "relative_path": "knowledge/notes/page.md",
            "sha256": source.record.sha256,
            "size": len(source.content),
            "status": "active",
            "type": "decision",
            "valid_from": None,
            "valid_to": None,
        }

    def test_generation_lookup_uses_source_key_without_legacy_fallback(
        self, tmp_path, monkeypatch
    ):
        import contextual_retrieval

        source = _source("knowledge/notes/concepts/shared.md", b"# Captured\n")
        other = _source("knowledge/notes/decisions/shared.md", b"# Other\n")
        generation = tmp_path / "generation-1"
        generation.mkdir()
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "shared.ctx").write_text("legacy collision", encoding="utf-8")
        monkeypatch.setattr(contextual_retrieval, "CONTEXT_DIR", legacy)
        contextual_retrieval.build_snapshot_contexts(
            _snapshot(source), generation, use_llm=False
        )

        assert "Captured" in contextual_retrieval.get_context(
            source, generation_dir=generation
        )
        assert contextual_retrieval.get_context(other, generation_dir=generation) is None
        assert contextual_retrieval.get_context("shared") == "legacy collision"

    def test_context_keys_include_mode_and_full_model_descriptor_revision(self):
        import contextual_retrieval
        from llm_client import ProviderDescriptor

        source = _source("knowledge/notes/page.md", b"# Captured\n")
        descriptor = ProviderDescriptor(
            provider="fake",
            model="model-a",
            capabilities={"json": True},
            inference_settings={"temperature": "0"},
            candidate_index=2,
            fallback_from=("openai:model-b",),
        )
        deterministic = contextual_retrieval.legacy_context_cache_path(
            "page", source_sha256=source.record.sha256, logical_path="page.md"
        )
        llm = contextual_retrieval.legacy_context_cache_path(
            "page",
            source_sha256=source.record.sha256,
            logical_path="page.md",
            generation_mode="llm",
            model_descriptor=descriptor,
            model_revision="revision-1",
        )

        assert deterministic != llm
        assert llm != contextual_retrieval.legacy_context_cache_path(
            "page",
            source_sha256=source.record.sha256,
            logical_path="page.md",
            generation_mode="llm",
            model_descriptor=descriptor,
            model_revision="revision-2",
        )
        with pytest.raises(ValueError, match="source_sha256"):
            contextual_retrieval.legacy_context_cache_path(
                "page",
                generation_mode="llm",
                model_descriptor=descriptor,
                model_revision="revision-1",
            )

    def test_snapshot_context_llm_opt_in_is_rejected_until_ablation(self, tmp_path):
        import contextual_retrieval
        from llm_client import ProviderDescriptor

        source = _source("knowledge/notes/page.md", b"# Captured\n")
        generation = tmp_path / "generation"
        generation.mkdir()

        with pytest.raises(ValueError, match="ablation"):
            contextual_retrieval.build_snapshot_contexts(
                _snapshot(source),
                generation,
                use_llm=True,
                max_prompt_bytes=4096,
                max_prompt_chars=4096,
                disclosure_policy="local",
                model_descriptor=ProviderDescriptor(
                    provider="fake",
                    model="model-a",
                    capabilities={},
                    inference_settings={},
                    candidate_index=0,
                    fallback_from=(),
                ),
                model_revision="revision-1",
            )
        assert list(generation.iterdir()) == []

    def test_generation_context_defaults_to_deterministic(self, monkeypatch):
        import contextual_retrieval
        import llm_client

        source = _source("knowledge/notes/page.md", b"# Captured default\n")
        monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
        monkeypatch.setattr(
            llm_client,
            "call_llm",
            lambda *_args, **_kwargs: pytest.fail("generation LLM was not opted in"),
        )

        assert "Captured default" in contextual_retrieval.generate_context_for_source(source)

    def test_source_llm_opt_in_is_rejected_until_ablation(self, monkeypatch):
        import contextual_retrieval
        import llm_client

        source = _source("knowledge/notes/page.md", b"# Page\n")
        monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
        monkeypatch.setattr(
            llm_client,
            "call_llm",
            lambda *_args, **_kwargs: pytest.fail("invalid opt-in reached the LLM"),
        )

        with pytest.raises(ValueError, match="ablation"):
            contextual_retrieval.generate_context_for_source(source, use_llm=True)
    def test_generation_read_rejects_malformed_source_object(self, tmp_path):
        import contextual_retrieval

        source = _source("knowledge/notes/page.md", b"# Page\n")
        generation = tmp_path / "generation-1"
        generation.mkdir()
        [descriptor] = contextual_retrieval.build_snapshot_contexts(_snapshot(source), generation)
        artifact_path = generation / descriptor["path"]
        artifact = json.loads(artifact_path.read_bytes())
        artifact["source"] = []
        artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

        with pytest.raises(ValueError, match="context artifact is invalid"):
            contextual_retrieval.get_context(source, generation_dir=generation)

    def test_generation_read_and_write_reject_symlink_ancestors(self, tmp_path, monkeypatch):
        import contextual_retrieval

        source = _source("knowledge/notes/page.md", b"# Page\n")
        parent = tmp_path / "linked"
        generation = parent / "generation-1"
        generation.mkdir(parents=True)
        original_lstat = Path.lstat

        class SymlinkMetadata:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.st_mode = stat.S_IFLNK | (wrapped.st_mode & 0o777)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            metadata = original_lstat(path)
            return SymlinkMetadata(metadata) if path == parent else metadata

        monkeypatch.setattr(Path, "lstat", fake_lstat)

        with pytest.raises(PermissionError, match="unsafe.*ancestor"):
            contextual_retrieval.build_snapshot_contexts(_snapshot(source), generation)
        with pytest.raises(PermissionError, match="unsafe.*ancestor"):
            contextual_retrieval.get_context(source, generation_dir=generation)

        assert list(generation.iterdir()) == []

    def test_generation_read_rejects_contextual_symlink(self, tmp_path, monkeypatch):
        import contextual_retrieval

        source = _source("knowledge/notes/page.md", b"# Page\n")
        generation = tmp_path / "generation-1"
        contextual = generation / "contextual"
        contextual.mkdir(parents=True)
        original_lstat = Path.lstat

        class SymlinkMetadata:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.st_mode = stat.S_IFLNK | (wrapped.st_mode & 0o777)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            metadata = original_lstat(path)
            return SymlinkMetadata(metadata) if path == contextual else metadata

        monkeypatch.setattr(Path, "lstat", fake_lstat)

        with pytest.raises(PermissionError, match="contextual"):
            contextual_retrieval.get_context(source, generation_dir=generation)
        with pytest.raises(PermissionError, match="contextual"):
            contextual_retrieval.build_snapshot_contexts(_snapshot(source), generation)

    def test_generation_write_rejects_reparse_ancestor(self, tmp_path, monkeypatch):
        import contextual_retrieval

        source = _source("knowledge/notes/page.md", b"# Page\n")
        parent = tmp_path / "parent"
        generation = parent / "generation-1"
        generation.mkdir(parents=True)
        original_lstat = Path.lstat

        class ReparseMetadata:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.st_file_attributes = 0x400

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def fake_lstat(path):
            metadata = original_lstat(path)
            return ReparseMetadata(metadata) if path == parent else metadata

        monkeypatch.setattr(Path, "lstat", fake_lstat)

        with pytest.raises(PermissionError, match="unsafe.*ancestor"):
            contextual_retrieval.build_snapshot_contexts(_snapshot(source), generation)

        assert list(generation.iterdir()) == []

    def test_batch_failure_publishes_no_partial_output(self, tmp_path, monkeypatch):
        import contextual_retrieval

        sources = (
            _source("knowledge/notes/a.md", b"# A\n"),
            _source("knowledge/notes/b.md", b"# B\n"),
        )
        generation = tmp_path / "generation-1"
        generation.mkdir()
        original = contextual_retrieval.generate_context_for_source
        calls = 0

        def fail_second(source, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("LLM failed")
            return original(source, **kwargs)

        monkeypatch.setattr(contextual_retrieval, "generate_context_for_source", fail_second)

        with pytest.raises(RuntimeError, match="LLM failed"):
            contextual_retrieval.build_snapshot_contexts(
                _snapshot(*sources), generation, use_llm=False
            )

        assert not (generation / "contextual").exists()
        assert list(generation.iterdir()) == []

    def test_batch_refuses_to_overwrite_generation_output(self, tmp_path):
        import contextual_retrieval

        source = _source("knowledge/notes/a.md", b"# A\n")
        generation = tmp_path / "generation-1"
        (generation / "contextual").mkdir(parents=True)
        marker = generation / "contextual/marker"
        marker.write_text("keep", encoding="utf-8")

        with pytest.raises(FileExistsError, match="contextual"):
            contextual_retrieval.build_snapshot_contexts(
                _snapshot(source), generation, use_llm=False
            )

        assert marker.read_text(encoding="utf-8") == "keep"


def test_contextual_cli_defaults_to_deterministic_and_rejects_llm_flag(monkeypatch):
    import contextual_retrieval

    calls = []
    monkeypatch.setattr(contextual_retrieval, "build_all_contexts", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["contextual_retrieval.py", "--all"])
    assert contextual_retrieval.main() == 0
    assert calls == [{"use_llm": False}]

    monkeypatch.setattr(sys, "argv", ["contextual_retrieval.py", "--all", "--llm"])
    with pytest.raises(SystemExit):
        contextual_retrieval.main()
