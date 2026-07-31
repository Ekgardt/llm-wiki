"""Tests for safely installing native Codex lifecycle hooks."""
from __future__ import annotations

import json
import multiprocessing
import os
import re
import shutil
import stat
import subprocess
import traceback
from pathlib import Path

import merge_codex_hooks as mch
import pytest


def _concurrent_codex_merge_worker(
    target: Path,
    template: Path,
    vault_root: Path,
    entered,
    release,
    pause: bool,
    results,
) -> None:
    import merge_codex_hooks as worker_mch

    original_merge = worker_mch.merge_hooks

    def controlled_merge(*args, **kwargs):
        entered.set()
        if pause and not release.wait(15):
            raise TimeoutError("test did not release paused Codex merger")
        return original_merge(*args, **kwargs)

    worker_mch.merge_hooks = controlled_merge
    try:
        worker_mch.apply_merge(target, template, vault_root)
    except BaseException:
        results.put(traceback.format_exc())
    else:
        results.put("")


def _commands(data: dict, event: str) -> list[dict]:
    return [
        hook
        for block in data.get("hooks", {}).get(event, [])
        for hook in block.get("hooks", [])
    ]


def test_merge_preserves_user_hooks_and_replaces_only_ours(tmp_path):
    user = {
        "description": "keep me",
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "echo user"},
                        {"type": "command", "command": "uv run python scripts/session_start_context.py"},
                    ],
                }
            ],
            "PermissionRequest": [{"hooks": [{"type": "command", "command": "echo policy"}]}],
        },
    }
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))

    merged = mch.merge_hooks(user, template, tmp_path)

    assert merged["description"] == "keep me"
    assert _commands(merged, "PermissionRequest")[0]["command"] == "echo policy"
    start = _commands(merged, "SessionStart")
    assert any(h["command"] == "echo user" for h in start)
    assert not any(h["command"] == "uv run python scripts/session_start_context.py" for h in start)
    assert sum("session_start_context.py" in h["command"] for h in start) == 1
    assert sum("session_start_project_state.py" in h["command"] for h in start) == 1


def test_merge_uses_only_nonempty_command_fields_as_ownership_tokens(tmp_path):
    unrelated_command = {"type": "command", "command": "echo unrelated-posix"}
    unrelated_windows = {
        "type": "command",
        "commandWindows": "echo unrelated-windows",
    }
    owned_command = {"type": "command", "command": "echo managed-posix"}
    owned_windows = {
        "type": "command",
        "commandWindows": "echo managed-windows",
    }
    user = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        unrelated_command,
                        owned_command,
                        unrelated_windows,
                        owned_windows,
                    ],
                }
            ]
        }
    }
    template_block = {
        "matcher": "startup",
        "hooks": [owned_command, owned_windows],
    }
    template = {"hooks": {"SessionStart": [template_block]}}

    merged = mch.merge_hooks(user, template, tmp_path / "vault")

    assert merged["hooks"]["SessionStart"] == [
        {
            "matcher": "startup",
            "hooks": [unrelated_command, unrelated_windows],
        },
        template_block,
    ]


def test_materialized_commands_have_absolute_cross_platform_paths(tmp_path):
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))
    merged = mch.merge_hooks({}, template, tmp_path.resolve())

    for event in ("SessionStart", "PostToolUse", "PreCompact", "Stop"):
        for hook in _commands(merged, event):
            assert str(tmp_path.resolve()).replace("\\", "/") in hook["command"].replace("\\", "/")
            assert str(tmp_path.resolve()) in hook["commandWindows"]
            assert 1 <= hook["timeout"] <= 30

    context_hook = next(
        hook
        for hook in _commands(merged, "SessionStart")
        if "session_start_context.py" in hook["command"]
    )
    assert "--omit-project-state" in context_hook["command"]
    assert "--omit-project-state" in context_hook["commandWindows"]


