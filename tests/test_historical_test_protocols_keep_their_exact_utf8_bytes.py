"""Windows text translation must not rewrite historical protocol fixtures."""
import json
from pathlib import Path

from tests.test_compile_hardening import (
    test_unusable_receipts_are_discarded_only_when_asked as _receipt_case,
)
from tests.test_receipt_versions_preserve_historical_archive_authority import _restore_v3_bag


def _windows_text_write(path, data, encoding=None, errors=None, newline=None):
    ending = {None: "\r\n"}.get(newline, newline)
    with path.open("w", encoding=encoding or "utf-8", errors=errors, newline=ending) as output:
        return output.write(data)


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
