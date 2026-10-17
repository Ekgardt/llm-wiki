"""Measure one registry walk in a fresh interpreter, outside pytest's workers."""
from __future__ import annotations

import json
import sys
import tracemalloc
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_symbol_registry_cancellation import _CancelAfter, import_resolver  # noqa: E402


def _walk(root, cancelled):
    try:
        registry = import_resolver.build_python_symbol_registry(root, cancelled=cancelled)
    except TimeoutError as error:
        assert "cancelled" in str(error)
        return True, 0
    return False, len(registry.symbols)


def measure(root, stop_after=None):
    with pytest.MonkeyPatch.context() as patch:
        counter = _CancelAfter(patch, sys.maxsize if stop_after is None else stop_after)
        cancelled = None if stop_after is None else counter
        tracemalloc.start()
        try:
            stopped, symbols = _walk(root, cancelled)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    return dict(peak_bytes=peak, parsed=counter.parsed, stopped=stopped, symbols=symbols)


def main():
    stop_after = int(sys.argv[2]) if len(sys.argv) == 3 else None
    print(json.dumps(measure(Path(sys.argv[1]), stop_after)))


if __name__ == "__main__":
    main()
