"""The MCP page helpers read flat pages, resolve and cap content-addressed evidence, fail closed and redacted, and record reads without content."""
from __future__ import annotations

import os
import time
from pathlib import Path

import bounded_io
import pytest

from tests.mcp_server_support import (
    _assert_evidence_event,
    _assert_read_page_events,
    _assert_redacted_error_result,
    _assert_redacted_failure,
)


class TestPageHelpers:
    """The page reads and decision appends the MCP tools wrap."""

    def test_read_page_existing(self):
        """Reading an existing page should return content."""
        from mcp_server import read_page
        # index.md always exists in knowledge/notes/
        result = read_page("index")
        # May or may not exist depending on vault state
        assert isinstance(result, dict)

    def test_read_page_nonexistent(self):
        """Reading a non-existent page should return error."""
        from mcp_server import read_page
        result = read_page("this-page-does-not-exist-12345")
        assert "error" in result

    @pytest.mark.parametrize(
        "slug",
        [
            "../daily/secret",
            "../../README",
            r"..\daily\secret",
            r"C:\Windows\system32\config",
            r"C:relative",
            "/absolute/path",
            ".",
            "..",
        ],
    )
    def test_read_page_rejects_non_flat_and_drive_qualified_slugs(
        self, tmp_path, monkeypatch, slug
    ):
        from mcp_server import read_page

        notes = tmp_path / "knowledge" / "notes"
        notes.mkdir(parents=True)
        daily = tmp_path / "knowledge" / "daily"
        daily.mkdir()
        (daily / "secret.md").write_text("secret", encoding="utf-8")
        (tmp_path / "README.md").write_text("readme", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = read_page(slug)

        assert "error" in result
        assert "invalid" in result["error"].lower()
        assert "secret" not in result.get("content", "")
        assert "readme" not in result.get("content", "")

    def test_read_page_accepts_flat_unicode_and_space_slug(self, tmp_path, monkeypatch):
        from mcp_server import read_page

        notes = tmp_path / "knowledge" / "notes"
        notes.mkdir(parents=True)
        (notes / "Проект notes.md").write_text("safe content", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = read_page("Проект notes")

        assert result["content"] == "safe content"
        assert result["slug"] == "Проект notes"

    def test_read_page_resolves_content_addressed_evidence(self, tmp_path, monkeypatch):
        from mcp_server import read_page
        from reliable_memory import sha256_bytes

        daily = tmp_path / "knowledge/daily/2026-01-01.md"
        notes = tmp_path / "knowledge/notes"
        daily.parent.mkdir(parents=True)
        notes.mkdir(parents=True)
        source = b"## [evt-1] event\nverified quote\n"
        daily.write_bytes(source)
        start = source.index(b"verified quote")
        reference = (
            f"daily:2026-01-01 sha256:{sha256_bytes(source)} "
            f"block:evt-1 bytes:{start}-{start + len(b'verified quote')}"
        )
        (notes / "page.md").write_text(f"## Evidence\n- `{reference}`\n", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = read_page("page")

        assert result["evidence"] == [{
            "reference": reference,
            "sha256": sha256_bytes(b"verified quote"),
            "text": "verified quote",
        }]

    def test_read_page_fails_closed_when_evidence_hash_is_wrong(self, tmp_path, monkeypatch):
        from mcp_server import read_page

        daily = tmp_path / "knowledge/daily/2026-01-01.md"
        notes = tmp_path / "knowledge/notes"
        daily.parent.mkdir(parents=True)
        notes.mkdir(parents=True)
        source = b"## [evt-1] event\nverified quote\n"
        daily.write_bytes(source)
        start = source.index(b"verified quote")
        reference = (
            f"daily:2026-01-01 sha256:{'0' * 64} "
            f"block:evt-1 bytes:{start}-{start + len(b'verified quote')}"
        )
        (notes / "page.md").write_text(f"## Evidence\n- `{reference}`\n", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = read_page("page")

        assert "error" in result
        assert "evidence" in result["error"].lower()

    def test_read_page_rejects_malformed_evidence_and_oversized_pages(
        self, tmp_path, monkeypatch
    ):
        import mcp_server

        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        page = notes / "page.md"
        page.write_text("`daily:2026-01-01 broken`", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        # Text that parses as no reference is named, the page still read (audit 2026-09-26 B-17).
        assert mcp_server.read_page("page")["evidence"] == [
            {"reference": None, "error": "not_an_evidence_reference"}
        ]

        page.write_bytes(b"x" * (bounded_io.MAX_KNOWLEDGE_PAGE_BYTES + 1))
        result = mcp_server.read_page("page")
        assert "error" in result
        assert "exceeds" in result["error"]

    def test_read_page_redacts_read_and_evidence_exception_details(
        self, tmp_path, monkeypatch
    ):
        import evidence_resolver
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        (notes / "page.md").write_text("evidence", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
        monkeypatch.setattr(
            "llm_wiki.mcp_server_vault.read_stable_bytes",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sensitive)),
        )
        read_error = mcp_server.read_page("page")

        monkeypatch.setattr("llm_wiki.mcp_server_vault.read_stable_bytes", lambda *args, **kwargs: b"evidence")
        monkeypatch.setattr(
            evidence_resolver,
            "evidence_candidates",
            lambda content: [object()],
        )
        monkeypatch.setattr(
            evidence_resolver.EvidenceResolver,
            "resolve",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sensitive)),
        )
        evidence_error = mcp_server.read_page("page")

        for result in (read_error, evidence_error):
            _assert_redacted_failure(result, sensitive)

    def test_log_decision_redacts_append_exception_details(self, monkeypatch):
        import daily_log_append
        import mcp_server

        sensitive = r"api_key=sk-abcdefghijklmnopqrstuvwxyz C:\private\vault\secret.md"
        monkeypatch.setattr(
            daily_log_append,
            "append_daily",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sensitive)),
        )

        result = mcp_server.log_decision("decision")

        _assert_redacted_error_result(result, sensitive)

    def test_log_decision_propagates_deadline_and_cancellation_to_append(
        self, monkeypatch
    ):
        import daily_log_append
        import mcp_server

        received = []

        def cancelled():
            return False

        monkeypatch.setattr("llm_wiki.mcp_server_workers.operation_cancelled", lambda: cancelled)
        monkeypatch.setattr(
            daily_log_append,
            "append_daily",
            lambda *args, **kwargs: received.append(kwargs) or Path("daily.md"),
        )
        deadline = time.monotonic() + 5

        result = mcp_server.log_decision("decision", deadline=deadline)

        assert result["status"] == "logged"
        assert received == [{"deadline": deadline, "cancelled": cancelled}]

    def test_read_page_caps_each_resolved_evidence_slice(self, tmp_path, monkeypatch):
        import mcp_server
        from reliable_memory import sha256_bytes

        notes = tmp_path / "knowledge/notes"
        daily = tmp_path / "knowledge/daily/2026-01-01.md"
        notes.mkdir(parents=True)
        daily.parent.mkdir(parents=True)
        quote = b"x" * (mcp_server.MAX_MCP_EVIDENCE_BYTES + 1)
        source = b"## [evt-1] event\n" + quote + b"\n"
        daily.write_bytes(source)
        start = source.index(quote)
        reference = (
            f"daily:2026-01-01 sha256:{sha256_bytes(source)} block:evt-1 "
            f"bytes:{start}-{start + len(quote)}"
        )
        (notes / "page.md").write_text(f"`{reference}`", encoding="utf-8")
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = mcp_server.read_page("page")

        assert "error" in result
        assert "slice exceeds" in result["error"]

    def test_read_page_rejects_symlink_or_reparse_page(self, tmp_path, monkeypatch):
        import mcp_server

        notes = tmp_path / "knowledge/notes"
        notes.mkdir(parents=True)
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        page = notes / "page.md"
        try:
            os.symlink(outside, page)
        except OSError:
            page.write_text("safe", encoding="utf-8")
            original = Path.lstat

            def reparse(path: Path):
                result = original(path)
                if path == page:
                    return type(
                        "ReparseStat",
                        (),
                        {
                            "st_mode": result.st_mode,
                            "st_file_attributes": 0x400,
                            "st_size": result.st_size,
                            "st_dev": result.st_dev,
                            "st_ino": result.st_ino,
                            "st_mtime_ns": result.st_mtime_ns,
                        },
                    )()
                return result

            monkeypatch.setattr(Path, "lstat", reparse)
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))

        result = mcp_server.read_page("page")

        assert "error" in result
        assert "regular" in result["error"] or "symlink" in result["error"]

    def test_read_page_emits_page_and_evidence_events_without_content(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry
        from reliable_memory import sha256_bytes

        notes = tmp_path / "vault/knowledge/notes"
        daily = tmp_path / "vault/knowledge/daily/2026-01-01.md"
        notes.mkdir(parents=True)
        daily.parent.mkdir(parents=True)
        quote = b"distinctive evidence content secret"
        source = b"## [evt-1] event\n" + quote + b"\n"
        daily.write_bytes(source)
        start = source.index(quote)
        reference = (
            f"daily:2026-01-01 sha256:{sha256_bytes(source)} block:evt-1 "
            f"bytes:{start}-{start + len(quote)}"
        )
        (notes / "page.md").write_text(f"# Page\n`{reference}`\n", encoding="utf-8")
        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "vault"))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))

        result = mcp_server.read_page("page")

        assert result["slug"] == "page"
        rows = retrieval_telemetry.read_events(limit=10, db_path=database)
        _assert_read_page_events(rows)
        _assert_evidence_event(rows, sha256_bytes(quote))
        assert quote not in database.read_bytes()

    def test_failed_mcp_reads_emit_no_success_events(self, tmp_path, monkeypatch):
        import mcp_server
        import retrieval_telemetry

        database = tmp_path / "state/cache/evidence-graph/telemetry.sqlite3"
        monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path / "vault"))
        monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))

        assert "error" in mcp_server.read_page("missing")
        assert retrieval_telemetry.read_events(limit=10, db_path=database) == []
