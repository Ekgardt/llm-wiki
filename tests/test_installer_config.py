from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from installer_config import (
    PROFILE_END,
    PROFILE_START,
    build_cron_command,
    configure_opencode,
    expected_opencode_entry,
    merge_opencode_user_config,
    opencode_global_dir,
    parse_jsonc,
    probe_effective_entry,
    replace_profile_block,
    selected_global_file,
    verify_effective_entry,
)


@pytest.mark.parametrize("xdg", [None, "", "relative/config"])
def test_posix_xdg_unset_empty_or_relative_falls_back(
    tmp_path: Path, xdg: str | None
) -> None:
    assert opencode_global_dir(tmp_path, xdg, platform="posix") == (
        tmp_path / ".config" / "opencode"
    )


def test_posix_absolute_xdg_is_used(tmp_path: Path) -> None:
    target = tmp_path / "xdg"
    assert opencode_global_dir(tmp_path, str(target), platform="posix") == (
        target / "opencode"
    )


def test_windows_ignores_xdg_and_uses_home(tmp_path: Path) -> None:
    assert opencode_global_dir(tmp_path, "C:/other", platform="windows") == (
        tmp_path / ".config" / "opencode"
    )


def test_jsonc_parser_preserves_comment_markers_inside_strings() -> None:
    value = parse_jsonc(
        r'''
        {
          // line comment
          "url": "https://example.test/a//b",
          "marker": "/* retained */",
          "items": [1, 2,],
          /* block comment */
        }
        '''
    )

    assert value == {
        "url": "https://example.test/a//b",
        "marker": "/* retained */",
        "items": [1, 2],
    }


@pytest.mark.parametrize(
    "source",
    [
        "[]",
        '{"value": 1} trailing',
        '{"value": "unterminated}',
        '{/* unterminated}',
    ],
)
def test_jsonc_parser_rejects_non_object_and_malformed_input(source: str) -> None:
    with pytest.raises(ValueError):
        parse_jsonc(source)


