"""Tests for archive_stale.py — type-aware age thresholds."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def fake_file(tmp_path):
    """Create a fake markdown file with frontmatter."""
    def _make(name: str, page_type: str = "debugging", age_days: int = 100):
        p = tmp_path / name
        content = f"""---
type: {page_type}
timestamp: {(datetime.now() - timedelta(days=age_days)).isoformat(timespec='seconds')}
---

# {name}

One-sentence summary: test page.

## Body
Content.
"""
        p.write_text(content, encoding="utf-8")
        # Set mtime to simulate age
        old_time = time.time() - (age_days * 86400)
        os.utime(p, (old_time, old_time))
        return p
    return _make


def test_debugging_archives_at_60_days(fake_file):
    """Debugging type should be stale at 60 days."""
    import archive_stale

    md = fake_file("test-debug.md", "debugging", age_days=65)
    cutoff = datetime.now().timestamp() - (180 * 86400)  # default 180
    assert archive_stale._is_stale(md, cutoff, 180) is True


def test_debugging_not_stale_at_30_days(fake_file):
    """Debugging type should NOT be stale at 30 days."""
    import archive_stale

    md = fake_file("test-debug.md", "debugging", age_days=30)
    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_decision_never_archives(fake_file):
    """Decision type is evergreen — never archives."""
    import archive_stale

    md = fake_file("old-decision.md", "decision", age_days=999)
    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_concept_never_archives(fake_file):
    """Concept type is evergreen."""
    import archive_stale

    md = fake_file("old-concept.md", "concept", age_days=999)
    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_superseded_not_archived_again(fake_file):
    """Already-superseded pages are skipped."""
    import archive_stale

    md = fake_file("superseded.md", "pattern", age_days=999)
    # Add superseded status
    content = md.read_text()
    content = content.replace("type: pattern", "type: pattern\nstatus: superseded")
    md.write_text(content)

    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_already_archived_skipped(fake_file):
    """Already-archived pages are skipped."""
    import archive_stale

    md = fake_file("archived.md", "debugging", age_days=999)
    content = md.read_text()
    content = content.replace("type: debugging", "type: debugging\nstatus: archived")
    md.write_text(content)

    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_skill_never_archives(fake_file):
    """Skill type is evergreen."""
    import archive_stale

    md = fake_file("my-skill.md", "skill", age_days=999)
    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_qa_uses_365_threshold(fake_file):
    """QA type uses 365-day threshold (longer than default)."""
    import archive_stale

    md = fake_file("old-qa.md", "qa", age_days=200)
    cutoff = datetime.now().timestamp() - (180 * 86400)
    # 200 days < 365 threshold → NOT stale
    assert archive_stale._is_stale(md, cutoff, 180) is False


def test_pattern_uses_180_threshold(fake_file):
    """Pattern type uses 180-day threshold."""
    import archive_stale

    md = fake_file("old-pattern.md", "pattern", age_days=185)
    cutoff = datetime.now().timestamp() - (180 * 86400)
    assert archive_stale._is_stale(md, cutoff, 180) is True


def test_type_threshold_lookup():
    """Type-specific thresholds are correct."""
    import archive_stale

    assert archive_stale._get_type_threshold("debugging") == 60
    assert archive_stale._get_type_threshold("decision") == 99999
    assert archive_stale._get_type_threshold("concept") == 99999
    assert archive_stale._get_type_threshold("pattern") == 180
    assert archive_stale._get_type_threshold("qa") == 365
    assert archive_stale._get_type_threshold("unknown") == 180  # default


def test_archive_page_actually_moves_file(fake_file, tmp_path, monkeypatch):
    """When apply=True, the file is physically moved to archive/ with status: archived."""
    import archive_stale

    # Point archive_stale at tmp_path so we don't touch the real vault.
    monkeypatch.setattr(archive_stale, "ROOT", tmp_path)
    monkeypatch.setattr(archive_stale, "KNOWLEDGE", tmp_path / "knowledge" / "notes")
    archive_dir = tmp_path / "knowledge" / "notes" / "archive"
    monkeypatch.setattr(archive_stale, "ARCHIVE_ROOT", archive_dir)

    # Create a stale debugging page (65 days old).
    md = fake_file("stale-debug.md", "debugging", age_days=65)

    result = archive_stale._archive_page(md, apply=True)

    # Original file must be gone.
    assert not md.exists(), f"original file still at {md}"

    # Archive copy must exist and contain status: archived.
    year = datetime.now().strftime("%Y")
    archived = archive_dir / year / md.name
    assert archived.exists(), f"archived file not at {archived}"
    content = archived.read_text(encoding="utf-8")
    assert "status: archived" in content

    # Result string should indicate a real archive, not dry-run.
    assert result.startswith("ARCHIVED:")


def test_archive_page_dry_run_does_not_move(fake_file, tmp_path, monkeypatch):
    """When apply=False (dry-run), the file stays put."""
    import archive_stale

    monkeypatch.setattr(archive_stale, "ROOT", tmp_path)
    monkeypatch.setattr(archive_stale, "KNOWLEDGE", tmp_path / "knowledge" / "notes")
    archive_dir = tmp_path / "knowledge" / "notes" / "archive"
    monkeypatch.setattr(archive_stale, "ARCHIVE_ROOT", archive_dir)

    md = fake_file("stale-debug.md", "debugging", age_days=65)

    result = archive_stale._archive_page(md, apply=False)

    # Original file must still exist.
    assert md.exists(), "original file was moved during dry-run"
    # No archive copy should exist.
    year = datetime.now().strftime("%Y")
    archived = archive_dir / year / md.name
    assert not archived.exists()
    # Result should indicate dry-run.
    assert result.startswith("WOULD ARCHIVE:")
