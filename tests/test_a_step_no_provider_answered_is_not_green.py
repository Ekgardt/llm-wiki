"""A nightly step that did none of its waiting work exits non-zero, and its budget fits its step.

Research: `docs/research/2026-09-17-a-step-no-provider-answered-is-not-green.md`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

DAILY = (
    "# 2023-05-22\n\n## [10:00:00] session_end | s\n\n_captured: 2023-05-22 10:00:00_\n\n"
    "**user:** I'm thinking of selling my old drum set, a 5-piece Pearl Export.\n\n"
    "**assistant:** You could list it on a marketplace.\n"
)
RECORD = "---\ntype: raw-source\nsession: abc123\n---\n\n# Session abc123\n\n**user:** why systemd?\n\n**assistant:** timers survive a reboot\n"

# A provider that does not answer: a local Ollama on a closed loopback port,
# refused at once; nothing leaves the host.
SILENT = {"MEMORY_LLM_PROVIDER": "ollama", "MEMORY_LLM_BASE_URL": "http://127.0.0.1:9/v1"}


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge" / "daily").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "raw" / "sessions" / "2026-08-23").mkdir(parents=True)
    (tmp_path / "state" / "run").mkdir(parents=True)
    return root


def _exit_code(tmp_path: Path, script: str, arguments: list[str], provider: dict[str, str]) -> int:
    environment = {
        **os.environ,
        "LLM_WIKI_ROOT": str(tmp_path / "vault"),
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"),
        **provider,
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    return done.returncode


def test_fact_keys_with_turns_waiting_and_a_silent_provider_is_a_failed_step(tmp_path):
    (_vault(tmp_path) / "knowledge" / "daily" / "2023-05-22.md").write_text(DAILY, encoding="utf-8")

    assert _exit_code(tmp_path, "fact_keys.py", [], SILENT) == 1


def test_fact_keys_that_keyed_its_turns_or_had_none_is_a_clean_step(tmp_path):
    root = _vault(tmp_path)
    answered = {"MEMORY_LLM_PROVIDER": "fake", "MEMORY_LLM_FAKE_RESPONSE": json.dumps({"0": ["I own a drum set"]})}

    nothing_waiting = _exit_code(tmp_path, "fact_keys.py", [], SILENT)
    (root / "knowledge" / "daily" / "2023-05-22.md").write_text(DAILY, encoding="utf-8")

    assert (nothing_waiting, _exit_code(tmp_path, "fact_keys.py", [], answered)) == (0, 0)


def test_episodes_stopped_by_a_silent_provider_is_a_failed_step(tmp_path):
    root = _vault(tmp_path)
    (root / "knowledge" / "raw" / "sessions" / "2026-08-23" / "abc123.md").write_text(RECORD, encoding="utf-8")
    arguments = ["--vault", str(root), "--day", "2026-08-23"]

    assert _exit_code(tmp_path, "episode_consolidation.py", arguments, SILENT) == 1


def test_episodes_with_no_records_is_a_clean_step(tmp_path):
    arguments = ["--vault", str(_vault(tmp_path)), "--day", "2026-08-23"]

    assert _exit_code(tmp_path, "episode_consolidation.py", arguments, SILENT) == 0


def test_the_fact_keys_budget_leaves_the_margin_every_nightly_step_keeps():
    import fact_keys
    import scheduled_nightly

    inside = fact_keys.DEFAULT_BUDGET_SECONDS + scheduled_nightly.STEP_START_MARGIN_SECONDS

    assert inside <= scheduled_nightly._fact_keys_step().timeout
