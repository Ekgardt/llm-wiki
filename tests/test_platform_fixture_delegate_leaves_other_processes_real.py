"""The delegate fixture replaces only the delegated call: another subprocess started meanwhile stays real."""
import sys

import integration_adapter
import markdown_transaction
from llm_wiki import integration_lifecycle

from tests import test_lifecycle_delegate_receives_redacted_payload as delegate_cases


def test_delegate_fixture_leaves_other_subprocesses_real(tmp_path, monkeypatch, capsys):
    original = integration_adapter.ingest_session_end

    def observe_other_process(*args, **kwargs):
        result = markdown_transaction.run_acl_command(
            [sys.executable, "-c", "print('neutral child')"])
        assert result.stdout == b"neutral child\n" or result.stdout == b"neutral child\r\n"
        return original(*args, **kwargs)

    monkeypatch.setattr(integration_lifecycle, "ingest_session_end", observe_other_process)
    delegate_cases.test_delegate_receives_only_normalized_redacted_payload(
        monkeypatch, capsys, tmp_path)
