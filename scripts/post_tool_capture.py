"""PostToolUse compatibility entrypoint for complete, durable, non-LLM capture.

The common adapter persists the redacted occurrence before asynchronous journal
delivery. No direct append, text truncation, tool filter or time suppression lives
here. Historical --background invocations keep using this same ingress.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_diagnostics import hook_object, record_capture_failure  # noqa: E402


def _read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as error:
        record_capture_failure("tool_input", "hook input could not be read", error=error)
        return {}
    return hook_object(raw, "tool_input")


def _capture_tool(hook: dict) -> None:
    """Direct hook invocations use the common durable ingress."""
    if not hook:
        return
    from integration_adapter import ingest_event, normalize_occurrence_event

    envelope = normalize_occurrence_event(
        str(hook.get("agent") or "claude"), "post_tool_use", hook,
    )
    ingest_event(envelope)


def main(arguments: list[str] | None = None) -> int:
    # Older installed hooks may still pass --background; durable ingress is the same.
    try:
        _capture_tool(_read_hook_input())
    except Exception as error:  # noqa: BLE001
        record_capture_failure(
            "post_tool_hook", f"{type(error).__name__}: {error}", error=error
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
