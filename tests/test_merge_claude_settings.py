"""Tests for merge_claude_settings (safe Claude user-settings merge)."""
from __future__ import annotations

import json
import multiprocessing
import re
import stat
import traceback
from pathlib import Path
from types import SimpleNamespace

import merge_claude_settings as mcs
import pytest


def _concurrent_claude_merge_worker(
    target: Path,
    template: Path,
    vault_root: str,
    state_root: str,
    entered,
    release,
    pause: bool,
    results,
) -> None:
    import merge_claude_settings as worker_mcs

    original_merge = worker_mcs.merge_settings

    def controlled_merge(*args, **kwargs):
        entered.set()
        if pause and not release.wait(15):
            raise TimeoutError("test did not release paused Claude merger")
        return original_merge(*args, **kwargs)

    worker_mcs.merge_settings = controlled_merge
    try:
        worker_mcs.apply_merge(target, template, vault_root, state_root)
    except BaseException:
        results.put(traceback.format_exc())
    else:
        results.put("")


def _hook_handlers(settings: dict) -> list[dict]:
    return [
        hook
        for blocks in settings.get("hooks", {}).values()
        for block in blocks
        for hook in block.get("hooks", [])
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("2.1.114 (Claude Code)", (2, 1, 114)),
        ("claude version 2.1.139", (2, 1, 139)),
        ("Claude Code v3.0.0-beta.1", (3, 0, 0)),
        ("wrapper 9.9.9 before 2.1.139 (Claude Code)", None),
        ("Node.js 22.0.0 is required", None),
        ("not installed", None),
    ),
)
def test_parse_claude_version(raw, expected):
    assert mcs.parse_claude_version(raw) == expected


def test_claude_2_1_114_materializes_absolute_posix_shell_hooks_safely():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    vault = "/tmp/O'Brien vault; printf unsafe"

    merged = mcs.merge_settings(
        {},
        template,
        vault,
        "/state",
        claude_version="2.1.114 (Claude Code)",
        legacy_shell="bash",
    )
    ours = _hook_handlers(merged)

    assert len(ours) == len(mcs.OUR_SCRIPT_MARKERS)
    for hook in ours:
        assert "args" not in hook
        assert hook["shell"] == "bash"
        command = hook["command"]
        assert command.startswith("'uv' 'run' '--directory' ")
        assert "scripts/" in command
        assert "$LLM_WIKI_ROOT" not in command
        assert "'\"'\"'" in command
        assert f"{vault}/scripts/" not in command


def test_claude_2_1_114_materializes_absolute_powershell_hooks_safely():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    vault = "C:/Memory O'Brien; Write-Output unsafe"

    merged = mcs.merge_settings(
        {},
        template,
        vault,
        "C:/state",
        claude_version="2.1.114",
        legacy_shell="powershell",
    )
    ours = _hook_handlers(merged)

    assert len(ours) == len(mcs.OUR_SCRIPT_MARKERS)
    for hook in ours:
        assert "args" not in hook
        assert hook["shell"] == "powershell"
        command = hook["command"]
        assert command.startswith("& 'uv' 'run' '--directory' ")
        assert "scripts\\" in command
        assert "O''Brien" in command
        assert "$LLM_WIKI_ROOT" not in command


def test_failed_version_detection_uses_legacy_shell_form(monkeypatch):
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    monkeypatch.setattr(
        mcs.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Node.js 22.0.0 is required",
        ),
    )
    detected = mcs.detect_claude_version()

    merged = mcs.merge_settings(
        {},
        template,
        "/vault",
        "/state",
        claude_version=detected,
        legacy_shell="bash",
    )

    assert detected is None
    assert all(
        "args" not in hook and hook.get("shell") == "bash"
        for hook in _hook_handlers(merged)
    )


def test_exec_hook_form_starts_at_claude_2_1_139():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))

    legacy = mcs.merge_settings(
        {}, template, "/vault", "/state", claude_version="2.1.138 (Claude Code)"
    )
    modern = mcs.merge_settings(
        {}, template, "/vault", "/state", claude_version="2.1.139 (Claude Code)"
    )

    assert all("args" not in hook for hook in _hook_handlers(legacy))
    assert all(isinstance(hook.get("args"), list) for hook in _hook_handlers(modern))


