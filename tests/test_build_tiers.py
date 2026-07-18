"""Tests for build_tiers.py — L0/L1/L2 progressive disclosure."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_tiers import (  # noqa: E402
    _deterministic_l1,
    build_snapshot_tiers,
    generate_l1_for_source,
    get_l0,
    get_l0_for_source,
    get_l2,
    get_l2_for_source,
    get_tier,
    tier_artifact_key,
)
from corpus_snapshot import collect_corpus  # noqa: E402
from llm_client import ProviderDescriptor  # noqa: E402


def _snapshot(tmp_path: Path, pages: dict[str, str]):
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for relative, content in pages.items():
        path = notes / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
    return vault, collect_corpus(vault)


def _model_descriptor(
    provider: str = "openai", model: str = "test-model"
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider=provider,
        model=model,
        capabilities={},
        inference_settings={},
        candidate_index=0,
        fallback_from=(),
    )


@pytest.mark.parametrize(
    "arguments", [("--status",), ("--get", "missing"), ("--slug", "missing")]
)
def test_legacy_cli_starts_without_site_packages(arguments):
    script = Path(__file__).resolve().parent.parent / "scripts/build_tiers.py"

    result = subprocess.run(
        [sys.executable, "-S", str(script), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "No module named 'yaml'" not in result.stderr


class TestL0:
    """Test L0 (one-sentence summary) extraction."""

    def test_l0_from_summary_line(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "test.md"
        page.write_text(
            "---\ntype: concept\n---\n\n"
            "# Test Page\n\n"
            "One-sentence summary: This is a test about auth.\n\n"
            "Body text.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_l0("test")
        assert "This is a test about auth." in result

    def test_l0_fallback_to_first_sentence(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "bare.md"
        page.write_text("# Bare Page\n\nFirst sentence here. Second sentence.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_l0("bare")
        assert "First sentence" in result

    def test_l0_nonexistent_page(self, tmp_path, monkeypatch):
        import build_tiers

        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", tmp_path / "notes")
        result = get_l0("nonexistent")
        assert result == ""


class TestL2:
    """Test L2 (full page content)."""

    def test_l2_reads_full_content(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "full.md"
        page.write_text("# Full Page\n\nAll content here.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_l2("full")
        assert "All content here." in result

    def test_l2_nonexistent(self, tmp_path, monkeypatch):
        import build_tiers

        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", tmp_path / "notes")
        assert get_l2("missing") == ""


class TestDeterministicL1:
    """Test deterministic L1 extraction (no LLM)."""

    def test_extracts_key_sections(self):
        body = (
            "# Page\n\n"
            "One-sentence summary: Test page.\n\n"
            "## Key Points\n\n"
            "- Point A\n"
            "- Point B\n\n"
            "## Details\n\n"
            "Detailed info.\n"
        )
        result = _deterministic_l1("test", body, "Test page.")
        assert "Test page." in result
        assert "Key Points" in result or "Point A" in result

    def test_stops_at_history(self):
        body = (
            "# Page\n\n"
            "Content.\n\n"
            "## History (pre-reflection)\n\n"
            "Old stuff that shouldn't be in L1.\n"
        )
        result = _deterministic_l1("test", body, "Summary.")
        assert "Old stuff" not in result

    def test_truncates_long_content(self):
        body = "# Page\n\n" + "A" * 5000 + "\n"
        result = _deterministic_l1("test", body, "Summary.")
        assert len(result) < 3000  # Should be truncated


class TestGetTier:
    """Test the tier dispatcher."""

    def test_auto_returns_best_available(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "tier.md"
        page.write_text(
            "# Tier Test\n\nOne-sentence summary: Tier test.\n\nBody.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tmp_path / "tiers")

        result = get_tier("tier", level="auto")
        assert result["content"] is not None
        assert "l0" in result["available"]

    def test_l0_level(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "x.md"
        page.write_text("# X\n\nOne-sentence summary: Summary X.\n\nBody.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_tier("x", level="l0")
        assert "Summary X." in result["content"]

    def test_l2_level(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "y.md"
        page.write_text("# Y\n\nFull content.\n", encoding="utf-8")
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)

        result = get_tier("y", level="l2")
        assert "Full content." in result["content"]


class TestBuildAllTiers:
    """Test batch L1 generation."""

    def test_build_all_deterministic(self, tmp_path, monkeypatch):
        """Build L1 for all pages using deterministic mode."""
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text("# A\n\nOne-sentence summary: Page A.\n\nBody A.\n", encoding="utf-8")
        (notes / "b.md").write_text("# B\n\nOne-sentence summary: Page B.\n\nBody B.\n", encoding="utf-8")

        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tmp_path / "tiers")

        stats = build_tiers.build_all_tiers(use_llm=False, verbose=False)
        assert stats["generated"] == 2
        # Task 15: cache files are hash-suffixed by source SHA-256 + extractor.
        l1_files = sorted((tmp_path / "tiers").glob("*.l1.md"))
        assert len(l1_files) == 2
        assert all("." in path.stem for path in l1_files)
        assert all(not path.name.endswith("a.l1.md") for path in l1_files)

    def test_duplicate_stems_use_captured_relative_paths(self, tmp_path, monkeypatch):
        import build_tiers

        notes = tmp_path / "notes"
        (notes / "one").mkdir(parents=True)
        (notes / "two").mkdir(parents=True)
        (notes / "one/shared.md").write_text("# One\n\nFirst.\n", encoding="utf-8")
        (notes / "two/shared.md").write_text("# Two\n\nSecond.\n", encoding="utf-8")
        tiers = tmp_path / "tiers"
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tiers)

        stats = build_tiers.build_all_tiers(use_llm=False, verbose=False)

        assert stats == {"generated": 2, "skipped": 0, "errors": 0}
        outputs = sorted(tiers.glob("*.l1.md"))
        assert len(outputs) == 2
        rendered = {path.read_text(encoding="utf-8") for path in outputs}
        assert any("First." in value for value in rendered)
        assert any("Second." in value for value in rendered)

    def test_legacy_tier_cache_rejects_escaping_and_windows_names(self, tmp_path, monkeypatch):
        import build_tiers

        monkeypatch.setattr(build_tiers, "TIERS_DIR", tmp_path)
        for slug, logical_path in (
            ("../escape", "escape.md"),
            ("con", "con.md"),
            ("safe", "../escape.md"),
            ("safe", "C:/escape.md"),
        ):
            with pytest.raises(ValueError):
                build_tiers.tier_legacy_cache_path(
                    slug,
                    source_sha256="a" * 64,
                    logical_path=logical_path,
                )


class TestCapturedSourceTiers:
    def test_l0_l1_l2_use_captured_bytes_after_live_source_changes(self, tmp_path):
        original = (
            "---\ntype: concept\n---\n"
            "# Captured\n\nOne-sentence summary: Captured summary.\n\n"
            "## Facts\n\n- Captured fact.\n"
        )
        vault, snapshot = _snapshot(tmp_path, {"captured.md": original})
        source = snapshot.sources[0]
        (vault / source.record.relative_path).write_text(
            "# Live replacement\n\nOne-sentence summary: Wrong live summary.\n",
            encoding="utf-8",
        )

        assert get_l0_for_source(source) == "Captured summary."
        assert "Captured fact" in generate_l1_for_source(source, use_llm=False)
        assert get_l2_for_source(source) == original

    def test_llm_prompt_contains_captured_bytes_only(self, tmp_path, monkeypatch):
        vault, snapshot = _snapshot(
            tmp_path,
            {"page.md": "# Page\n\nOne-sentence summary: Old.\n\nOld captured body.\n"},
        )
        source = snapshot.sources[0]
        (vault / source.record.relative_path).write_text(
            "# Page\n\nNew live body.\n", encoding="utf-8"
        )
        prompts: list[str] = []

        descriptor = _model_descriptor()

        def fake_call_candidate(candidate, prompt, _system, max_tokens):
            assert candidate is descriptor
            prompts.append(prompt)
            assert max_tokens == 1000
            return SimpleNamespace(text="LLM overview")

        import llm_client

        monkeypatch.delenv("MEMORY_LLM_PROVIDER", raising=False)
        monkeypatch.setattr(llm_client, "call_candidate", fake_call_candidate)

        assert generate_l1_for_source(
            source,
            use_llm=True,
            model_descriptor=descriptor,
            model_revision="revision-1",
        ) == "LLM overview"
        assert "Old captured body" in prompts[0]
        assert "New live body" not in prompts[0]

    def test_identity_binds_logical_id_source_hash_and_extractor_version(self, tmp_path):
        path = "# Same\n\nOne-sentence summary: Before.\n"
        vault, before = _snapshot(tmp_path, {"same.md": path})
        source_before = before.sources[0]
        live = vault / source_before.record.relative_path
        stat = live.stat()
        changed = path.replace("Before", "After!")
        assert len(changed.encode()) == len(path.encode())
        live.write_text(changed, encoding="utf-8", newline="")
        os.utime(live, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        source_after = collect_corpus(vault).sources[0]

        assert tier_artifact_key(source_before) != tier_artifact_key(source_after)
        assert tier_artifact_key(source_before, extractor_version="tiers/v2") != (
            tier_artifact_key(source_before, extractor_version="tiers/v1")
        )

    def test_llm_identity_binds_descriptor_and_revision_not_generated_output(self, tmp_path):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        source = snapshot.sources[0]
        descriptor = _model_descriptor()
        options = {
            "model_descriptor": descriptor,
            "model_revision": "revision-1",
        }

        first = tier_artifact_key(source, generated_l1="First", **options)

        assert first == tier_artifact_key(source, generated_l1="Second", **options)
        assert first != tier_artifact_key(
            source,
            generated_l1="First",
            model_descriptor=descriptor,
            model_revision="revision-2",
        )
        assert first != tier_artifact_key(
            source,
            generated_l1="First",
            model_descriptor=_model_descriptor(model="other-model"),
            model_revision="revision-1",
        )


class TestCapturedTierBatch:
    @staticmethod
    def _load_artifact(generation: Path) -> dict:
        return json.loads((generation / "tiers/tiers.json").read_text(encoding="utf-8"))

    def test_exact_snapshot_membership_is_deterministic_and_duplicate_stems_are_distinct(
        self, tmp_path
    ):
        vault, snapshot = _snapshot(
            tmp_path,
            {
                "concept/same.md": "---\ntype: concept\n---\n# One\n\nFirst body.\n",
                "pattern/same.md": "---\ntype: pattern\n---\n# Two\n\nSecond body.\n",
            },
        )
        (vault / "knowledge/notes/live-only.md").write_text("# Live only\n", encoding="utf-8")
        first = tmp_path / "generation-a"
        second = tmp_path / "generation-b"
        first.mkdir()
        second.mkdir()

        descriptors = build_snapshot_tiers(snapshot, first, use_llm=False)
        second_descriptors = build_snapshot_tiers(snapshot, second, use_llm=False)

        assert descriptors == second_descriptors
        assert descriptors == sorted(descriptors, key=lambda item: item["path"])
        assert len(descriptors) == 1
        assert descriptors[0]["path"] == "tiers/tiers.json"
        artifact = self._load_artifact(first)
        entries = artifact["entries"]
        assert len({entry["key"] for entry in entries}) == 2
        assert {entry["source"]["logical_id"] for entry in entries} == {
            "source:knowledge/notes/concept/same.md",
            "source:knowledge/notes/pattern/same.md",
        }
        assert all(
            "live-only" not in entry["source"]["logical_id"] for entry in entries
        )
        assert {
            (entry["source"]["relative_path"], entry["source"]["sha256"])
            for entry in entries
        } == set(snapshot.source_hashes)
        assert {
            (
                entry["source"]["logical_id"],
                entry["source"]["size"],
                entry["source"]["git_oid"],
            )
            for entry in entries
        } == {
            (source.record.logical_id, source.record.size, source.record.git_oid)
            for source in snapshot.sources
        }
        assert {entry["tiers"]["l2"] for entry in entries} == {
            "---\ntype: concept\n---\n# One\n\nFirst body.\n",
            "---\ntype: pattern\n---\n# Two\n\nSecond body.\n",
        }
        assert all(
            (first / descriptor["path"]).stat().st_size == descriptor["size"]
            for descriptor in descriptors
        )
        assert all(
            hashlib.sha256((first / descriptor["path"]).read_bytes()).hexdigest()
            == descriptor["sha256"]
            for descriptor in descriptors
        )
        assert (first / "tiers/tiers.json").read_bytes() == (
            second / "tiers/tiers.json"
        ).read_bytes()

    def test_large_snapshot_uses_one_shared_catalog_descriptor(self, tmp_path):
        _, base = _snapshot(tmp_path, {"page.md": "# Page\n"})
        source = base.sources[0]
        sources = tuple(
            dataclasses.replace(
                source,
                record=dataclasses.replace(
                    source.record,
                    logical_id=f"source:knowledge/notes/page-{number:04d}.md",
                    relative_path=f"knowledge/notes/page-{number:04d}.md",
                ),
            )
            for number in range(1025)
        )
        snapshot = dataclasses.replace(base, sources=sources)
        generation = tmp_path / "generation"
        generation.mkdir()

        descriptors = build_snapshot_tiers(snapshot, generation)

        assert len(descriptors) == 1
        artifact = self._load_artifact(generation)
        assert len(artifact["entries"]) == len(snapshot.sources) == 1025

    def test_generation_defaults_to_deterministic_without_calling_llm(
        self, tmp_path, monkeypatch
    ):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        generation = tmp_path / "generation"
        generation.mkdir()

        def unexpected_call(*_args, **_kwargs):
            raise AssertionError("LLM must be explicitly enabled")

        import llm_client

        monkeypatch.setattr(llm_client, "call_candidate", unexpected_call)

        build_snapshot_tiers(snapshot, generation)

        artifact = self._load_artifact(generation)
        assert artifact["generation"] == {"mode": "deterministic", "model": None}

    def test_llm_generation_requires_bounded_descriptor_and_revision(self, tmp_path):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        generation = tmp_path / "generation"
        generation.mkdir()

        with pytest.raises(ValueError, match="descriptor.*revision"):
            build_snapshot_tiers(snapshot, generation, use_llm=True)
        with pytest.raises(ValueError, match="revision"):
            build_snapshot_tiers(
                snapshot,
                generation,
                use_llm=True,
                model_descriptor=_model_descriptor(),
                model_revision="x" * 129,
            )
        assert not (generation / "tiers").exists()

    def test_llm_generation_persists_model_and_output_bound_key(
        self, tmp_path, monkeypatch
    ):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        generation = tmp_path / "generation"
        generation.mkdir()
        descriptor = _model_descriptor()

        import llm_client

        monkeypatch.setattr(
            llm_client,
            "call_candidate",
            lambda *_args, **_kwargs: SimpleNamespace(text="Generated overview"),
        )

        build_snapshot_tiers(
            snapshot,
            generation,
            use_llm=True,
            model_descriptor=descriptor,
            model_revision="revision-1",
        )

        artifact = self._load_artifact(generation)
        assert artifact["generation"] == {
            "mode": "llm",
            "model": {**descriptor.canonical(), "revision": "revision-1"},
        }
        entry = artifact["entries"][0]
        assert entry["tiers"]["l1"] == "Generated overview"
        assert entry["key"] == tier_artifact_key(
            snapshot.sources[0],
            model_descriptor=descriptor,
            model_revision="revision-1",
            generated_l1="Generated overview",
        )

    def test_refuses_existing_tier_output(self, tmp_path):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        generation = tmp_path / "generation"
        (generation / "tiers").mkdir(parents=True)
        (generation / "tiers/partial.tmp").write_text("partial", encoding="utf-8")

        with pytest.raises(FileExistsError, match="tier output"):
            build_snapshot_tiers(snapshot, generation, use_llm=False)
        assert (generation / "tiers/partial.tmp").read_text(encoding="utf-8") == "partial"

    def test_fsyncs_file_and_directories_around_atomic_publication(
        self, tmp_path, monkeypatch
    ):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        generation = tmp_path / "generation"
        generation.mkdir()
        events: list[str] = []
        real_replace = Path.replace

        def recording_fsync(_descriptor):
            events.append("file")

        def recording_directory_fsync(_path):
            events.append("directory")

        def recording_replace(path, target):
            events.append("replace")
            return real_replace(path, target)

        monkeypatch.setattr(os, "fsync", recording_fsync)
        monkeypatch.setattr(Path, "replace", recording_replace)
        import build_tiers

        monkeypatch.setattr(
            build_tiers, "_fsync_directory", recording_directory_fsync, raising=False
        )

        build_snapshot_tiers(snapshot, generation)

        assert events == ["file", "directory", "replace", "directory"]

    def test_post_publish_sync_failure_removes_output(self, tmp_path, monkeypatch):
        _, snapshot = _snapshot(tmp_path, {"page.md": "# Page\n\nBody.\n"})
        generation = tmp_path / "generation"
        generation.mkdir()
        calls = 0

        def fail_generation_sync(_path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("generation sync failed")

        import build_tiers

        monkeypatch.setattr(
            build_tiers, "_fsync_directory", fail_generation_sync, raising=False
        )

        with pytest.raises(OSError, match="generation sync failed"):
            build_snapshot_tiers(snapshot, generation)
        assert not (generation / "tiers").exists()
        assert not list(generation.glob(".tiers-*"))

    def test_llm_failure_leaves_no_partial_generation_artifact(
        self, tmp_path, monkeypatch
    ):
        _, snapshot = _snapshot(
            tmp_path,
            {"a.md": "# A\n\nBody A.\n", "b.md": "# B\n\nBody B.\n"},
        )
        generation = tmp_path / "generation"
        generation.mkdir()
        calls = 0

        def fail_second(source, use_llm=True, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("LLM failed")
            return "overview"

        import build_tiers

        monkeypatch.setattr(build_tiers, "generate_l1_for_source", fail_second)

        with pytest.raises(RuntimeError, match="LLM failed"):
            build_snapshot_tiers(
                snapshot,
                generation,
                use_llm=True,
                model_descriptor=_model_descriptor(),
                model_revision="revision-1",
            )
        assert not (generation / "tiers").exists()
