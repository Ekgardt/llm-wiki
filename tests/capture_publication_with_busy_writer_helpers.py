"""Helpers shared by test_capture_publication_with_busy_writer.py and the test files importing them."""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from tests.slow_machine import LONG_TIMEOUT

CHECKOUT = Path(__file__).resolve().parents[1]


@contextmanager
def _other_process_writer(state_root: Path):
    command = """
import sys
from pathlib import Path
from tests.queue_v3_capture_links_helpers import _coordinator
with _coordinator(Path(sys.argv[1])).writer_gate():
    print('writer-held', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", command, str(state_root)],
        cwd=CHECKOUT, env={**os.environ, "PYTHONPATH": str(CHECKOUT / "scripts")},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "writer-held"
        yield
    finally:
        _, error = process.communicate("", timeout=LONG_TIMEOUT)
    assert process.returncode == 0, error
