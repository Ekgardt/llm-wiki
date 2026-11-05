"""`scripts` is a real package, so its relative imports have a parent module.

Static analysis and Python both learn that from `scripts/__init__.py`. Without it mypy
stopped at the first `from . import ...`, and any other regular package named `scripts`
on the path took over the name. See
`docs/research/2026-11-05-scripts-is-a-regular-package.md`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.slow_machine import SHORT_TIMEOUT

ROOT =Path(__file__).resolve().parents[1]

_REPORT_WHERE_THE_RELATIVE_IMPORT_LANDED = """
import sys
sys.path.append(sys.argv[1])
import scripts.code_graph
print(sys.modules["scripts.value_references"].__file__)
"""


def test_a_relative_import_lands_in_this_checkout_beside_another_scripts_package(tmp_path):
    other_package = tmp_path / "scripts"
    other_package.mkdir()
    (other_package / "__init__.py").write_text("", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _REPORT_WHERE_THE_RELATIVE_IMPORT_LANDED, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=SHORT_TIMEOUT,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == ROOT / "scripts" / "value_references.py"