def test_materialized_windows_command_preserves_literal_percent_path_through_cmd(tmp_path):
    cmd = shutil.which("cmd.exe")
    if os.name != "nt" or not cmd:
        pytest.skip("real cmd.exe is unavailable")
    vault = tmp_path / "vault %TEMP% with spaces"
    printer = tmp_path / "print_arg.py"
    printer.write_text("import sys\nprint(sys.argv[1])\n", encoding="ascii")
    template = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo managed",
                            "commandWindows": (
                                f'python "{printer}" '
                                "{{VAULT_ROOT_WINDOWS}}"
                            ),
                        }
                    ]
                }
            ]
        }
    }
    merged = mch.merge_hooks({}, template, vault)
    [handler] = _commands(merged, "SessionStart")
    env = os.environ.copy()
    env["TEMP"] = "EXPANDED_TEMP_SENTINEL"

    result = subprocess.run(
        f'"{cmd}" /d /s /c {handler["commandWindows"]}',
        env=env,
        check=True,
        capture_output=True,
        text=True,
        errors="replace",
    )

    assert result.stdout.strip() == str(vault.resolve())
    assert "EXPANDED_TEMP_SENTINEL" not in result.stdout


def test_default_codex_hooks_have_ownership_markers_and_fork_matcher():
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))

    assert all(
        hook.get("statusMessage", "").startswith("[LLM Wiki] ")
        for blocks in template["hooks"].values()
        for block in blocks
        for hook in block.get("hooks", [])
    )
    [session_start] = template["hooks"]["SessionStart"]
    assert "fork" in session_start["matcher"].split("|")


def test_marked_codex_hook_is_replaced_after_vault_move(tmp_path):
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))
    marked_old = {
        "type": "command",
        "command": "python D:/old-vault/scripts/renamed.py",
        "statusMessage": "[LLM Wiki] Previous generated hook",
    }
    unrelated = {
        "type": "command",
        "command": "echo keep",
        "statusMessage": "LLM Wiki user note",
    }
    user = {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [marked_old, unrelated]}
            ]
        }
    }

    merged = mch.merge_hooks(user, template, tmp_path / "new-vault")
    handlers = _commands(merged, "SessionStart")

    assert marked_old not in handlers
    assert unrelated in handlers


def test_unmarked_current_codex_hooks_remerge_by_known_owned_root(tmp_path):
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))
    vault_root = tmp_path / "owned vault"
    previous = mch.merge_hooks({}, template, vault_root)
    for event in previous["hooks"].values():
        for block in event:
            for hook in block.get("hooks", []):
                hook.pop("statusMessage", None)

    merged = mch.merge_hooks(previous, template, vault_root)
    handlers = [
        hook
        for event in merged["hooks"].values()
        for block in event
        for hook in block.get("hooks", [])
    ]

    assert len(handlers) == len(mch.OUR_SCRIPT_MARKERS)


def test_apply_merge_writes_timestamped_backup(tmp_path):
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    mch.apply_merge(target, mch.default_template(), tmp_path / "vault")

    assert json.loads(target.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert len(list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))) == 1


def test_apply_merge_refuses_malformed_existing_json(tmp_path):
    target = tmp_path / "hooks.json"
    target.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot safely read"):
        mch.apply_merge(target, mch.default_template(), tmp_path / "vault")
    assert target.read_text(encoding="utf-8") == "{broken"


def test_merge_preserves_similarly_named_hook_from_other_project(tmp_path):
    command = "uv run python /other/project/scripts/session_start_context.py"
    user = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))

    merged = mch.merge_hooks(user, template, tmp_path / "vault")

    assert any(h["command"] == command for h in _commands(merged, "SessionStart"))


def test_merge_preserves_exact_legacy_shape_rooted_at_other_project(tmp_path):
    command = (
        "uv run --directory /other/project python "
        "/other/project/scripts/session_start_context.py --omit-project-state"
    )
    hook = {"type": "command", "command": command}
    user = {"hooks": {"SessionStart": [{"hooks": [hook]}]}}
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))

    merged = mch.merge_hooks(user, template, tmp_path / "owned-vault")

    assert hook in _commands(merged, "SessionStart")


def test_merge_replaces_prior_materialized_context_command_without_omit_flag(tmp_path):
    template = json.loads(mch.default_template().read_text(encoding="utf-8"))
    previous_template = json.loads(json.dumps(template))
    for hook in _commands(previous_template, "SessionStart"):
        hook["command"] = hook["command"].replace(" --omit-project-state", "")
        hook["commandWindows"] = hook["commandWindows"].replace(
            " --omit-project-state", ""
        )
    previous = mch.merge_hooks({}, previous_template, tmp_path)

    merged = mch.merge_hooks(previous, template, tmp_path)
    context_hooks = [
        hook
        for hook in _commands(merged, "SessionStart")
        if "session_start_context.py" in hook["command"]
    ]

    assert len(context_hooks) == 1
    assert "--omit-project-state" in context_hooks[0]["command"]


