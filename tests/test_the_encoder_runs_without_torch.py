"""Whole-product encoder dependency policy; numerical tests live in its module packet."""
from __future__ import annotations

import re
from pathlib import Path

from tests.script_sources import script_files, script_key

ROOT = Path(__file__).resolve().parent.parent


def test_nothing_in_the_product_loads_the_encoder_through_sentence_transformers() -> None:
    offenders = [
        script_key(path)
        for path in script_files()
        if re.search(r"^\s*(from|import) sentence_transformers", path.read_text(encoding="utf-8"), re.M)
    ]

    assert offenders == []


def test_the_search_extras_do_not_pull_torch() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    semantic = pyproject[pyproject.index("semantic = [") : pyproject.index("hybrid = [")]

    assert [name for name in ("torch", "sentence-transformers", "optimum") if name in semantic] == []
