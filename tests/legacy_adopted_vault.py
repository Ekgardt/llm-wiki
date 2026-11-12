"""An old planned installation, rebuilt through the real adoption for upgrade regressions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from llm_wiki.adoption_admission import expected_schema_digests

from tests.adopted_vault import adopt


def adopt_legacy(tmp_path: Path) -> tuple[Path, Path]:
    with patch("llm_wiki.adoption_admission.initial_schema_digests",
               return_value=expected_schema_digests()):
        return adopt(tmp_path)
