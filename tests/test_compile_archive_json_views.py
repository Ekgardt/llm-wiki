"""Archive array admission validates everything without retaining all values."""
import hashlib
import io
from dataclasses import replace

import compile_controller as controller
import compile_memory as compiler
import pytest
from bounded_io import VerifiedByteRange, open_stable_range

from tests.test_compile_archive_daily_sharing import _parts, _reader


def test_daily_restore_does_not_read_entire_metadata_blob(monkeypatch):
    frame = compiler.NativeCompileFrame("journal", 0, 3, "12:00:00", "{}", "text")
    part = replace(_parts("knowledge/daily/2026-10-20.md")[0], native_frames=(frame,))
    with _reader((part,)) as reader:
        metadata = reader.record_metadata("dailies", 0)
        forbidden = {metadata[name] for name in controller._ARCHIVE_JSON_FIELDS}
        original = reader.blob

        def guarded(digest):
            if digest in forbidden:
                raise AssertionError("whole metadata blob read")
            return original(digest)

        monkeypatch.setattr(reader, "blob", guarded)
        restored = next(reader.daily_records())
        assert tuple(restored.native_frames) == (frame,)
        detached = reader.planning_inputs().dailies[0]
    assert detached.native_frames == (frame,)
    with pytest.raises(ValueError, match="closed"):
        restored.native_frames[0]


def test_restored_position_checks_do_not_decode_native_payloads(monkeypatch):
    frame = compiler.NativeCompileFrame("journal", 0, 3, "12:00:00", "{}", "text")
    part = replace(_parts("knowledge/daily/2026-10-20.md")[0], native_frames=(frame,))
    with _reader((part,)) as reader:
        frames = next(reader.daily_records()).native_frames

        def refuse(*args):
            raise AssertionError("position check decoded native payload")

        monkeypatch.setattr(controller, "_native_frame_from_json", refuse)
        assert list(compiler._native_frame_spans(frames)) == [(0, 3)]


@pytest.mark.parametrize("value", [
    [], [1, 12.5, True, None, "quotes \\\" and й", {"x": [",", "]"]}],
    ["\\" * 32768 + '"', {"long": "й" * 65536}],
])
def test_array_view_preserves_canonical_values_and_slices(tmp_path, value):
    raw = controller._snapshot_json(value)
    path = tmp_path / "array"
    path.write_bytes(raw)
    with open_stable_range(path, len(raw)) as source:
        result = controller._ArchiveJsonArray(source)
        assert list(result) == value
        assert list(result[::-1]) == value[::-1]
        assert hashlib.sha256(raw).hexdigest() == source.digest
    with pytest.raises(ValueError, match="closed"):
        len(result)


@pytest.mark.parametrize("raw", [
    b"{}", b"[1,]", b"[,1]", b"[1] ", b"[ 1]", b"[1 2]",
    b'[{"a":1,"a":2}]', b'[{"z":1,"a":2}]', b'["\\u0061"]',
    b"[NaN]", b'["unterminated]', b"[[1}", b'["bad\\q"]',
])
def test_invalid_or_noncanonical_array_is_rejected_at_admission(tmp_path, raw):
    path = tmp_path / "array"
    path.write_bytes(raw)
    with open_stable_range(path, len(raw)) as source:
        with pytest.raises((ValueError, controller.progress.ProgressCorrupt)):
            controller._ArchiveJsonArray(source)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 8, 13])
def test_escaping_and_nesting_cross_every_small_transfer_boundary(chunk_size):
    values = ['\\",[]{}й', "\\\\", {"nested": [1, "comma, quote\"", []]}, False, None]
    raw = controller._snapshot_json(values)
    with io.BytesIO(raw) as stream:
        source = VerifiedByteRange(stream, len(raw), hashlib.sha256(raw).hexdigest(),
                                   chunk_size=chunk_size)
        assert list(controller._ArchiveJsonArray(source)) == values
