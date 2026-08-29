"""An abandoned symbol-registry walk stops working, not just stops being awaited.

NEW-110, second half: ``mcp_server`` bounds a code-graph call by abandoning it
on a daemon thread — "``code_graph``'s live extraction takes no deadline and
cannot be interrupted from outside, so an abandoned run keeps its worker until
it finishes on its own". A Python thread cannot be interrupted from outside, so
the only way an abandoned run stops allocating is for the walk to check a stop
itself. These tests hold that line: the walk stops where it was told to, it
stops allocating there, and an unbounded caller gets the identical registry.
"""

from __future__ import annotations

import sys
import time
import tracemalloc
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import import_resolver  # noqa: E402  (needs the scripts/ path above)

_BODY = "\n".join(f"def handler_{index:03d}(argument): return argument" for index in range(60))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workspace(root: Path, files: int) -> Path:
    for index in range(files):
        _write(root / "pkg" / f"module_{index:04d}.py", _BODY)
    return root


class _CancelAfter:
    """A ``cancelled`` callable that flips once the walk has parsed enough."""

    def __init__(self, monkeypatch, limit: int) -> None:
        self.parsed = 0
        self._limit = limit
        real_parse = import_resolver.ast.parse

        def counting_parse(*args, **kwargs):
            self.parsed += 1
            return real_parse(*args, **kwargs)

        monkeypatch.setattr(import_resolver.ast, "parse", counting_parse)

    def __call__(self) -> bool:
        return self.parsed >= self._limit


def test_a_cancelled_walk_stops_parsing_where_it_was_cancelled(tmp_path, monkeypatch):
    """Cancellation stops the walk, and the exception names why it stopped."""
    _workspace(tmp_path, 200)
    cancelled = _CancelAfter(monkeypatch, 20)

    with pytest.raises(TimeoutError, match="cancelled"):
        import_resolver.build_python_symbol_registry(tmp_path, cancelled=cancelled)

    assert cancelled.parsed == 20, (
        f"the walk parsed {cancelled.parsed} files after cancellation at 20; an "
        "abandoned extraction must stop where it was told, not run to the end"
    )


def test_a_cancelled_walk_stops_allocating(tmp_path, monkeypatch):
    """The abandoned run must cost a fraction of the full run's allocation."""
    _workspace(tmp_path, 200)

    tracemalloc.start()
    import_resolver.build_python_symbol_registry(tmp_path)
    whole = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    cancelled = _CancelAfter(monkeypatch, 20)
    tracemalloc.start()
    with pytest.raises(TimeoutError):
        import_resolver.build_python_symbol_registry(tmp_path, cancelled=cancelled)
    abandoned = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert abandoned < whole / 2, (
        f"the abandoned run peaked at {abandoned / 2**20:.2f} MiB against the "
        f"full run's {whole / 2**20:.2f} MiB; it kept allocating after the stop"
    )


def test_an_expired_deadline_stops_the_walk_before_any_file_is_parsed(tmp_path, monkeypatch):
    """A deadline already in the past costs no parse at all."""
    _workspace(tmp_path, 50)
    counter = _CancelAfter(monkeypatch, 10**9)

    with pytest.raises(TimeoutError, match="deadline"):
        import_resolver.build_python_symbol_registry(
            tmp_path, deadline=time.monotonic() - 1.0
        )

    assert counter.parsed == 0


def test_a_deadline_that_expires_mid_walk_stops_the_walk(tmp_path, monkeypatch):
    """A live deadline stops a walk that is already running."""
    _workspace(tmp_path, 400)
    counter = _CancelAfter(monkeypatch, 10**9)

    with pytest.raises(TimeoutError, match="deadline"):
        import_resolver.build_python_symbol_registry(
            tmp_path, deadline=time.monotonic() + 0.05
        )

    assert 0 < counter.parsed < 400


def test_the_registry_is_unchanged_when_no_stop_is_requested(tmp_path):
    """Comparison, not assertion: the same bytes in, the same registry out."""
    _write(tmp_path / "pkg" / "inner.py", "def deep(): pass\nclass Thing:\n    def method(self): pass\n")
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.inner import deep as shallow\n")
    _write(tmp_path / "top" / "__init__.py", "from pkg import shallow\n")

    plain = import_resolver.build_python_symbol_registry(tmp_path)
    never = import_resolver.build_python_symbol_registry(
        tmp_path, deadline=time.monotonic() + 600.0, cancelled=lambda: False
    )

    assert plain == never
    assert "top.shallow" in plain.symbols
