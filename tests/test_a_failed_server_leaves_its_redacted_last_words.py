"""What a language server printed before it failed reaches its failure evidence.

Until now the server's stderr was drained into a 4 MiB ring that nothing read,
and `lsp_security.redact_lsp_text` guarded nothing, while `CLAUDE.md` listed
"safe log redaction" as implemented. A failed start left a code and no reason.
See `docs/research/2026-09-17-lsp-a-server-that-dies-leaves-its-last-words.md`.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from lsp_process import LspProcess
from lsp_security import redact_lsp_text

from tests.slow_machine import SHORT_TIMEOUT

OWNER_NONCE = "d" * 32
BEARER = "abcdefghijklmnopqrst"
API_KEY = "0123456789abcdef0123"

# A server that says why it is unhappy, in the way servers do, and then dies.
# It lingers first, as `fake_lsp_server` does, so the manager finishes starting
# it and the exit is an unexpected one rather than a startup failure. That
# pause is a fixture process's own, not a wait this test makes.
_SERVER = f"""
import sys, time
sys.stderr.write("authorization: Bearer {BEARER}\\n")
sys.stderr.write("X-Api-Key: {API_KEY}\\n")
sys.stderr.write("cannot open workspace\\n")
sys.stderr.flush()
time.sleep(1.0)
"""


def _failure_record(owner_root: Path) -> dict:
    """The record, once it is named and no longer held open by its writer.

    Waiting for the name alone is not enough on Windows: a record is published
    by renaming a handle that shares nothing, so between the rename and the
    close the file stands under its contract name and no reader can open it for
    data. `is_file()` passes there — an attribute-only open skips the sharing
    check — and `read_bytes()` gets ERROR_SHARING_VIOLATION as `[Errno 13]`. The
    product tolerates the same window for the lease. A denial that outlasts the
    deadline still fails, and names itself. Research:
    `docs/research/2026-09-18-a-published-record-is-let-go-before-it-is-flushed.md`.
    """
    evidence = owner_root / "failure.json"
    deadline = time.monotonic() + SHORT_TIMEOUT
    while True:
        try:
            return json.loads(evidence.read_bytes())
        except (FileNotFoundError, PermissionError) as error:
            if time.monotonic() > deadline:
                raise AssertionError("no readable failure evidence in time") from error
        time.sleep(0.01)


def test_the_failed_server_reason_is_kept_with_its_credentials_removed(
    tmp_path: Path,
) -> None:
    process = LspProcess.start(
        [sys.executable, "-c", _SERVER], cwd=tmp_path, owner_root=tmp_path / OWNER_NONCE
    )
    try:
        record = _failure_record(process.owner_root)
    finally:
        process.close(time.monotonic() + SHORT_TIMEOUT)
    tail = record.get("stderr_tail", "")
    found = ("cannot open workspace" in tail, BEARER in tail, API_KEY in tail)
    assert (record["code"], found) == ("process_exited", (True, False, False))


@pytest.mark.parametrize(
    "line, secret",
    [
        ("api-key: sekrit-one", "sekrit-one"),
        ("apikey=sekrit-two", "sekrit-two"),
        ("X-Api-Key: sekrit-three", "sekrit-three"),
        ("passwd=sekrit-four", "sekrit-four"),
        ("private_key=sekrit-five", "sekrit-five"),
        ("cookie: sekrit-six", "sekrit-six"),
        ("sent Bearer sekrit-seven-is-long", "sekrit-seven-is-long"),
        ("key sk-ant-api03-sekritsekritsekrit", "sk-ant-api03-sekritsekritsekrit"),
    ],
)
def test_the_redactor_removes_every_credential_spelling_a_server_prints(
    line: str, secret: str
) -> None:
    assert secret not in redact_lsp_text(line)
