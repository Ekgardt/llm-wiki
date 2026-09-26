"""The vault runs from its own .venv; another environment is named, not half-used (audit 2026-09-26 B-26).

docs/research/2026-09-26-the-vault-runs-from-its-own-venv.md
"""
from __future__ import annotations

import json

import installer_config


def _plan(root, capsys, *extra: str) -> dict:
    installer_config.main(["sync-args", "--root", str(root), *extra])
    return json.loads(capsys.readouterr().out)


def test_a_custom_environment_is_named_and_not_used(tmp_path, capsys) -> None:
    plan = _plan(tmp_path, capsys, "--environment", str(tmp_path / "elsewhere"))

    assert (plan["environment"], plan["ignored_environment"]) == (
        str((tmp_path / ".venv").resolve()),
        str((tmp_path / "elsewhere").resolve()),
    )


def test_the_vault_venv_itself_is_not_called_ignored(tmp_path, capsys) -> None:
    plan = _plan(tmp_path, capsys, "--environment", str(tmp_path / ".venv"))

    assert plan["ignored_environment"] is None
