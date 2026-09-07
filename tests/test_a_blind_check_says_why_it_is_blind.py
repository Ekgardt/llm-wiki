"""A check that could not read the state must say how far past the bound it was.

Two checks reported "could not be read within safety bounds" and neither said
what the bound was or how far over the file had gone. On this vault on
2026-09-07 the answer was 2.8 MB against a 256 KiB bound, all of it one
project's undrained checkpoint queue — a diagnosis the operator had to reach
with a Python one-liner against `run/state.json`.

The reader stays bounded. Only the sentence changes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doctor  # noqa: E402


def _state(root: Path, payload: bytes) -> Path:
    run = root / "run"
    run.mkdir(parents=True, exist_ok=True)
    path = run / "state.json"
    path.write_bytes(payload)
    return path


def test_the_size_and_the_bound_are_both_named(tmp_path):
    _state(tmp_path, b"x" * 900_000)

    hint = doctor.state_size_hint(tmp_path)

    assert "900000 bytes" in hint
    assert str(doctor.MAX_STATE_BYTES) in hint


def test_a_missing_state_file_adds_nothing(tmp_path):
    assert doctor.state_size_hint(tmp_path) == ""


def test_the_hint_reads_the_size_and_not_the_content(tmp_path):
    """A file too large to read is exactly the one this must not parse."""
    _state(tmp_path, b"{ this is not json " + b"y" * 500_000)

    hint = doctor.state_size_hint(tmp_path)

    assert "bytes against" in hint