def _minimal_codex_template(event: str = "SessionStart") -> dict:
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


def _codex_hook_document(handler: dict, **block_metadata) -> dict:
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
        ({"hooks": []}, _minimal_codex_template(), "user hooks"),
        ({}, {"hooks": []}, "template hooks"),
        (
            {"hooks": {"SessionStart": {}}},
            _minimal_codex_template(),
            "user hooks.SessionStart",
        ),
        ({}, {"hooks": {"SessionStart": {}}}, "template hooks.SessionStart"),
        (
            {"hooks": {"SessionStart": ["not-an-object"]}},
            _minimal_codex_template(),
            "user hooks.SessionStart block",
        ),
        (
            {},
            {"hooks": {"SessionStart": ["not-an-object"]}},
            "template hooks.SessionStart block",
        ),
        (
            {"hooks": {"SessionStart": [{"hooks": {}}]}},
            _minimal_codex_template(),
            "user hooks.SessionStart block hooks",
        ),
        (
            {},
            {"hooks": {"SessionStart": [{"hooks": {}}]}},
            "template hooks.SessionStart block hooks",
        ),
        (
            {"hooks": {"SessionStart": [{"hooks": ["not-an-object"]}]}},
            _minimal_codex_template(),
            "user hooks.SessionStart handler",
        ),
        (
            {},
            {"hooks": {"SessionStart": [{"hooks": ["not-an-object"]}]}},
            "template hooks.SessionStart handler",
        ),
        (
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"command": {"unexpected": True}}]}
                    ]
                }
            },
            _minimal_codex_template(),
            "user hooks.SessionStart handler command",
        ),
    ),
)
def test_apply_merge_rejects_invalid_managed_structure_without_backup_or_write(
    tmp_path,
    user,
    template,
    message,
):
    target = tmp_path / "hooks.json"
    original = (json.dumps(user, separators=(",", ":")) + "\r\n").encode()
    target.write_bytes(original)
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(ValueError, match=".*".join(map(re.escape, message.split()))):
        mch.apply_merge(target, template_path, tmp_path / "vault")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))


@pytest.mark.parametrize("side", ("user", "template"))
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        pytest.param("command", 7, id="command"),
        pytest.param("commandWindows", {}, id="command-windows"),
        pytest.param("statusMessage", [], id="status-message"),
        pytest.param("args", ["valid", 7], id="args"),
        pytest.param("timeout", True, id="timeout-bool"),
        pytest.param("timeout", -1, id="timeout-negative"),
        pytest.param("timeout", 1.5, id="timeout-fraction"),
        pytest.param("timeout", "5", id="timeout-string"),
        pytest.param("async", "yes", id="async"),
    ),
)
def test_apply_merge_rejects_malformed_known_codex_handler_fields_before_write(
    tmp_path,
    side,
    field,
    invalid_value,
):
    handler = {"type": "command", "command": "echo user", field: invalid_value}
    invalid_document = _codex_hook_document(handler)
    user = invalid_document if side == "user" else {}
    template_data = invalid_document if side == "template" else _minimal_codex_template()
    target = tmp_path / "hooks.json"
    original = (json.dumps(user, separators=(",", ":")) + "\r\n").encode()
    target.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(template_data), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        mch.apply_merge(target, template, tmp_path / "vault")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))


def test_merge_accepts_valid_known_codex_handler_fields_and_unknown_metadata(tmp_path):
    handler = {
        "type": "command",
        "command": "echo user",
        "commandWindows": "echo user on windows",
        "statusMessage": "User hook",
        "args": ["one", "two"],
        "timeout": 0,
        "async": False,
        "futureField": {"nested": True},
    }

    merged = mch.merge_hooks(
        _codex_hook_document(handler),
        _minimal_codex_template(),
        tmp_path / "vault",
    )

    assert merged["hooks"]["SessionStart"][0]["hooks"] == [handler]


