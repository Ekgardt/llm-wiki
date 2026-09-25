"""A one-shot process waits for its inference thread at exit, as the server does.

See docs/research/2026-09-25-every-process-settles-its-inference-at-exit.md.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def test_the_process_does_not_leave_while_inference_runs(tmp_path: Path) -> None:
    marker = tmp_path / "finished"
    program = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(SCRIPTS)!r})
        import inference_threads
        inference_threads.start(lambda: (time.sleep(0.5), open({str(marker)!r}, "w").close()), name="inference")
        """
    )

    subprocess.run([sys.executable, "-c", program], check=True, timeout=60)

    assert marker.exists()
