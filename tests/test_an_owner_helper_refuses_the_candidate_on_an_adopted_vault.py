"""On an adopted vault an owner helper needs the vault's registry, not the candidate.

See docs/research/2026-09-25-an-owner-helper-refuses-the-candidate-on-an-adopted-vault.md.
"""

from __future__ import annotations

from pathlib import Path

import operational_ownership
import pytest


def test_the_default_registry_is_refused_once_adoption_is_recorded(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "reliability-v3-adopted.json").write_text("{}", encoding="utf-8")

    with pytest.raises(operational_ownership.OperationalOwnershipError) as raised:
        operational_ownership.acquire_scheduled_owner("nightly", state_root=tmp_path)

    assert raised.value.code == "adopted_registry_required"