def test_apply_merge_rejects_insertable_non_string_codex_description(tmp_path):
    target = tmp_path / "hooks.json"
    original = b'{"hooks":{"FutureEvent":[]}}\r\n'
    target.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({"description": {"future": True}, **_minimal_codex_template()}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="description"):
        mch.apply_merge(target, template, tmp_path / "vault")

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))


def test_merge_leaves_existing_codex_description_untouched_when_template_is_malformed(
    tmp_path,
):
    existing = {"future": [1, 2, 3]}
    user = {"description": existing}
    template = {"description": {"malformed": True}, **_minimal_codex_template()}

    merged = mch.merge_hooks(user, template, tmp_path / "vault")

    assert merged["description"] == existing


def test_merge_preserves_empty_managed_block_with_future_metadata(tmp_path):
    block = {
        "matcher": "startup",
        "futureBlockMetadata": {"mode": "keep"},
        "hooks": [],
    }
    user = {"hooks": {"SessionStart": [block]}}

    merged = mch.merge_hooks(user, _minimal_codex_template(), tmp_path / "vault")

    assert block in merged["hooks"]["SessionStart"]


def test_merge_preserves_future_block_metadata_after_removing_owned_handler(tmp_path):
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

    merged = mch.merge_hooks(user, _minimal_codex_template(), tmp_path / "vault")

    assert {
        "matcher": "startup",
        "futureBlockMetadata": {"mode": "keep"},
        "hooks": [],
    } in merged["hooks"]["SessionStart"]
    assert {"matcher": "resume", "hooks": []} not in merged["hooks"]["SessionStart"]


def test_merge_preserves_unknown_unmanaged_structure_exactly(tmp_path):
    opaque_event = [
        "non-object block",
        {"matcher": {"future": True}, "hooks": "future-schema"},
        {"nested": [1, {"x": None}]},
    ]
    opaque_top = {"list": [1, False, {"future": "value"}]}
    user = {
        "futureTop": opaque_top,
        "hooks": {
            "FutureEvent": opaque_event,
            "SessionStart": [
                {"matcher": "", "hooks": [{"command": "echo user"}]}
            ],
        },
    }

    merged = mch.merge_hooks(user, _minimal_codex_template(), tmp_path / "vault")

    assert merged["futureTop"] == opaque_top
    assert merged["hooks"]["FutureEvent"] == opaque_event


def test_load_json_uses_named_positive_four_mib_plus_one_bound(monkeypatch, tmp_path):
    hooks = tmp_path / "hooks.json"
    hooks.write_text('{"hooks":{}}\n', encoding="utf-8")
    real_open = Path.open
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

    def tracking_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path == hooks:
            return TrackingFile(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    assert mch._load_json(hooks) == {"hooks": {}}
    assert read_sizes == [mch.MAX_HOOKS_JSON_BYTES + 1]


@pytest.mark.parametrize("invalid_kind", ("oversize", "invalid-utf8"))
@pytest.mark.parametrize("invalid_side", ("target", "template"))
def test_apply_merge_rejects_bounded_or_non_utf8_input_before_backup_or_write(
    tmp_path,
    invalid_side,
    invalid_kind,
):
    target = tmp_path / "hooks.json"
    original = b'{"hooks":{"FutureEvent":[]}}\n'
    target.write_bytes(original)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_minimal_codex_template()), encoding="utf-8")
    invalid = (
        b'{"padding":"' + b"x" * (mch.MAX_HOOKS_JSON_BYTES + 1) + b'"}\n'
        if invalid_kind == "oversize"
        else b'{"hooks":{}}\xff\n'
    )
    (target if invalid_side == "target" else template).write_bytes(invalid)
    expected = invalid if invalid_side == "target" else original

    with pytest.raises(ValueError):
        mch.apply_merge(target, template, tmp_path / "vault")

    assert target.read_bytes() == expected
    assert not list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))


def test_apply_merge_dry_run_creates_no_parent_lock_backup_or_target(tmp_path):
    target = tmp_path / "missing" / "hooks.json"

    mch.apply_merge(target, mch.default_template(), tmp_path / "vault", dry_run=True)

    assert not target.parent.exists()


