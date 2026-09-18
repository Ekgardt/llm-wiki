"""Tests for build_tiers.py — L0/L1/L2 progressive disclosure."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_tiers import (  # noqa: E402
    _deterministic_l1,
    get_l0,
    get_l2,
    get_tier,
)
from llm_client import ProviderDescriptor  # noqa: E402


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

    def test_a_pass_leaves_one_l1_per_page_and_no_older_ones(self, tmp_path, monkeypatch):
        """The name carries the page's hash, so an edit used to leave a file for ever."""
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "a.md"
        page.write_text("# A\n\nOne-sentence summary: Page A.\n", encoding="utf-8")
        tiers = tmp_path / "tiers"
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tiers)

        build_tiers.build_all_tiers(use_llm=False, verbose=False)
        first = sorted(path.name for path in tiers.glob("*.l1.md"))
        page.write_text("# A\n\nOne-sentence summary: Page A, revised.\n", encoding="utf-8")
        build_tiers.build_all_tiers(use_llm=False, verbose=False)
        second = sorted(path.name for path in tiers.glob("*.l1.md"))

        assert (len(first), len(second)) == (1, 1)
        assert first != second

    def test_two_pages_with_one_stem_keep_both_of_their_l1_files(self, tmp_path, monkeypatch):
        """Pruning by slug alone would delete the sibling's current file."""
        import build_tiers

        notes = tmp_path / "notes"
        for directory in ("concepts", "patterns"):
            (notes / directory).mkdir(parents=True)
            (notes / directory / "shared.md").write_text(
                f"# Shared\n\nOne-sentence summary: {directory}.\n", encoding="utf-8"
            )
        tiers = tmp_path / "tiers"
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tiers)

        build_tiers.build_all_tiers(use_llm=False, verbose=False)

        assert len(list(tiers.glob("shared.*.l1.md"))) == 2

    def test_build_all_defaults_to_deterministic_and_llm_mode_fails_before_write(
        self, tmp_path, monkeypatch
    ):
        import build_tiers

        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "a.md").write_text("# A\n\nBody.\n", encoding="utf-8")
        tiers = tmp_path / "tiers"
        monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
        monkeypatch.setattr(build_tiers, "TIERS_DIR", tiers)

        assert build_tiers.build_all_tiers(verbose=False)["generated"] == 1
        with pytest.raises(ValueError, match="descriptor.*revision"):
            build_tiers.build_all_tiers(use_llm=True, verbose=False)
        assert len(list(tiers.glob("*.l1.md"))) == 1

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

        outputs = sorted(tiers.glob("*.l1.md"))
        rendered = "\n".join(path.read_text(encoding="utf-8") for path in outputs)
        assert (
            stats,
            len(outputs),
            "First." in rendered,
            "Second." in rendered,
        ) == ({"generated": 2, "skipped": 0, "errors": 0}, 2, True, True)

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

    def test_legacy_tier_key_includes_generation_mode_and_full_model_revision(
        self, tmp_path, monkeypatch
    ):
        import build_tiers

        monkeypatch.setattr(build_tiers, "TIERS_DIR", tmp_path)
        descriptor = _model_descriptor(model="model-a")
        deterministic = build_tiers.tier_legacy_cache_path(
            "page", source_sha256="a" * 64, logical_path="page.md"
        )
        generated = build_tiers.tier_legacy_cache_path(
            "page",
            source_sha256="a" * 64,
            logical_path="page.md",
            generation_mode="llm",
            model_descriptor=descriptor,
            model_revision="revision-1",
        )

        assert deterministic != generated
        assert generated != build_tiers.tier_legacy_cache_path(
            "page",
            source_sha256="a" * 64,
            logical_path="page.md",
            generation_mode="llm",
            model_descriptor=descriptor,
            model_revision="revision-2",
        )
        with pytest.raises(ValueError, match="source_sha256"):
            build_tiers.tier_legacy_cache_path(
                "page",
                generation_mode="llm",
                model_descriptor=descriptor,
                model_revision="revision-1",
            )


def test_tier_cli_defaults_to_deterministic_and_llm_flag_fails_closed(monkeypatch):
    import build_tiers

    calls = []
    monkeypatch.setattr(build_tiers, "build_all_tiers", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["build_tiers.py", "--all"])
    assert build_tiers.main() == 0
    assert calls == [{"use_llm": False}]

    monkeypatch.setattr(sys, "argv", ["build_tiers.py", "--all", "--llm"])
    with pytest.raises(SystemExit):
        build_tiers.main()
