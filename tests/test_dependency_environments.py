from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from installer_config import resolve_uv_project_environment, uv_sync_arguments

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_MCP_ARGS = [
    "run",
    "--locked",
    "--no-sync",
    "--directory",
    "ROOT",
    "python",
    "scripts/mcp_server.py",
]


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_reranker_extra_owns_every_direct_import() -> None:
    reranker = _project()["project"]["optional-dependencies"]["reranker"]
    names = {re.split(r"[<>=!~;\s\[]", value, maxsplit=1)[0].casefold() for value in reranker}

    assert names == {
        "onnxruntime",
        "optimum",
        "tokenizers",
        "torch",
        "transformers",
    }
    assert sum(value.startswith("onnxruntime") for value in reranker) == 2


@pytest.mark.parametrize("override", [None, "relative environment"])
def test_project_environment_defaults_or_resolves_relative_from_vault(
    tmp_path: Path, override: str | None
) -> None:
    expected = tmp_path / (override or ".venv")
    assert resolve_uv_project_environment(tmp_path, override) == expected.resolve()


def test_project_environment_preserves_absolute_override(tmp_path: Path) -> None:
    selected = tmp_path / "outside environment"
    assert resolve_uv_project_environment(tmp_path / "vault", str(selected)) == selected.resolve()


def test_existing_non_virtual_environment_is_rejected(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    with pytest.raises(ValueError, match="not a virtual environment"):
        uv_sync_arguments(tmp_path, str(selected))


def test_fresh_install_is_locked_without_default_groups(tmp_path: Path) -> None:
    environment, arguments = uv_sync_arguments(tmp_path, None)
    assert environment == (tmp_path / ".venv").resolve()
    assert arguments == [
        "--directory",
        str(tmp_path.resolve()),
        "sync",
        "--locked",
        "--no-default-groups",
        "--quiet",
    ]


def test_reinstall_is_locked_inexact_and_preserves_selected_extras(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "custom environment"
    selected.mkdir()
    (selected / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
    environment, arguments = uv_sync_arguments(tmp_path, str(selected))
    assert environment == selected.resolve()
    assert arguments[-1] == "--inexact"
    assert arguments[:-1] == [
        "--directory",
        str(tmp_path.resolve()),
        "sync",
        "--locked",
        "--no-default-groups",
        "--quiet",
    ]


def _hook_commands(document: dict) -> list[str]:
    return [
        command
        for groups in document["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        for key in ("command", "commandWindows")
        if (command := hook.get(key))
    ]


def test_json_hook_commands_are_locked_and_no_sync() -> None:
    for path in (
        ROOT / "integrations" / "claude-code" / "settings.json",
        ROOT / "integrations" / "codex" / "hooks.json",
    ):
        commands = _hook_commands(json.loads(path.read_text(encoding="utf-8")))
        assert commands
        assert all("uv run --locked --no-sync --directory" in command for command in commands)


def test_opencode_lifecycle_command_is_locked_and_no_sync() -> None:
    source = (ROOT / "scripts" / "llm-wiki-memory-opencode.js").read_text(
        encoding="utf-8"
    )
    command = re.search(r"\[\s*\"uv\",(.*?)\],\s*payload", source, re.DOTALL)
    assert command is not None
    arguments = re.findall(r'"([^"$]+)"', command.group(1))
    assert arguments[:4] == ["run", "--locked", "--no-sync", "--directory"]


def test_codex_wrapper_unattended_commands_are_locked_and_no_sync() -> None:
    source = (ROOT / "scripts" / "codex-memory-wrapper.ps1").read_text(encoding="utf-8")
    commands = [
        line
        for line in source.splitlines()
        if "& uv run" in line or "= & uv run" in line
    ]
    assert commands
    assert all("run --locked --no-sync --directory" in line for line in commands)


def test_generated_mcp_entries_use_exact_arguments(tmp_path: Path) -> None:
    import codex_memory
    from installer_config import expected_opencode_entry

    root = tmp_path / "ROOT"
    expected = [value.replace(str(root), "ROOT") for value in expected_opencode_entry(root)["command"]]
    assert expected[1:] == EXPECTED_MCP_ARGS

    config = tmp_path / "config.toml"
    root_literal = str(root).replace("\\", "\\\\")
    args_literal = json.dumps(EXPECTED_MCP_ARGS).replace("ROOT", root_literal)
    config.write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
        f"args = {args_literal}\n"
        "enabled = true\n",
        encoding="utf-8",
    )
    assert codex_memory.codex_mcp_config_state(config, root) == "equivalent"


def test_installer_optional_commands_are_additive() -> None:
    expected = {
        f"uv sync --locked --no-default-groups --inexact --extra {extra}"
        for extra in ("hybrid", "code-graph", "reranker")
    }
    for installer in ("install.sh", "install.ps1"):
        source = (ROOT / installer).read_text(encoding="utf-8")
        commands = set(re.findall(r'"  (uv sync --locked[^"\r\n]+)"', source))
        assert expected <= commands