def test_apply_merge_creates_unique_exact_backups_and_preserves_mode(tmp_path):
    target = tmp_path / "hooks.json"
    original = b'{\r\n  "hooks": {"FutureEvent": []}\r\n}\r\n'
    target.write_bytes(original)
    target.chmod(0o640)
    expected_mode = stat.S_IMODE(target.stat().st_mode)

    mch.apply_merge(target, mch.default_template(), tmp_path / "vault")
    second_base = target.read_bytes()
    mch.apply_merge(target, mch.default_template(), tmp_path / "vault")

    backups = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert len(backups) == 2
    assert {backup.read_bytes() for backup in backups} == {original, second_base}
    assert stat.S_IMODE(target.stat().st_mode) == expected_mode


def test_apply_merge_failure_before_replace_keeps_parseable_target_and_cleans_temp(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "hooks.json"
    original = b'{"hooks":{"FutureEvent":[]}}\n'
    target.write_bytes(original)

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(mch.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        mch.apply_merge(target, mch.default_template(), tmp_path / "vault")

    assert target.read_bytes() == original
    assert json.loads(target.read_bytes()) == {"hooks": {"FutureEvent": []}}
    assert not list(tmp_path.glob(".hooks.json.llm-wiki-*.tmp"))


def test_apply_merge_aborts_if_noncooperating_editor_changes_target(tmp_path, monkeypatch):
    """The final optimistic check catches a write after the initial base check."""
    target = tmp_path / "hooks.json"
    original = b'{"hooks":{"MergeBase":[]}}\n'
    edited = b'{"hooks":{"ExternalEditor":[]}}\n'
    target.write_bytes(original)
    original_create_backup = mch._create_backup

    def edit_after_backup(*args, **kwargs):
        backup = original_create_backup(*args, **kwargs)
        target.write_bytes(edited)
        return backup

    monkeypatch.setattr(mch, "_create_backup", edit_after_backup)
    with pytest.raises(ValueError, match="changed during merge"):
        mch.apply_merge(target, mch.default_template(), tmp_path / "vault")

    assert target.read_bytes() == edited
    [backup] = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert backup.read_bytes() == original
    assert not list(tmp_path.glob(".hooks.json.llm-wiki-*.tmp"))


def test_apply_merge_updates_symlink_referent_without_replacing_link(tmp_path):
    referent = tmp_path / "actual-hooks.json"
    original = b'{"hooks":{"FutureEvent":[]}}\n'
    referent.write_bytes(original)
    referent.chmod(0o640)
    expected_mode = stat.S_IMODE(referent.stat().st_mode)
    target = tmp_path / "hooks.json"
    try:
        target.symlink_to(referent.name)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    mch.apply_merge(target, mch.default_template(), tmp_path / "vault")

    assert target.is_symlink()
    assert json.loads(referent.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert stat.S_IMODE(referent.stat().st_mode) == expected_mode
    [backup] = list(tmp_path.glob("hooks.json.bak-llm-wiki-*"))
    assert backup.read_bytes() == original


def test_apply_merge_syncs_referent_parent_after_atomic_replace(tmp_path, monkeypatch):
    target = tmp_path / "hooks.json"
    target.write_text("{}\n", encoding="utf-8")
    synced: list[Path] = []
    monkeypatch.setattr(mch, "_sync_parent_directory", synced.append)

    mch.apply_merge(target, mch.default_template(), tmp_path / "vault")

    assert target.parent.resolve() in synced


def test_concurrent_codex_mergers_serialize_entire_read_modify_publish(tmp_path):
    target = tmp_path / "hooks.json"
    target.write_text('{"hooks":{},"futureTop":{"keep":true}}\n', encoding="utf-8")
    first_template = tmp_path / "first.json"
    second_template = tmp_path / "second.json"
    first_template.write_text(
        json.dumps(_minimal_codex_template("SessionStart")),
        encoding="utf-8",
    )
    second_template.write_text(
        json.dumps(_minimal_codex_template("Stop")),
        encoding="utf-8",
    )
    context = multiprocessing.get_context("spawn")
    first_entered = context.Event()
    second_entered = context.Event()
    release = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_concurrent_codex_merge_worker,
        args=(
            target,
            first_template,
            tmp_path / "vault",
            first_entered,
            release,
            True,
            results,
        ),
    )
    second = context.Process(
        target=_concurrent_codex_merge_worker,
        args=(
            target,
            second_template,
            tmp_path / "vault",
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
    assert merged["hooks"]["Stop"]
