"""Windows text translation must not rewrite historical protocol fixtures."""
import json
from pathlib import Path

from tests.historical_test_protocols_keep_their_exact_utf8_bytes_helpers import _windows_text_write
from tests.receipt_versions_preserve_historical_archive_authority_helpers import _restore_v3_bag
from tests.test_compile_hardening import (
    test_unusable_receipts_are_discarded_only_when_asked as _receipt_case,
)


def test_receipt_discard_preserves_good_fixture_under_windows_text_io(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "write_text", _windows_text_write)
    _receipt_case(tmp_path, monkeypatch)


def test_historical_archive_restores_every_original_utf8_byte(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "write_text", _windows_text_write)
    directory = _restore_v3_bag(tmp_path / "historical-v3")
    fixture = Path(__file__).parent / "fixtures/compile-receipt-v3-bag.json"
    expected = json.loads(fixture.read_bytes())["files"]
    for name, text in expected.items():
        assert (directory / name).read_bytes() == text.encode("utf-8"), name