def test_legacy_remerge_is_idempotent_and_preserves_unrelated_hook():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    unrelated = {"type": "command", "command": "echo keep-me"}
    user = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [unrelated]}]}}

    merged = mcs.merge_settings(
        user,
        template,
        "/vault with spaces",
        "/state",
        claude_version="2.1.114",
        legacy_shell="bash",
    )
    remerged = mcs.merge_settings(
        merged,
        template,
        "/vault with spaces",
        "/state",
        claude_version="2.1.114",
        legacy_shell="bash",
    )
    handlers = _hook_handlers(remerged)

    assert handlers.count(unrelated) == 1
    assert sum(
        any(marker in hook.get("command", "") for marker in mcs.OUR_SCRIPT_MARKERS)
        for hook in handlers
    ) == len(mcs.OUR_SCRIPT_MARKERS)


def test_installers_select_their_legacy_shell():
    root = mcs._default_template().parents[2]

    assert "--legacy-shell bash" in (root / "install.sh").read_text(encoding="utf-8")
    assert "--legacy-shell powershell" in (root / "install.ps1").read_text(
        encoding="utf-8"
    )


def test_default_session_start_matcher_includes_fork():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))

    [session_start] = template["hooks"]["SessionStart"]

    assert "fork" in session_start["matcher"].split("|")


def test_default_claude_hooks_carry_explicit_ownership_marker():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))

    assert all(
        hook.get("statusMessage", "").startswith("[LLM Wiki] ")
        for hook in _hook_handlers(template)
    )


def test_marked_claude_hook_is_replaced_after_vault_move():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    marked_old = {
        "type": "command",
        "command": "python",
        "args": ["D:/old-vault/scripts/renamed-wrapper.py"],
        "statusMessage": "[LLM Wiki] Previous generated hook",
    }
    unrelated = {
        "type": "command",
        "command": "echo keep",
        "statusMessage": "LLM Wiki user note",
    }
    user = {
        "env": {"LLM_WIKI_ROOT": "D:/old-vault"},
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [marked_old, unrelated]}
            ]
        },
    }

    merged = mcs.merge_settings(user, template, "D:/new-vault", "D:/state")
    handlers = _hook_handlers(merged)

    assert marked_old not in handlers
    assert unrelated in handlers


def test_unmarked_generated_claude_hook_uses_previous_configured_vault_root():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    old_root = "D:/old vault"
    previous = mcs.merge_settings({}, template, old_root, "D:/state")
    for hook in _hook_handlers(previous):
        hook.pop("statusMessage", None)

    merged = mcs.merge_settings(previous, template, "D:/new vault", "D:/state")
    handlers = _hook_handlers(merged)

    assert not any(old_root in str(hook) for hook in handlers)
    assert sum(
        any(marker in str(hook) for marker in mcs.OUR_SCRIPT_MARKERS)
        for hook in handlers
    ) == len(mcs.OUR_SCRIPT_MARKERS)


def test_default_hooks_materialize_direct_argv_and_remerge_idempotently():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))
    unrelated = {
        "type": "command",
        "command": "uv",
        "args": ["run", "unrelated.py"],
    }
    user = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [unrelated]}]}}

    merged = mcs.merge_settings(user, template, "/vault with spaces", "/state")
    remerged = mcs.merge_settings(merged, template, "/vault with spaces", "/state")
    handlers = _hook_handlers(remerged)
    ours = [
        hook
        for hook in handlers
        if any(
            marker in str(arg)
            for marker in mcs.OUR_SCRIPT_MARKERS
            for arg in hook.get("args", [])
        )
    ]

    assert len(ours) == len(mcs.OUR_SCRIPT_MARKERS)
    assert sum(hook == unrelated for hook in handlers) == 1
    for hook in ours:
        assert hook["command"] == "uv"
        assert hook["args"][:4] == [
            "run",
            "--directory",
            "/vault with spaces",
            "python",
        ]
        assert all("$LLM_WIKI_ROOT" not in arg for arg in hook["args"])
    context_hook = next(
        hook for hook in ours if "scripts/session_start_context.py" in hook["args"]
    )
    assert context_hook["args"] == [
        "run",
        "--directory",
        "/vault with spaces",
        "python",
        "scripts/session_start_context.py",
        "--omit-project-state",
    ]


