"""Check or explicitly repair an installed LLM-Wiki vault."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from installed_memory_repair import (
    describe_failure,
    inspect_installed_vault,
    repair_installed_vault,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Check or explicitly repair an installed LLM-Wiki vault.",
        epilog=(
            "This command never removes run/, knowledge, legacy caches, or "
            "compatibility markers."
        ),
    )
    mode = result.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="read-only validation; this is the default",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="permit the selected resumable repair",
    )
    result.add_argument(
        "--adopt-ownership-v3",
        action="store_true",
        help="perform the offline v3 adoption",
    )
    result.add_argument(
        "--confirm-all-agents-stopped",
        action="store_true",
        help="confirm every process using this vault is stopped for offline adoption",
    )
    result.add_argument("--root", type=Path, default=None)
    result.add_argument("--state-root", type=Path, default=None)
    result.add_argument("--json", action="store_true")
    result.add_argument(
        "--summary",
        action="store_true",
        help="print one plain line saying what the state means for session capture",
    )
    return result


_FLAG_RULES = (
    (lambda args: args.adopt_ownership_v3 and not args.apply, "--adopt-ownership-v3 requires --apply"),
    (
        lambda args: args.confirm_all_agents_stopped and not args.adopt_ownership_v3,
        "--confirm-all-agents-stopped requires --adopt-ownership-v3",
    ),
    (
        lambda args: args.adopt_ownership_v3 and not args.confirm_all_agents_stopped,
        "offline adoption requires --confirm-all-agents-stopped",
    ),
)


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    _require_coherent_flags(argument_parser, args)
    root_input = args.root or os.environ.get("LLM_WIKI_ROOT")
    if root_input is None:
        argument_parser.error("--root or LLM_WIKI_ROOT is required")
    mode_name = "apply" if args.apply else "check"
    status, report, payload = _run_repair(args, root_input, mode_name)
    _print_outcome(args, mode_name, status, report, payload)
    return {"ok": 0, "degraded": 1, "error": 2}[status]


def _require_coherent_flags(argument_parser, args) -> None:
    """The first flag combination that cannot run ends the command with its message."""
    for broken, message in _FLAG_RULES:
        if broken(args):
            argument_parser.error(message)


def _run_repair(args, root_input: str, mode_name: str) -> tuple[str, dict, str]:
    try:
        report = _repair_report(args, root_input)
        status = _report_status(report)
        payload = json.dumps(
            report,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    except Exception as exc:  # noqa: BLE001 - this is the redacted process boundary
        return _backend_error(mode_name, exc)
    return status, report, payload


def _resolved_path(value: str | Path) -> Path:
    """NUL is outside both POSIX and Windows filesystem path contracts."""
    if "\x00" in str(value):
        raise ValueError("embedded null byte")
    return Path(value).resolve()


def _repair_report(args, root_input: str) -> dict:
    root = _resolved_path(root_input)
    state_root = _resolved_path(
        args.state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root)
    )
    if args.apply:
        return repair_installed_vault(
            root=root,
            state_root=state_root,
            adopt_ownership_v3=args.adopt_ownership_v3,
            confirm_all_agents_stopped=args.confirm_all_agents_stopped,
        )
    return inspect_installed_vault(root=root, state_root=state_root)


def _report_status(report: dict) -> str:
    status = str(report.get("overall_status", ""))
    if status not in {"ok", "degraded", "error"}:
        raise ValueError("backend returned an invalid status")
    return status


def _backend_error(mode_name: str, exc: Exception) -> tuple[str, dict, str]:
    """The boundary's report names the redacted cause, never only that something failed."""
    status = "error"
    report = {
        "mode": mode_name,
        "overall_status": status,
        "actions": [],
        "blockers": [{"code": "repair_backend_error"}],
        "details": {"error": describe_failure(exc)},
    }
    return status, report, json.dumps(report, indent=2, sort_keys=True)


# What each adoption state means for session capture, in the words an operator
# reads. A state absent here is reported by name with its blockers and cause, and
# is never described as something it is not.
# See docs/research/2026-09-28-a-check-names-its-cause.md.
_SUMMARIES = {
    "adopted": "Reliability V3 is adopted; session capture is enabled.",
    "fresh": "Reliability V3 is not adopted yet (fresh vault); session capture waits for adoption.",
    "upgrade-required": (
        "Reliability V3 is not adopted: the earlier queue is still in use; "
        "session capture waits for adoption."
    ),
    "partial": "Reliability V3 adoption stopped part way; session capture waits until it resumes.",
    "unreadable": (
        "The Reliability V3 records could not be read just now; this says nothing "
        "about whether capture is enabled."
    ),
    "conflict": "The Reliability V3 records disagree or are invalid; session capture is refused.",
}


def summary_line(report: dict) -> str:
    """One plain line: what the adoption state is, what it means, and why it failed."""
    details = report.get("details", {})
    state = str(details.get("adoption_state") or "unknown")
    line = _SUMMARIES.get(state, f"Reliability V3 state is '{state}'.")
    return line + _cause(report, details)


def _cause(report: dict, details: dict) -> str:
    """The blocker codes and the redacted error, or nothing when there is neither."""
    codes = ", ".join(str(item.get("code")) for item in report.get("blockers", []))
    reasons = [part for part in (codes, details.get("error")) if part]
    if not reasons:
        return ""
    return f" Cause: {'; '.join(reasons)}"


def _print_outcome(args, mode_name: str, status: str, report: dict, payload: str) -> None:
    if args.json:
        print(payload)
        return
    if args.summary:
        print(summary_line(report))
        return
    print(f"{mode_name}: {status}")
    print(f"actions: {len(report.get('actions', []))}")
    print(f"blockers: {len(report.get('blockers', []))}")


if __name__ == "__main__":
    raise SystemExit(main())
