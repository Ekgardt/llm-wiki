"""What nothing ran is gone, and the signal the product writes has a reader.

Third audit, 2026-09-17, the dead-code rows of the retrieval area. `contextual_retrieval` and
the snapshot-tier island of `build_tiers` built generation artifacts nothing produced or read;
the LanceDB stand adapters measured a store the product retired on 2026-09-07; the refusal
counts had no caller although every dropped claim writes one.
See `docs/research/2026-09-17-what-nothing-runs-is-removed-or-reachable.md`.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.mark.parametrize("name", ["contextual_retrieval"])
def test_a_module_nothing_ran_is_gone(name: str) -> None:
    assert not (SCRIPTS / f"{name}.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


@pytest.mark.parametrize(
    "name", ["build_snapshot_tiers", "generate_l1_for_source", "tier_artifact_key"]
)
def test_the_tier_artifact_island_is_gone_and_the_page_tiers_stay(name: str) -> None:
    import build_tiers

    assert not hasattr(build_tiers, name)
    assert hasattr(build_tiers, "build_all_tiers")
    assert hasattr(build_tiers, "get_l1")


def test_the_stand_no_longer_offers_a_retired_store() -> None:
    sys.path.insert(0, str(ROOT / "benchmark"))
    import run_scale_matrix

    assert all("lance" not in adapter for adapter in run_scale_matrix.ADAPTER_IDS)


@pytest.mark.parametrize(
    "path",
    [
        "docs/ARCHITECTURE.md",
        "scripts/lookup_mode.py",
        "scripts/search_memory.py",
        "skills/knowledge-lookup/SKILL.md",
    ],
)
def test_no_document_still_calls_a_retired_store_this_product_s_engine(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8").casefold()

    assert "lancedb" not in text
    assert "lance paths" not in text


# What an interpreter needs before it can run anything at all. A Windows Python
# started without SystemRoot cannot seed its random numbers and dies before its
# first import (python/cpython#105436). The point of the environment below is
# that none of the operator's own settings reach the reader — not that the
# interpreter cannot start.
BOOT_VARIABLES = ("SystemRoot", "SYSTEMROOT", "windir", "TEMP", "TMP")


def _bare_environment(**values: str) -> dict[str, str]:
    """`values`, a default PATH, and only what the interpreter needs to boot."""
    inherited = {name: os.environ[name] for name in BOOT_VARIABLES if name in os.environ}
    return {"PATH": os.defpath, **inherited, **values}


def test_the_refusal_signal_has_a_reader_an_operator_can_run(tmp_path: Path) -> None:
    """Every dropped claim writes a `refused:<gate>` row; this prints them."""
    script = SCRIPTS / "retrieval_disposition.py"
    environment = _bare_environment(
        LLM_WIKI_ROOT=str(tmp_path / "vault"),
        LLM_WIKI_STATE_ROOT=str(tmp_path / "state"),
        PYTHONPATH=str(SCRIPTS),
    )
    (tmp_path / "vault" / "knowledge" / "notes").mkdir(parents=True)

    done = subprocess.run(
        [sys.executable, str(script)], env=environment, capture_output=True, text=True
    )

    assert done.returncode == 0
    assert "the gate that refused:" in done.stdout