def test_merge_keeps_user_hooks_and_replaces_ours(tmp_path):
    user = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "echo user-hook", "timeout": 5},
                        {
                            "type": "command",
                            "command": "uv run python scripts/session_start_context.py",
                            "timeout": 15,
                        },
                    ],
                }
            ],
            "Notification": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "echo notify"}],
                }
            ],
        },
        "permissions": {"allow": ["Bash(echo *)"], "deny": []},
        "env": {"OTHER": "keep-me"},
    }
    template = {
        "autoMemoryEnabled": True,
        "permissions": {
            "allow": ["Bash(uv run --directory *)"],
            "deny": ["Read(./.env)"],
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'uv run --directory "$LLM_WIKI_ROOT" python scripts/session_start_context.py --omit-project-state',
                            "timeout": 15,
                        }
                    ],
                }
            ]
        },
    }
    merged = mcs.merge_settings(user, template, "/vault", "/state")
    assert merged["env"]["OTHER"] == "keep-me"
    assert merged["env"]["LLM_WIKI_ROOT"] == "/vault"
    assert merged["env"]["LLM_WIKI_STATE_ROOT"] == "/state"
    assert "Bash(echo *)" in merged["permissions"]["allow"]
    assert "Bash(uv run --directory *)" in merged["permissions"]["allow"]
    assert "Read(./.env)" in merged["permissions"]["deny"]

    ss = merged["hooks"]["SessionStart"]
    cmds = []
    for block in ss:
        for h in block.get("hooks", []):
            cmds.append(h["command"])
    assert any("echo user-hook" in c for c in cmds)
    assert any("$LLM_WIKI_ROOT" in c and "session_start_context" in c for c in cmds)
    context_command = next(c for c in cmds if "session_start_context.py" in c)
    assert "--omit-project-state" in context_command
    # Old relative ours removed
    assert not any(c == "uv run python scripts/session_start_context.py" for c in cmds)
    # Unrelated event preserved
    assert merged["hooks"]["Notification"]


def test_apply_merge_writes_backup(tmp_path):
    user_path = tmp_path / "settings.json"
    user_path.write_text(json.dumps({"env": {"X": "1"}}), encoding="utf-8")
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "uv run python scripts/session_end_capture.py",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    mcs.apply_merge(user_path, template, "/v", "/s", dry_run=False)
    data = json.loads(user_path.read_text(encoding="utf-8"))
    assert data["env"]["LLM_WIKI_ROOT"] == "/v"
    backups = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert len(backups) == 1


def test_apply_merge_refuses_malformed_existing_json_without_touching_target(
    tmp_path,
):
    user_path = tmp_path / "settings.json"
    original = b'{"hooks": broken}\r\n'
    user_path.write_bytes(original)

    with pytest.raises(ValueError, match="cannot safely read existing Claude settings"):
        mcs.apply_merge(user_path, mcs._default_template(), "/v", "/s")

    assert user_path.read_bytes() == original
    assert list(tmp_path.glob("settings.json.bak-llm-wiki-*")) == []


def test_apply_merge_refuses_non_object_existing_json_without_touching_target(
    tmp_path,
):
    user_path = tmp_path / "settings.json"
    original = b'["valid JSON", "wrong top-level type"]\n'
    user_path.write_bytes(original)

    with pytest.raises(ValueError, match="existing Claude settings must be a JSON object"):
        mcs.apply_merge(user_path, mcs._default_template(), "/v", "/s")

    assert user_path.read_bytes() == original
    assert list(tmp_path.glob("settings.json.bak-llm-wiki-*")) == []


def test_load_json_requests_an_explicit_byte_bound(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"env":{"SAFE":"1"}}\n', encoding="utf-8")
    real_open = Path.open
    real_read_text = Path.read_text
    read_sizes: list[int] = []

    class TrackingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.handle.read(size)

    def reject_read_text(path, *args, **kwargs):
        if path == settings:
            raise AssertionError("Claude settings read must be byte-bounded")
        return real_read_text(path, *args, **kwargs)

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == settings and "r" in mode:
            assert "b" in mode
            return TrackingFile(handle)
        return handle

    monkeypatch.setattr(Path, "read_text", reject_read_text)
    monkeypatch.setattr(Path, "open", tracking_open)

    assert mcs._load_json(settings) == {"env": {"SAFE": "1"}}
    assert read_sizes and all(size > 0 for size in read_sizes)


