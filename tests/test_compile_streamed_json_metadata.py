"""Canonical metadata arrays must not require a whole-array JSON allocation."""
import hashlib
import io
import zipfile
from dataclasses import asdict

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_archive_daily_sharing import _parts, _reader


def test_metadata_array_encoding_is_incremental_and_byte_identical(monkeypatch):
    values = (("й", 0, 4), ("quoted\ntext", 4, 20))
    expected = controller._snapshot_json(values)
    encode = controller._snapshot_json

    def forbid_whole(value):
        if value is values:
            raise AssertionError("whole metadata array encoded")
        return encode(value)

    monkeypatch.setattr(controller, "_snapshot_json", forbid_whole)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        writer = controller._SnapshotArchiveWriter(archive)
        digest = controller._put_snapshot_json(writer.put, values)
    assert digest == hashlib.sha256(expected).hexdigest()
    with zipfile.ZipFile(output) as archive:
        assert archive.read("blobs/" + digest) == expected


def test_native_metadata_roundtrip_keeps_exact_old_identity():
    from dataclasses import replace

    frame = compiler.NativeCompileFrame("journal", 0, 3, "12:34:56", 'é"\\', "é\n")
    part = replace(_parts("knowledge/daily/2026-10-01.md")[0], native_frames=(frame,))
    expected = hashlib.sha256(controller._snapshot_json((asdict(frame),))).hexdigest()
    value = controller._archive_record_value(part, controller._blob_digest)
    assert value["native_frames"] == expected
    with _reader((part,)) as reader:
        assert reader.planning_inputs().dailies == (part,)


def test_changed_metadata_cannot_write_under_old_digest():
    values = [["before", 1, 2]]
    content = controller._SnapshotJsonArray(values)
    values[0][0] = "after"
    with pytest.raises(ValueError, match="changed"):
        b"".join(controller._snapshot_content_chunks(content))
