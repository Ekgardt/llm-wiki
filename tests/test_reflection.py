"""Tests for reflection.py — A-MEM memory evolution (page consolidation)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from reflection import REFLECTION_THRESHOLD, find_reflection_candidates  # noqa: E402


class TestFindCandidates:
    """Test finding pages that need reflection."""

    def test_no_candidates_in_empty_vault(self, tmp_path, monkeypatch):
        import reflection

        monkeypatch.setattr(reflection, "KNOWLEDGE", tmp_path / "notes")
        candidates = find_reflection_candidates()
        assert candidates == []

    def test_finds_page_with_multiple_updates(self, tmp_path, monkeypatch):
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "growing-page.md"
        page.write_text(
            "---\ntype: pattern\n---\n\n"
            "# Growing Page\n\n"
            "Original content.\n\n"
            "## Update (2026-01-15)\nFirst update.\n\n"
            "## Update (2026-02-20)\nSecond update.\n\n"
            "## Update (2026-03-10)\nThird update.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 1
        assert candidates[0]["slug"] == "growing-page"
        assert candidates[0]["update_count"] == 3

    def test_ignores_pages_below_threshold(self, tmp_path, monkeypatch):
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "few-updates.md"
        page.write_text(
            "---\ntype: concept\n---\n\n"
            "# Few Updates\n\n"
            "Content.\n\n"
            "## Update (2026-01-15)\nOnly one.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 0

    def test_skips_superseded_pages(self, tmp_path, monkeypatch):
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "old.md"
        page.write_text(
            "---\ntype: decision\nstatus: superseded\n---\n\n"
            "# Old\n\n"
            "## Update (2026-01-15)\nA\n\n"
            "## Update (2026-02-15)\nB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 0

    def test_skips_already_reflected_pages(self, tmp_path, monkeypatch):
        """Pages with ## History section are already reflected."""
        import reflection

        notes = tmp_path / "notes"
        notes.mkdir()
        page = notes / "reflected.md"
        page.write_text(
            "---\ntype: pattern\n---\n\n"
            "# Reflected\n\n"
            "Clean content.\n\n"
            "## History (pre-reflection)\nOld stuff.\n\n"
            "## Update (2026-01-15)\nA\n\n"
            "## Update (2026-02-15)\nB\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(reflection, "KNOWLEDGE", notes)

        candidates = find_reflection_candidates()
        assert len(candidates) == 0

    def test_threshold_value(self):
        """Threshold should be at least 2."""
        assert REFLECTION_THRESHOLD >= 2


class TestReflectPage:
    """Test the reflect_page function."""

    def test_dry_run_returns_message(self, tmp_path):
        from reflection import reflect_page

        page = tmp_path / "test.md"
        page.write_text(
            "# Test\n\nBody.\n\n"
            "## Update (2026-01-15)\nA\n\n"
            "## Update (2026-02-15)\nB\n",
            encoding="utf-8",
        )
        result = reflect_page(page, apply=False)
        assert "candidate" in result.lower()

    def test_skip_page_below_threshold(self, tmp_path):
        from reflection import reflect_page

        page = tmp_path / "test.md"
        page.write_text("# Test\n\nNo updates here.\n", encoding="utf-8")
        result = reflect_page(page, apply=False)
        assert "skipping" in result.lower()


GOOD_REWRITE = (
    "# Growing Page\n\nOne-sentence summary: the page after its updates were folded in.\n\n"
    + " ".join(["The consolidated narrative keeps every fact the updates stated."] * 8)
    + "\n"
)
PAGE = (
    "---\ntype: pattern\n---\n\n# Growing Page\n\n"
    "One-sentence summary: a page that gathers updates.\n\nOriginal content.\n\n"
    "## Update (2026-01-15)\nFirst update.\n\n## Update (2026-02-20)\nSecond update.\n"
)


def _vault_page(tmp_path, monkeypatch, text: str, reply: str):
    import reflection

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    page = notes / "growing-page.md"
    page.write_text(text, encoding="utf-8")
    written: dict = {}
    monkeypatch.setattr(reflection, "ROOT", tmp_path)
    monkeypatch.setattr(reflection, "KNOWLEDGE", notes)
    monkeypatch.setattr("llm_client.call_llm", lambda *args, **kwargs: reply)
    monkeypatch.setattr(reflection, "mutate_knowledge", lambda _id, files, **_k: written.update(files))
    return reflection, page, written


class TestARewriteIsCheckedBeforeItIsWritten:
    """`docs/research/2026-09-17-a-reflection-is-checked-before-it-is-written.md`."""

    def test_a_refusal_leaves_the_page_as_it_was(self, tmp_path, monkeypatch):
        reflection, page, written = _vault_page(tmp_path, monkeypatch, PAGE, "I'm sorry, I can't help with that.")

        message = reflection.reflect_page(page, apply=True)

        assert (written, "not written" in message) == ({}, True)

    def test_a_rewrite_that_lost_the_summary_is_not_written(self, tmp_path, monkeypatch):
        reply = GOOD_REWRITE.replace("One-sentence summary:", "Summary:")
        reflection, page, written = _vault_page(tmp_path, monkeypatch, PAGE, reply)

        reflection.reflect_page(page, apply=True)

        assert written == {}

    def test_a_good_rewrite_keeps_the_old_body_once(self, tmp_path, monkeypatch):
        reflection, page, written = _vault_page(tmp_path, monkeypatch, PAGE, GOOD_REWRITE)

        reflection.reflect_page(page, apply=True)

        text = written[page].decode("utf-8")
        assert (text.count("Original content."), text.count("## History (pre-reflection")) == (1, 1)

    def test_a_decision_is_never_a_candidate_and_never_rewritten(self, tmp_path, monkeypatch):
        decision = PAGE.replace("type: pattern", "type: decision")
        reflection, page, written = _vault_page(tmp_path, monkeypatch, decision, GOOD_REWRITE)

        message = reflection.reflect_page(page, apply=True)

        assert (reflection.find_reflection_candidates(), written, "never rewritten" in message) == ([], {}, True)

    def test_a_reflected_page_with_new_updates_is_a_candidate_again(self, tmp_path, monkeypatch):
        reflected = (
            GOOD_REWRITE.replace("# Growing Page", "---\ntype: pattern\n---\n\n# Growing Page")
            + "\n## Update (2026-05-01)\nNew.\n\n## Update (2026-06-01)\nNewer.\n"
            + "\n\n## History (pre-reflection 2026-03-01)\n<details>\n\n## Update (2026-01-15)\nOld.\n\n</details>\n"
        )
        reflection, _page, _written = _vault_page(tmp_path, monkeypatch, reflected, GOOD_REWRITE)

        candidates = reflection.find_reflection_candidates()

        assert [item["update_count"] for item in candidates] == [2]