def test_apply_merge_refuses_oversized_existing_json_without_touching_target(
    tmp_path,
):
    user_path = tmp_path / "settings.json"
    original = b'{"padding":"' + (b"x" * (mcs.MAX_SETTINGS_JSON_BYTES + 1)) + b'"}\n'
    user_path.write_bytes(original)

    with pytest.raises(ValueError, match="exceeds byte limit"):
        mcs.apply_merge(user_path, mcs._default_template(), "/v", "/s")

    assert user_path.read_bytes() == original
    assert list(tmp_path.glob("settings.json.bak-llm-wiki-*")) == []


def test_default_template_omits_duplicate_project_state_from_context_hook():
    template = json.loads(mcs._default_template().read_text(encoding="utf-8"))

    merged = mcs.merge_settings({}, template, "/vault", "/state")
    hooks = [
        hook
        for block in merged["hooks"]["SessionStart"]
        for hook in block["hooks"]
    ]

    context_hook = next(
        hook
        for hook in hooks
        if any("session_start_context.py" in arg for arg in hook.get("args", []))
    )
    assert "--omit-project-state" in context_hook["args"]
    assert sum(
        any("session_start_project_state.py" in arg for arg in hook.get("args", []))
        for hook in hooks
    ) == 1


def test_merge_preserves_unowned_same_basename_hooks():
    unrelated = [
        {
            "type": "command",
            "command": "echo session_start_context.py",
        },
        {
            "type": "command",
            "command": "uv",
            "args": [
                "run",
                "--directory",
                "/other/vault",
                "python",
                "scripts/session_start_context.py",
            ],
        },
        {
            "type": "command",
            "command": "python",
            "args": ["/other/vault/scripts/session_start_context.py"],
        },
        {
            "type": "command",
            "command": "python",
            "args": ["/srv/vault/scripts/session_start_context.py"],
        },
    ]
    owned_legacy = {
        "type": "command",
        "command": "uv run python scripts/session_start_context.py",
    }
    user = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [*unrelated, owned_legacy]}
            ]
        }
    }
    template = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv",
                            "args": [
                                "run",
                                "--directory",
                                "$LLM_WIKI_ROOT",
                                "python",
                                "scripts/session_start_context.py",
                            ],
                        }
                    ],
                }
            ]
        }
    }

    merged = mcs.merge_settings(user, template, "/srv/Vault", "/state")
    handlers = _hook_handlers(merged)

    assert all(hook in handlers for hook in unrelated)
    assert owned_legacy not in handlers


def test_merge_replaces_current_windows_vault_exec_paths_across_separators():
    owned = [
        {
            "type": "command",
            "command": "uv",
            "args": [
                "run",
                "--directory",
                "c:\\memory vault\\",
                "python",
                "scripts\\session_start_context.py",
            ],
        },
        {
            "type": "command",
            "command": "python",
            "args": ["c:\\memory vault\\scripts\\session_start_context.py"],
        },
    ]
    user = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": owned}]
        }
    }
    template = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv",
                            "args": [
                                "run",
                                "--directory",
                                "$LLM_WIKI_ROOT",
                                "python",
                                "scripts/session_start_context.py",
                            ],
                        }
                    ],
                }
            ]
        }
    }

    merged = mcs.merge_settings(user, template, "C:/Memory Vault", "C:/state")
    handlers = _hook_handlers(merged)

    assert all(hook not in handlers for hook in owned)
    assert len(handlers) == 1
    assert handlers[0]["args"][2] == "C:/Memory Vault"


def _minimal_claude_template(event: str = "SessionStart") -> dict:
    return {
        "hooks": {
            event: [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "echo managed"}],
                }
            ]
        }
    }


def _claude_hook_document(handler: dict, **block_metadata) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    **block_metadata,
                    "hooks": [handler],
                }
            ]
        }
    }


