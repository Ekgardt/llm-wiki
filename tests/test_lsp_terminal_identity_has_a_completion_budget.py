"""Identity preservation remains testable after slower real evidence publication."""
import time

from llm_wiki import lsp_process_cleanup, lsp_process_limits

import tests.test_lsp_process_windows_child_and_identity as scenario
from tests.slow_machine import LONG_TIMEOUT


def test_first_identity_survives_evidence_outlasting_the_normal_grace(tmp_path, monkeypatch):
    original_start = scenario._start
    original_evidence = lsp_process_cleanup.record_initial_evidence
    elapsed_evidence = 2 * lsp_process_limits.GRACEFUL_CLEANUP_SECONDS
    processes = []

    def capture_start(*args, **kwargs):
        process = original_start(*args, **kwargs)
        processes.append(process)
        return process

    def slower_evidence(run, start):
        original_evidence(run, start)
        run.deadline -= elapsed_evidence

    monkeypatch.setattr(scenario, "_start", capture_start)
    monkeypatch.setattr(lsp_process_cleanup, "record_initial_evidence", slower_evidence)
    try:
        scenario.test_first_terminal_identity_survives_later_heartbeat_failure(monkeypatch, tmp_path)
    finally:
        monkeypatch.setattr(lsp_process_cleanup, "record_initial_evidence", original_evidence)
        for process in processes:
            process.close(time.monotonic() + LONG_TIMEOUT)
