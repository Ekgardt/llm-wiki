"""A lifecycle delegate receives only the normalized, redacted payload, and the host sees nothing on stdout."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _assert_delegate_saw_only_redacted(observed, secret):
    delegated = json.loads(observed["input"])
    assert "host_only" not in delegated
    assert secret not in observed["input"]
    assert delegated["session_id"] == "[REDACTED_API_KEY]"
    assert delegated["reason"] == "token=[REDACTED]"


def test_delegate_receives_only_normalized_redacted_payload(monkeypatch, capsys, tmp_path):
    import integration_adapter

    from tests.adopted_vault import adopt

    # This one runs the adapter for real, so it writes a project journal. Without
    # its own root that lands in the live vault under the redacted slug.
    vault, state = adopt(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    observed = {}

    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:2] != [sys.executable, str(SCRIPTS_DIR / "session_end_project_tag.py")]:
            return real_run(command, *args, **kwargs)
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="context", stderr=secret)

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = {
        "session_id": secret,
        "cwd": f"C:/work/{secret}",
        "reason": f"token={secret}",
        "transcript_path": f"C:/tmp/{secret}.jsonl",
        "host_only": secret,
    }
    with patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
        rc = integration_adapter.main(
            [
                "--source",
                "claude",
                "--event",
                "session_end",
                "--delegate",
                "session_end_project_tag.py",
            ]
        )

    assert rc == 0
    _assert_delegate_saw_only_redacted(observed, secret)
    assert capsys.readouterr().out == ""
