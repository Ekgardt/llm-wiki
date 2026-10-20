"""Verified native capture retains coordinates, not every decoded payload."""
import gc
import weakref

import compile_memory as compiler
import pytest

from tests.test_compile_archive_daily_sharing import _reader
from tests.test_native_compile_uses_whole_container import _native_inputs


@pytest.mark.parametrize("producer", ["capture", "historical"])
def test_verified_native_frames_are_released_after_coordinate_capture(tmp_path, monkeypatch, producer):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Actual user fact.\n" * 1000)
    logical = inputs.dailies[0].logical_path
    path = tmp_path / logical
    observed = []
    original = compiler._verified_native_frame

    def track(*args, **kwargs):
        frame = original(*args, **kwargs)
        if frame is not None:
            observed.append(weakref.ref(frame))
        return frame

    monkeypatch.setattr(compiler, "_verified_native_frame", track)
    with compiler._open_daily_source(path) as source:
        frames = _capture_frames(producer, logical, source, tmp_path)
        gc.collect()
        assert observed
        assert all(reference() is None for reference in observed)
        assert tuple(frames) == tuple(inputs.dailies[0].native_frames)
    with pytest.raises(ValueError, match="closed"):
        frames[0]


def _capture_frames(producer, logical, source, root):
    if producer == "historical":
        return compiler._native_daily_frames(logical, source, root)
    part = next(compiler._CapturedPartitions().iter_daily(logical, source, None))
    return part.native_frames


def test_position_checks_do_not_redecode_payloads(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Complete fact.")
    frames = inputs.dailies[0].native_frames
    expected = [(frame.byte_start, frame.byte_end) for frame in frames]

    def refuse(*args):
        raise AssertionError("position check decoded payload")

    monkeypatch.setattr(compiler, "_native_frame_from_coordinates", refuse)
    assert list(compiler._native_frame_spans(frames)) == expected


@pytest.mark.parametrize("producer", ["capture", "historical"])
def test_initial_capture_still_verifies_permanent_source(tmp_path, monkeypatch, producer):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, "Complete fact.")
    logical = inputs.dailies[0].logical_path
    head = next(tmp_path.glob("knowledge/raw/sessions/**/*.breadcrumb.md"))
    head.unlink()
    with compiler._open_daily_source(tmp_path / logical) as source:
        with pytest.raises((OSError, ValueError)):
            _capture_frames(producer, logical, source, tmp_path)


def test_source_backed_native_frames_survive_archive_roundtrip(tmp_path, monkeypatch):
    inputs, _ = _native_inputs(tmp_path, monkeypatch, 'Unicode й, quotes " and slash \\.\n' * 1000)
    expected = tuple(inputs.dailies[0].native_frames)
    with _reader(inputs.dailies) as reader:
        restored = reader.planning_inputs().dailies
        assert restored == inputs.dailies
        assert restored[0].native_frames == expected
