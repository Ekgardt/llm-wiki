"""UserPromptSubmit compatibility entrypoint for durable, non-LLM capture.

The common adapter preserves the complete redacted occurrence before advisory
or project follow-ups. Delivery to the original day's journal is recoverable.
Empty prompts are ignored; separate meaningful occurrences are never suppressed
by their text, size, or frequency. Failures are recorded without blocking the host.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# Force UTF-8 stdout (Windows console default is cp1251 — breaks emoji
# and non-ASCII prompts).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
# A missing `memory_state` used to be replaced by no-op stand-ins, so the hook ran
# and silently wrote nothing; now the import fails, the process exits non-zero,
# and the adapter records the lost capture (`record_failed_delegate`).

from capture_diagnostics import hook_object, record_capture_failure  # noqa: E402


def _read_hook_input() -> dict:
    """Parse Claude Code hook JSON from stdin. Tolerant of empty stdin."""
    # Read errors belong to main's existing recorded-failure boundary; they
    # must not be turned into an ordinary empty host invocation.
    return hook_object(sys.stdin.read(), "prompt_input")


def should_skip(prompt: str) -> bool:
    """Only empty input is skipped.

    It also skipped every prompt inside the vault, as "maintenance loops"; the
    adapter's reentry marker has named the memory's own processes exactly since
    2026-09-17, and the vault rule only hid the owner's own sessions there. See
    `docs/research/2026-09-24-a-long-session-in-the-vault-is-captured.md`.
    """
    return not prompt.strip()




def record_prompt(hook: dict, prompt: str) -> None:
    """Direct hook invocations use the same durable publisher as host adapters."""
    from integration_adapter import ingest_event, normalize_occurrence_event

    envelope = normalize_occurrence_event(
        str(hook.get("agent") or "claude"), "user_prompt", {**hook, "prompt": prompt},
    )
    ingest_event(envelope)


def main() -> int:
    try:
        hook = _read_hook_input()
        prompt = str(hook.get("prompt") or "")
        if should_skip(prompt):
            return 0
        record_prompt(hook, prompt)
    except Exception as error:  # noqa: BLE001
        # Last-resort: never break the user's session over a logging hook,
        # but never lose the capture silently either.
        record_capture_failure(
            "user_prompt_hook", f"{type(error).__name__}: {error}", error=error
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