def test_jsonc_merge_preserves_unrelated_values_and_is_idempotent(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    config = config_dir / "opencode.jsonc"
    original = (
        b'{\n  // retained by the backup\n  "model": "local/model",\n'
        b'  "mcp": {"other": {"enabled": true,},},\n}\n'
    )
    config.write_bytes(original)
    expected = expected_opencode_entry(tmp_path / "vault")

    first = merge_opencode_user_config(config_dir, expected)
    first_bytes = config.read_bytes()
    second = merge_opencode_user_config(config_dir, expected)

    assert first.changed is True
    assert first.config_file == config
    assert second.changed is False
    assert config.read_bytes() == first_bytes
    parsed = parse_jsonc(config.read_text(encoding="utf-8"))
    assert parsed["model"] == "local/model"
    assert parsed["mcp"]["other"]["enabled"] is True
    assert parsed["mcp"]["llm-wiki"] == expected
    assert first.backup is not None and first.backup.read_bytes() == original
    assert second.backup is None


def test_malformed_jsonc_causes_no_write_or_backup(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "opencode.jsonc"
    original = b'{"mcp": /* incomplete'
    config.write_bytes(original)

    with pytest.raises(ValueError):
        merge_opencode_user_config(config_dir, expected_opencode_entry(tmp_path))

    assert config.read_bytes() == original
    assert list(config_dir.glob("*.bak")) == []


def test_symlinked_selected_config_is_rejected_without_target_write(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = tmp_path / "target.json"
    target.write_text('{"model":"unchanged"}\n', encoding="utf-8")
    selected = config_dir / "opencode.jsonc"
    try:
        selected.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="symlink"):
        merge_opencode_user_config(config_dir, expected_opencode_entry(tmp_path))

    assert target.read_text(encoding="utf-8") == '{"model":"unchanged"}\n'


@pytest.mark.parametrize(
    ("present", "expected_name"),
    [
        (("opencode.jsonc", "opencode.json", "config.json"), "opencode.jsonc"),
        (("opencode.json", "config.json"), "opencode.json"),
        (("config.json",), "config.json"),
        ((), "opencode.jsonc"),
    ],
)
def test_global_config_selection_order(
    tmp_path: Path, present: tuple[str, ...], expected_name: str
) -> None:
    for name in present:
        (tmp_path / name).write_text("{}\n", encoding="utf-8")

    assert selected_global_file(tmp_path).name == expected_name


def test_hash_named_backup_is_create_only_and_must_match(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "opencode.jsonc"
    original = b'{"model":"private/value"}\n'
    config.write_bytes(original)
    expected = expected_opencode_entry(tmp_path / "vault")
    first = merge_opencode_user_config(config_dir, expected)
    assert first.backup is not None

    config.write_bytes(original)
    first.backup.write_bytes(b"different")
    with pytest.raises(ValueError, match="backup"):
        merge_opencode_user_config(config_dir, expected)

    assert config.read_bytes() == original


def test_effective_entry_distinguishes_active_and_conflict(tmp_path: Path) -> None:
    expected = expected_opencode_entry(tmp_path / "vault")
    assert verify_effective_entry({"mcp": {"llm-wiki": expected}}, expected) == "active"
    assert (
        verify_effective_entry(
            {"mcp": {"llm-wiki": {**expected, "enabled": False}}}, expected
        )
        == "conflict"
    )
    assert verify_effective_entry({}, expected) == "conflict"


def test_unrelated_higher_precedence_values_leave_entry_active(tmp_path: Path) -> None:
    expected = expected_opencode_entry(tmp_path / "vault")
    effective = {"model": "project/model", "mcp": {"llm-wiki": expected}}
    assert verify_effective_entry(effective, expected) == "active"


def test_profile_rewrites_only_owned_block_and_keeps_custom_state(
    tmp_path: Path,
) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_text("export USER_SETTING=yes\n", encoding="utf-8")
    replace_profile_block(profile, Path("/vault one"), Path("/state two"))
    first = profile.read_text(encoding="utf-8")
    replace_profile_block(profile, Path("/vault one"), Path("/state two"))

    assert profile.read_text(encoding="utf-8") == first
    assert first.count(PROFILE_START) == first.count(PROFILE_END) == 1
    assert "export USER_SETTING=yes" in first
    assert "LLM_WIKI_ROOT='/vault one'" in first
    assert "LLM_WIKI_STATE_ROOT='/state two'" in first


@pytest.mark.parametrize("kind", ["nightly", "weekly"])
def test_cron_command_round_trips_exact_paths_without_cwd_dependency(
    tmp_path: Path, kind: str
) -> None:
    root = tmp_path / "vault's source"
    state = tmp_path / "state's data"
    uv = tmp_path / "bin's tools" / "uv"
    log = state / "logs" / f"cron-{kind}.log"
    root.mkdir(parents=True)
    state.mkdir(parents=True)
    uv.parent.mkdir(parents=True)
    command = build_cron_command(
        root=root,
        state_root=state,
        uv_path=uv,
        kind=kind,
        log_path=log,
    )

    assert " cd " not in f" {command} "
    assert "run --locked --no-sync --directory" in command
    if shutil.which("sh") is None:
        pytest.skip("POSIX sh unavailable")
    environment = os.environ.copy()
    environment["PATH"] = str(uv.parent) + os.pathsep + environment.get("PATH", "")
    probe = subprocess.run(
        ["sh", "-n", "-c", command],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert f"LLM_WIKI_ROOT={shlex.quote(str(root))}" in command
    assert f"LLM_WIKI_STATE_ROOT={shlex.quote(str(state))}" in command
    assert shlex.quote(str(uv)) in command
    assert shlex.quote(str(log)) in command


@pytest.mark.parametrize(
    "existing",
    [
        f"{PROFILE_START}\n",
        f"{PROFILE_END}\n",
        f"{PROFILE_START}\na\n{PROFILE_END}\n{PROFILE_START}\nb\n{PROFILE_END}\n",
    ],
)
def test_invalid_profile_markers_fail_without_a_write(
    tmp_path: Path, existing: str
) -> None:
    profile = tmp_path / ".profile"
    profile.write_text(existing, encoding="utf-8")
    before = profile.read_bytes()

    with pytest.raises(ValueError, match="ownership"):
        replace_profile_block(profile, Path("/root"), Path("/state"))

    assert profile.read_bytes() == before


_DEBUG_SCRIPT = r'''
import json
import os
from pathlib import Path

root = os.environ["TEST_VAULT"]
entry = {
    "type": "local",
    "command": [
        "uv", "run", "--locked", "--no-sync", "--directory", root,
        "python", "scripts/mcp_server.py",
    ],
    "enabled": True,
}
conflict = any(os.environ.get(name) for name in (
    "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT",
    "TEST_MANAGED_CONFLICT",
)) or (Path.cwd() / "opencode.json").exists()
if conflict:
    entry = {**entry, "enabled": False}
print(json.dumps({"model": "unrelated/value", "mcp": {"llm-wiki": entry}}))
'''


@pytest.mark.parametrize(
    "source",
    [
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
        "TEST_MANAGED_CONFLICT",
        "project",
    ],
)
def test_effective_probe_detects_each_higher_precedence_conflict(
    tmp_path: Path, source: str
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    environment = {"TEST_VAULT": str(vault)}
    if source == "project":
        (cwd / "opencode.json").write_text("{}\n", encoding="utf-8")
    else:
        environment[source] = "configured"

    status = probe_effective_entry(
        [sys.executable, "-c", _DEBUG_SCRIPT],
        cwd=cwd,
        environment=environment,
        expected=expected_opencode_entry(vault),
        timeout_seconds=5,
        max_bytes=1024 * 1024,
    )

    assert status == "conflict"


def test_effective_probe_accepts_unrelated_higher_precedence_values(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    status = probe_effective_entry(
        [sys.executable, "-c", _DEBUG_SCRIPT],
        cwd=tmp_path,
        environment={"TEST_VAULT": str(vault)},
        expected=expected_opencode_entry(vault),
        timeout_seconds=5,
        max_bytes=1024 * 1024,
    )
    assert status == "active"


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_text_quietly(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _await_probe_pid(pid_file: Path, timeout: float = 2.0) -> int:
    """The child records its PID before sleeping; the write can lag the kill."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _read_text_quietly(pid_file)
        if text:
            return int(text)
        time.sleep(0.01)
    pytest.fail(f"probe child never recorded its PID in {pid_file}")


@pytest.mark.parametrize("mode", ["hung", "oversized"])
def test_hung_or_oversized_debug_probe_is_bounded_and_cleans_child(
    tmp_path: Path, mode: str
) -> None:
    pid_file = tmp_path / f"{mode}.pid"
    prefix = (
        "import os, pathlib, sys, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
    )
    if mode == "hung":
        code = prefix + "time.sleep(30)"
    else:
        code = prefix + "sys.stdout.write('x' * 1000000); sys.stdout.flush(); time.sleep(30)"
    started = time.monotonic()

    status = probe_effective_entry(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        environment={},
        expected=expected_opencode_entry(tmp_path),
        timeout_seconds=0.2 if mode == "hung" else 5,
        max_bytes=1024,
    )

    assert status == "configured_unverified"
    assert time.monotonic() - started < 3
    pid = _await_probe_pid(pid_file)
    for _ in range(100):
        if not _process_exists(pid):
            break
        time.sleep(0.01)
    assert not _process_exists(pid)


def test_configure_opencode_absent_does_not_create_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import installer_config

    monkeypatch.setattr(installer_config.shutil, "which", lambda _name: None)
    home = tmp_path / "home"
    result = configure_opencode(
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        cwd=tmp_path,
        home=home,
        xdg_config_home=None,
        platform="posix",
    )

    assert result["status"] == "not_detected"
    assert not (home / ".config" / "opencode").exists()


def test_configure_opencode_merges_global_source_and_copies_plugin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    plugin_source = scripts / "llm-wiki-memory-opencode.js"
    plugin_source.write_text(
        "const _EMBEDDED_ROOT = null; // llm-wiki:embedded-root\n"
        "export const plugin = true;\n",
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    state_root.mkdir()
    caller = tmp_path / "caller"
    caller.mkdir()
    xdg = tmp_path / "xdg"
    debug_script = _DEBUG_SCRIPT + "\nPath('debug.cwd').write_text(str(Path.cwd()))\n"

    result = configure_opencode(
        root=root,
        state_root=state_root,
        cwd=caller,
        home=tmp_path / "home",
        xdg_config_home=str(xdg),
        platform="posix",
        debug_command=[sys.executable, "-c", debug_script],
        environment={"TEST_VAULT": str(root)},
    )

    config = xdg / "opencode" / "opencode.jsonc"
    plugin = xdg / "opencode" / "plugins" / "llm-wiki-memory.js"
    assert result == {
        "config_file": str(config),
        "plugin_file": str(plugin),
        "status": "active",
    }
    assert parse_jsonc(config.read_text(encoding="utf-8"))["mcp"]["llm-wiki"] == (
        expected_opencode_entry(root)
    )
    published = plugin.read_text(encoding="utf-8")
    encoded_root = json.dumps(str(root))
    assert f"const _EMBEDDED_ROOT = {encoded_root};" in published
    assert published.replace(encoded_root, "null") == plugin_source.read_text(encoding="utf-8")
    assert (caller / "debug.cwd").read_text(encoding="utf-8") == str(caller)