@pytest.mark.parametrize(
    ("user", "template", "message"),
    (
        ({"env": []}, _minimal_claude_template(), "user env"),
        ({}, {"env": [], **_minimal_claude_template()}, "template env"),
        ({"permissions": []}, _minimal_claude_template(), "user permissions"),
        ({}, {"permissions": [], **_minimal_claude_template()}, "template permissions"),
        (
            {"permissions": {"allow": "Bash(*)"}},
            _minimal_claude_template(),
            "user permissions.allow",
        ),
        (
            {},
            {"permissions": {"deny": [7]}, **_minimal_claude_template()},
            "template permissions.deny",
        ),
        ({"hooks": []}, _minimal_claude_template(), "user hooks"),
        ({}, {"hooks": []}, "template hooks"),
        (
            {"hooks": {"SessionStart": {}}},
            _minimal_claude_template(),
            "user hooks.SessionStart",
        ),
        ({}, {"hooks": {"SessionStart": {}}}, "template hooks.SessionStart"),
        (
            {"hooks": {"SessionStart": ["not-an-object"]}},
            _minimal_claude_template(),
            "user hooks.SessionStart block",
        ),
        (
            {},
            {"hooks": {"SessionStart": ["not-an-object"]}},
            "template hooks.SessionStart block",
        ),
        (
            {"hooks": {"SessionStart": [{"hooks": {}}]}},
            _minimal_claude_template(),
            "user hooks.SessionStart block hooks",
        ),
        (
            {},
            {"hooks": {"SessionStart": [{"hooks": {}}]}},
            "template hooks.SessionStart block hooks",
        ),
        (
            {"hooks": {"SessionStart": [{"hooks": ["not-an-object"]}]}},
            _minimal_claude_template(),
            "user hooks.SessionStart handler",
        ),
        (
            {},
            {"hooks": {"SessionStart": [{"hooks": ["not-an-object"]}]}},
            "template hooks.SessionStart handler",
        ),
    ),
)
def test_apply_merge_rejects_invalid_managed_structure_without_backup_or_write(
    tmp_path,
    user,
    template,
    message,
):
    target = tmp_path / "settings.json"
    original = (json.dumps(user, separators=(",", ":")) + "\r\n").encode()
    target.write_bytes(original)
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(ValueError, match=".*".join(map(re.escape, message.split()))):
        mcs.apply_merge(target, template_path, "/vault", "/state")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("settings.json.bak-llm-wiki-*"))


@pytest.mark.parametrize("key", ("LLM_WIKI_ROOT", "LLM_WIKI_STATE_ROOT"))
@pytest.mark.parametrize(
    "invalid_value",
    (
        pytest.param(None, id="null"),
        pytest.param(7, id="number"),
        pytest.param([], id="list"),
        pytest.param({"path": "/wrong"}, id="object"),
    ),
)
def test_apply_merge_rejects_non_string_managed_user_env_without_backup_or_write(
    tmp_path,
    key,
    invalid_value,
):
    user = {"env": {"OTHER": "keep", key: invalid_value}}
    target = tmp_path / "settings.json"
    original = (json.dumps(user, separators=(",", ":")) + "\r\n").encode()
    target.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_minimal_claude_template()), encoding="utf-8")

    with pytest.raises(ValueError, match=key):
        mcs.apply_merge(target, template, "/vault", "/state")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("settings.json.bak-llm-wiki-*"))


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    (
        pytest.param("$schema", 7, id="schema"),
        pytest.param("autoMemoryEnabled", "yes", id="auto-memory"),
    ),
)
def test_apply_merge_rejects_insertable_malformed_known_claude_top_level_field(
    tmp_path,
    key,
    invalid_value,
):
    user = {"futureTop": {"keep": True}}
    target = tmp_path / "settings.json"
    original = (json.dumps(user, separators=(",", ":")) + "\r\n").encode()
    target.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({key: invalid_value, **_minimal_claude_template()}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape(key)):
        mcs.apply_merge(target, template, "/vault", "/state")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("settings.json.bak-llm-wiki-*"))


@pytest.mark.parametrize(
    ("key", "template_value"),
    (
        ("$schema", 7),
        ("autoMemoryEnabled", "yes"),
    ),
)
def test_merge_preserves_existing_opaque_known_claude_top_level_field(
    key,
    template_value,
):
    existing = {"future": [1, 2, 3]}
    user = {key: existing}
    template = {key: template_value, **_minimal_claude_template()}

    merged = mcs.merge_settings(user, template, "/vault", "/state")

    assert merged[key] == existing


