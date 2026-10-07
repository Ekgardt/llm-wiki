"""Platform fixture behavior must reach its intended unchanged production guard."""
import sys
from pathlib import Path
from types import SimpleNamespace

import integration_adapter
import markdown_transaction
import reliable_memory

from tests import test_native_user_frames_keep_their_physical_evidence as native_cases
from tests import test_plugin_helpers as plugin_cases
from tests import test_reliable_memory as runtime_cases


def _windows_text_read(path, encoding=None, errors=None):
    with path.open("r", encoding=encoding or "cp1252", errors=errors) as source:
        return source.read()


def test_forged_frame_control_reaches_authority_guard_with_windows_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "read_text", _windows_text_read)
    native_cases.test_forged_journal_framing_grants_no_daily_fact(
        tmp_path, "## [01:00:00] Captured event")


def test_delegate_fixture_leaves_other_subprocesses_real(tmp_path, monkeypatch, capsys):
    original = integration_adapter._ingest_session_end

    def observe_other_process(*args, **kwargs):
        result = markdown_transaction._run_acl_command(
            [sys.executable, "-c", "print('neutral child')"])
        assert result.stdout == b"neutral child\n" or result.stdout == b"neutral child\r\n"
        return original(*args, **kwargs)

    monkeypatch.setattr(integration_adapter, "_ingest_session_end", observe_other_process)
    plugin_cases.test_delegate_receives_only_normalized_redacted_payload(
        monkeypatch, capsys, tmp_path)


def _contains_real_directory_link(path):
    return any(parent.is_symlink() for parent in Path(path).parents)


def test_inside_reparse_control_preserves_the_windows_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_cases, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(reliable_memory, "_windows_reparse_point", _contains_real_directory_link)
    runtime_cases.test_runtime_parent_authority_is_fresh_after_symlink_retarget(tmp_path)
