"""Running the delegate scripts an agent hook event is routed to."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_wiki import runtime_roots
from llm_wiki.integration_settings import (
    BACKGROUND_DELEGATE_TIMEOUTS,
    DELEGATE_TIMEOUT_SECONDS,
    DELEGATE_TIMEOUTS,
    DELEGATES,
    SCRIPTS_DIR,
)


def run_delegate(
    name: str,
    payload: Mapping[str, Any],
    *,
    forward_stdout: bool = False,
    project_dir: Path | None = None,
    background: bool = False,
) -> subprocess.CompletedProcess[str]:
    if name not in DELEGATES:
        raise ValueError("invalid integration delegate")
    env = os.environ.copy()
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *delivery_arguments(background)],
        cwd=str(runtime_roots.vault_root()),
        env=env,
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=delegate_timeout(name, background),
    )
    _forward_delegate_stdout(result, forward_stdout)
    record_failed_delegate(name, result)
    return result


def delivery_arguments(background: bool) -> list[str]:
    return ["--background"] if background else []


def delegate_timeout(name: str, background: bool) -> float:
    if background and name in BACKGROUND_DELEGATE_TIMEOUTS:
        return BACKGROUND_DELEGATE_TIMEOUTS[name]
    return DELEGATE_TIMEOUTS.get(name, DELEGATE_TIMEOUT_SECONDS)


def record_failed_delegate(name: str, result: subprocess.CompletedProcess[str]) -> None:
    """A delegate that exited non-zero is a lost capture, and says why.

    Its exit status used to be read only to decide whether to forward its
    output, so a delegate that could not even import died without a trace. See
    `docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.
    """
    if result.returncode == 0:
        return
    from capture_diagnostics import record_capture_failure

    last = (result.stderr or "").strip().splitlines()[-1:] or ["no error output"]
    record_capture_failure(f"delegate_{Path(name).stem}", f"exit {result.returncode}: {last[0]}")


def _forward_delegate_stdout(
    result: subprocess.CompletedProcess[str], forward_stdout: bool
) -> None:
    if result.returncode != 0 or not forward_stdout:
        return
    if _is_hook_output(result.stdout):
        sys.stdout.write(result.stdout)


def _is_hook_output(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    hook_output = parsed.get("hookSpecificOutput")
    return isinstance(hook_output, dict) and isinstance(hook_output.get("additionalContext"), str)