@pytest.mark.parametrize("side", ("user", "template"))
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        pytest.param("command", 7, id="command"),
        pytest.param("statusMessage", [], id="status-message"),
        pytest.param("shell", {}, id="shell"),
        pytest.param("args", ["valid", 7], id="args"),
        pytest.param("timeout", True, id="timeout-bool"),
        pytest.param("timeout", -1, id="timeout-negative"),
        pytest.param("timeout", 1.5, id="timeout-fraction"),
        pytest.param("timeout", "5", id="timeout-string"),
        pytest.param("async", "yes", id="async"),
    ),
)
def test_apply_merge_rejects_malformed_known_claude_handler_fields_before_write(
    tmp_path,
    side,
    field,
    invalid_value,
):
    handler = {"type": "command", "command": "echo user", field: invalid_value}
    invalid_document = _claude_hook_document(handler)
    user = invalid_document if side == "user" else {}
    template_data = (
        invalid_document if side == "template" else _minimal_claude_template()
    )
    target = tmp_path / "settings.json"
    original = (json.dumps(user, separators=(",", ":")) + "\r\n").encode()
    target.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(template_data), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        mcs.apply_merge(target, template, "/vault", "/state")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("settings.json.bak-llm-wiki-*"))


def test_merge_accepts_valid_known_claude_handler_fields_and_unknown_metadata():
    handler = {
        "type": "command",
        "command": "echo user",
        "statusMessage": "User hook",
        "shell": "bash",
        "args": ["one", "two"],
        "timeout": 0,
        "async": False,
        "futureField": {"nested": True},
    }

    merged = mcs.merge_settings(
        _claude_hook_document(handler),
        _minimal_claude_template(),
        "/vault",
        "/state",
    )

    assert merged["hooks"]["SessionStart"][0]["hooks"] == [handler]


def test_merge_preserves_empty_managed_block_with_future_metadata():
    block = {
        "matcher": "startup",
        "futureBlockMetadata": {"mode": "keep"},
        "hooks": [],
    }
    user = {"hooks": {"SessionStart": [block]}}

    merged = mcs.merge_settings(user, _minimal_claude_template(), "/vault", "/state")

    assert block in merged["hooks"]["SessionStart"]


def test_merge_preserves_future_block_metadata_after_removing_owned_handler():
    owned = {
        "type": "command",
        "command": "echo old generated hook",
        "statusMessage": "[LLM Wiki] Previous generated hook",
    }
    metadata_block = {
        "matcher": "startup",
        "futureBlockMetadata": {"mode": "keep"},
        "hooks": [owned],
    }
    structural_only = {"matcher": "resume", "hooks": [owned]}
    user = {"hooks": {"SessionStart": [metadata_block, structural_only]}}

    merged = mcs.merge_settings(user, _minimal_claude_template(), "/vault", "/state")

    assert {
        "matcher": "startup",
        "futureBlockMetadata": {"mode": "keep"},
        "hooks": [],
    } in merged["hooks"]["SessionStart"]
    assert {"matcher": "resume", "hooks": []} not in merged["hooks"]["SessionStart"]


def test_merge_preserves_unmanaged_and_unknown_structure_exactly():
    opaque_event = [
        "non-object block",
        {"matcher": {"future": True}, "hooks": "future-schema"},
        {"nested": [1, {"x": None}]},
    ]
    opaque_top = {"list": [1, False, {"future": "value"}]}
    user = {
        "futureTop": opaque_top,
        "env": {"OTHER": {"future": [1, 2]}},
        "permissions": {"futurePolicy": {"nested": [1, 2]}},
        "hooks": {
            "FutureEvent": opaque_event,
            "SessionStart": [
                {"matcher": "", "hooks": [{"command": "echo user"}]}
            ],
        },
    }

    merged = mcs.merge_settings(
        user,
        _minimal_claude_template(),
        "/vault",
        "/state",
    )

    assert merged["futureTop"] == opaque_top
    assert merged["env"]["OTHER"] == user["env"]["OTHER"]
    assert merged["permissions"]["futurePolicy"] == user["permissions"]["futurePolicy"]
    assert merged["hooks"]["FutureEvent"] == opaque_event


def test_apply_merge_dry_run_creates_no_parent_lock_backup_or_target(tmp_path):
    target = tmp_path / "missing" / "settings.json"

    mcs.apply_merge(target, mcs._default_template(), "/vault", "/state", dry_run=True)

    assert not target.parent.exists()


def test_apply_merge_creates_unique_exact_backups_and_preserves_mode(tmp_path):
    target = tmp_path / "settings.json"
    original = b'{\r\n  "env": {"BASE": "one"}\r\n}\r\n'
    target.write_bytes(original)
    target.chmod(0o640)
    expected_mode = stat.S_IMODE(target.stat().st_mode)

    mcs.apply_merge(target, mcs._default_template(), "/vault", "/state")
    second_base = target.read_bytes()
    mcs.apply_merge(target, mcs._default_template(), "/vault", "/state")

    backups = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert len(backups) == 2
    assert {backup.read_bytes() for backup in backups} == {original, second_base}
    assert stat.S_IMODE(target.stat().st_mode) == expected_mode


