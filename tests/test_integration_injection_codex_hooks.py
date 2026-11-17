"""The Codex hook template and merge: user hooks preserved, owned backups pruned, inline hooks and the feature and MCP config states honoured."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.breadcrumb_worker_helpers import ingress as ingress
from tests.integration_injection_case_helpers import (
    _codex_inline_hooks_toml,
)
from tests.integration_injection_helpers import (
    ROOT,
    _ensure_scripts_on_path,
)


def _hook_handlers(merged: dict) -> list[dict]:
    return [
        handler
        for groups in merged["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]


def _codex_memory_handlers(merged: dict) -> list[dict]:
    handlers = _hook_handlers(merged)
    return [handler for handler in handlers if "codex_memory.py" in handler.get("command", "")]


def _stop_hook_commands(merged: dict) -> list[str]:
    groups = merged["hooks"]["Stop"]
    return [handler.get("command") for group in groups for handler in group["hooks"]]


# (event, group) -> (matcher, script, argument tail). Lifecycle: codex_memory;
# #24 C2: the graph hint after a shell search and the reminder; 2026-09-11:
# prompt and edit capture (docs/research/2026-09-11-codex-leaves-breadcrumbs-too.md).
_LIFECYCLE = ("codex_memory.py", " hook")


_GRAPH = ("graph_hint.py", " --source codex")


_CODEX_CONTRACT = {
    ("SessionStart", 0): ("startup|resume|clear|compact", *_LIFECYCLE),
    ("PreCompact", 0): ("manual|auto", *_LIFECYCLE),
    ("PostCompact", 0): ("manual|auto", *_LIFECYCLE),
    ("UserPromptSubmit", 0): (
        None,
        "integration_adapter.py",
        " --source codex --event user_prompt",
    ),
    ("PostToolUse", 0): ("Bash", *_GRAPH),
    ("PostToolUse", 1): (
        "^(apply_patch|Bash)$",
        "integration_adapter.py",
        " --source codex --event post_tool_use",
    ),
    ("SubagentStart", 0): (None, *_GRAPH),
    ("Stop", 0): (None, *_LIFECYCLE),
}


def test_codex_official_hook_template_matches_supported_contract():
    template = json.loads(
        (ROOT / "integrations" / "codex" / "hooks.json").read_text(encoding="utf-8")
    )
    groups = _codex_template_groups(template["hooks"])

    assert set(groups) == set(_CODEX_CONTRACT)
    assert [key for key, group in groups.items() if not _group_keeps_contract(key, group)] == []


def _codex_template_groups(hooks: dict) -> dict:
    return {
        (event, index): group
        for event, event_groups in hooks.items()
        for index, group in enumerate(event_groups)
    }


def _command_texts_end_with(command: dict, script: str, ending: str) -> bool:
    texts = (command["command"], command["commandWindows"])
    return all(script in text and text.endswith(ending) for text in texts)


def _group_keeps_contract(key: tuple[str, int], group: dict) -> bool:
    matcher, script, ending = _CODEX_CONTRACT[key]
    (command,) = group["hooks"]
    return (
        group.get("matcher") == matcher
        and command["type"] == "command"
        and _command_texts_end_with(command, script, ending)
        and _codex_timeout_keeps_contract(key, command)
    )


def _codex_timeout_keeps_contract(key: tuple[str, int], command: dict) -> bool:
    if key in {("UserPromptSubmit", 0), ("PostToolUse", 1)}:
        return "timeout" not in command
    return 0 < command["timeout"] <= 15


def test_codex_hook_merge_preserves_user_hooks_and_is_idempotent(tmp_path):
    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    destination = tmp_path / "hooks.json"
    original = (
        json.dumps(
            {
                "custom": {"preserved": True},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "echo user"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old scripts/codex_memory.py hook",
                                }
                            ]
                        },
                    ]
                },
            }
        )
        + "\r\n"
    )
    destination.write_bytes(original.encode("utf-8"))

    codex_memory.merge_codex_hooks(source, destination)
    first = destination.read_bytes()
    backups = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert (
        len(backups),
        backups[0].read_bytes(),
    ) == (
        1,
        original.encode("utf-8"),
    )
    codex_memory.merge_codex_hooks(source, destination)
    merged = json.loads(destination.read_text(encoding="utf-8"))

    assert (
        destination.read_bytes(),
        list(tmp_path.glob("hooks.json.bak-llm-wiki-*")),
        merged["custom"],
    ) == (
        first,
        backups,
        {"preserved": True},
    )
    assert "echo user" in _stop_hook_commands(merged)
    assert len(_codex_memory_handlers(merged)) == 4


def _owned_backup_mtime(now: float, index: int) -> float:
    if index == 0:
        return now - 91 * 24 * 60 * 60
    return now - (11 - index)


def _seed_owned_backups(tmp_path: Path, now: float, count: int) -> Path:
    for index in range(count):
        backup = tmp_path / f"hooks.json.bak-llm-wiki-20260801-000000-{index:06d}"
        backup.write_bytes(str(index).encode("ascii"))
        modified = _owned_backup_mtime(now, index)
        os.utime(backup, (modified, modified))
    return tmp_path / "hooks.json.bak-llm-wiki-20260801-000000-000000"


def test_codex_hook_merge_prunes_owned_backups_but_keeps_newest_and_unrelated(tmp_path):
    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    destination = tmp_path / "hooks.json"
    original = b'{"hooks":{}}\n'
    destination.write_bytes(original)
    now = time.time()
    old = _seed_owned_backups(tmp_path, now, 11)
    unrelated = tmp_path / "hooks.json.backup"
    unrelated.write_bytes(b"keep")

    codex_memory.merge_codex_hooks(source, destination)

    backups = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert not old.exists()
    assert sum(path.stat().st_size for path in backups) <= 100 * 1024 * 1024
    assert original in [path.read_bytes() for path in backups]
    assert unrelated.read_bytes() == b"keep"


def test_codex_hook_merge_skips_equivalent_inline_hooks(tmp_path):

    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(), encoding="utf-8")
    destination.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n',
        encoding="utf-8",
    )
    before = destination.read_bytes()

    result = codex_memory.merge_codex_hooks(source, destination, config=config)

    assert result == "inline-equivalent"
    assert destination.read_bytes() == before


def test_codex_hook_command_reports_equivalent_inline_without_creating_json(tmp_path, capsys):
    import argparse

    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(), encoding="utf-8")

    result = codex_memory.command_merge_hooks(
        argparse.Namespace(source=str(source), destination=str(destination), config=str(config))
    )

    assert result == 3
    assert capsys.readouterr().out.strip() == "inline-equivalent"
    assert not destination.exists()


def test_codex_hook_merge_preserves_json_when_inline_is_equivalent(tmp_path):

    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(), encoding="utf-8")
    installed = json.loads(source.read_text(encoding="utf-8"))
    installed["custom"] = True
    installed["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "echo user"}]})
    destination.write_text(json.dumps(installed), encoding="utf-8")

    result = codex_memory.merge_codex_hooks(source, destination, config=config)

    assert result == "inline-equivalent"
    assert json.loads(destination.read_text(encoding="utf-8")) == installed


def test_codex_hook_merge_rejects_unrelated_inline_hooks_without_creating_json(tmp_path):

    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "echo user"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manual merge and trust review required"):
        codex_memory.merge_codex_hooks(source, destination, config=config)

    assert not destination.exists()


def test_codex_hook_merge_rejects_partial_inline_hooks_without_writing(tmp_path):

    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text(_codex_inline_hooks_toml(include_stop=False), encoding="utf-8")
    destination.write_text('{"custom":true}\n', encoding="utf-8")
    before = destination.read_bytes()

    with pytest.raises(ValueError, match="manual merge and trust review required"):
        codex_memory.merge_codex_hooks(source, destination, config=config)

    assert destination.read_bytes() == before


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        ("hooks = false", "disabled"),
        ("codex_hooks = false", "disabled"),
        ("hooks = true\ncodex_hooks = false", "enabled"),
        ("hooks = false\ncodex_hooks = true", "disabled"),
        ("hooks = true", "enabled"),
        ("", "enabled"),
    ],
    ids=[
        "canonical-disabled",
        "alias-disabled",
        "canonical-enabled-wins",
        "canonical-disabled-wins",
        "canonical-enabled",
        "default-enabled",
    ],
)
def test_codex_hooks_feature_state_obeys_canonical_precedence(tmp_path, features, expected):

    _ensure_scripts_on_path()
    import codex_config_state
    import codex_memory

    config = tmp_path / "config.toml"
    body = f"[features]\n{features}\n" if features else 'model = "gpt-5.6"\n'
    config.write_text(body, encoding="utf-8")

    state = codex_memory.codex_hooks_feature_state(codex_config_state.read_codex_toml(config))

    assert state == expected


def test_codex_hook_command_reports_disabled_without_writing(tmp_path, capsys):
    import argparse

    _ensure_scripts_on_path()
    import codex_memory

    source = ROOT / "integrations" / "codex" / "hooks.json"
    config = tmp_path / "config.toml"
    destination = tmp_path / "hooks.json"
    config.write_text("[features]\nhooks = false\n", encoding="utf-8")

    result = codex_memory.command_merge_hooks(
        argparse.Namespace(source=str(source), destination=str(destination), config=str(config))
    )

    assert result == 4
    assert capsys.readouterr().out.strip() == "hooks-disabled"
    assert not destination.exists()


@pytest.mark.parametrize("quoted", [False, True], ids=["dotted", "quoted"])
def test_codex_mcp_config_state_accepts_exact_enabled_table(tmp_path, quoted):

    _ensure_scripts_on_path()
    import codex_config_state

    config = tmp_path / "config.toml"
    table = '[mcp_servers."llm-wiki"]' if quoted else "[mcp_servers.llm-wiki]"
    from integration_hook_config import codex_launch_args

    args = codex_launch_args(ROOT, "mcp_server.py", (), relative_target=True)
    config.write_text(
        f'{table}\ncommand = "uv"\nargs = {json.dumps(args)}\nenabled = true\n',
        encoding="utf-8",
    )

    assert codex_config_state.codex_mcp_config_state(config, ROOT) == "equivalent"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('model = "gpt-5.6"\n', "absent"),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["python", "other.py"]\n',
            "conflict",
        ),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
            'args = ["run", "--locked", "--no-sync", "--directory", "wrong", "python", '
            '"scripts/mcp_server.py"]\nenabled = false\n',
            "disabled",
        ),
        (
            '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
            'args = ["run", "--directory", "elsewhere", "python", "scripts/mcp_server.py"]\n',
            "stale",
        ),
    ],
)
def test_codex_mcp_config_state_distinguishes_absent_and_conflicting(tmp_path, body, expected):

    _ensure_scripts_on_path()
    import codex_config_state

    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")

    assert codex_config_state.codex_mcp_config_state(config, ROOT) == expected