def test_apply_merge_failure_before_replace_keeps_parseable_target_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "settings.json"
    original = b'{"env":{"BASE":"unchanged"}}\n'
    target.write_bytes(original)

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(mcs.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        mcs.apply_merge(target, mcs._default_template(), "/vault", "/state")

    assert target.read_bytes() == original
    assert json.loads(target.read_bytes()) == {"env": {"BASE": "unchanged"}}
    assert not list(tmp_path.glob(".settings.json.llm-wiki-*.tmp"))


def test_apply_merge_aborts_if_noncooperating_editor_changes_target(tmp_path, monkeypatch):
    """The final optimistic check catches a write after the initial base check."""
    target = tmp_path / "settings.json"
    original = b'{"env":{"BASE":"merge-base"}}\n'
    edited = b'{"env":{"BASE":"external-editor"}}\n'
    target.write_bytes(original)
    original_create_backup = mcs._create_backup

    def edit_after_backup(*args, **kwargs):
        backup = original_create_backup(*args, **kwargs)
        target.write_bytes(edited)
        return backup

    monkeypatch.setattr(mcs, "_create_backup", edit_after_backup)
    with pytest.raises(ValueError, match="changed during merge"):
        mcs.apply_merge(target, mcs._default_template(), "/vault", "/state")

    assert target.read_bytes() == edited
    [backup] = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert backup.read_bytes() == original
    assert not list(tmp_path.glob(".settings.json.llm-wiki-*.tmp"))


def test_apply_merge_updates_symlink_referent_without_replacing_link(tmp_path):
    referent = tmp_path / "actual-settings.json"
    original = b'{"env":{"BASE":"through-link"}}\n'
    referent.write_bytes(original)
    referent.chmod(0o640)
    expected_mode = stat.S_IMODE(referent.stat().st_mode)
    target = tmp_path / "settings.json"
    try:
        target.symlink_to(referent.name)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    mcs.apply_merge(target, mcs._default_template(), "/vault", "/state")

    assert target.is_symlink()
    assert json.loads(referent.read_text(encoding="utf-8"))["env"]["LLM_WIKI_ROOT"] == "/vault"
    assert stat.S_IMODE(referent.stat().st_mode) == expected_mode
    [backup] = list(tmp_path.glob("settings.json.bak-llm-wiki-*"))
    assert backup.read_bytes() == original


def test_apply_merge_syncs_referent_parent_after_atomic_replace(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text("{}\n", encoding="utf-8")
    synced: list[Path] = []
    monkeypatch.setattr(mcs, "_sync_parent_directory", synced.append)

    mcs.apply_merge(target, mcs._default_template(), "/vault", "/state")

    assert target.parent.resolve() in synced


def test_concurrent_claude_mergers_serialize_entire_read_modify_publish(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text('{"hooks":{},"env":{"BASE":"keep"}}\n', encoding="utf-8")
    first_template = tmp_path / "first.json"
    second_template = tmp_path / "second.json"
    first_template.write_text(
        json.dumps(_minimal_claude_template("SessionStart")),
        encoding="utf-8",
    )
    second_template.write_text(
        json.dumps(_minimal_claude_template("SessionEnd")),
        encoding="utf-8",
    )
    context = multiprocessing.get_context("spawn")
    first_entered = context.Event()
    second_entered = context.Event()
    release = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_concurrent_claude_merge_worker,
        args=(
            target,
            first_template,
            "/vault",
            "/state",
            first_entered,
            release,
            True,
            results,
        ),
    )
    second = context.Process(
        target=_concurrent_claude_merge_worker,
        args=(
            target,
            second_template,
            "/vault",
            "/state",
            second_entered,
            release,
            False,
            results,
        ),
    )

    first.start()
    try:
        assert first_entered.wait(10)
        second.start()
        assert not second_entered.wait(2), "second merger entered before stable lock released"
    finally:
        release.set()
    first.join(15)
    second.join(15)
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert results.get(timeout=2) == ""
    assert results.get(timeout=2) == ""
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["hooks"]["SessionStart"]
    assert merged["hooks"]["SessionEnd"]
