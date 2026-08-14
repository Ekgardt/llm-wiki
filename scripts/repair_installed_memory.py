"""Check or explicitly repair an installed LLM-Wiki vault."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from installed_memory_repair import inspect_installed_vault, repair_installed_vault


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
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.adopt_ownership_v3 and not args.apply:
        argument_parser.error("--adopt-ownership-v3 requires --apply")
    if args.confirm_all_agents_stopped and not args.adopt_ownership_v3:
        argument_parser.error(
            "--confirm-all-agents-stopped requires --adopt-ownership-v3"
        )
    if args.adopt_ownership_v3 and not args.confirm_all_agents_stopped:
        argument_parser.error(
            "offline adoption requires --confirm-all-agents-stopped"
        )
    root_input = args.root or os.environ.get("LLM_WIKI_ROOT")
    if root_input is None:
        argument_parser.error("--root or LLM_WIKI_ROOT is required")
    mode_name = "apply" if args.apply else "check"
    try:
        root = Path(root_input).resolve()
        state_root = Path(
            args.state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root)
        ).resolve()
        report = (
            repair_installed_vault(
                root=root,
                state_root=state_root,
                adopt_ownership_v3=args.adopt_ownership_v3,
                confirm_all_agents_stopped=args.confirm_all_agents_stopped,
            )
            if args.apply
            else inspect_installed_vault(root=root, state_root=state_root)
        )
        status = str(report.get("overall_status", ""))
        if status not in {"ok", "degraded", "error"}:
            raise ValueError("backend returned an invalid status")
        payload = json.dumps(
            report,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    except Exception:  # noqa: BLE001 - this is the redacted process boundary
        status = "error"
        report = {
            "mode": mode_name,
            "overall_status": status,
            "actions": [],
            "blockers": [{"code": "repair_backend_error"}],
            "details": {},
        }
        payload = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        print(payload)
    else:
        print(f"{mode_name}: {status}")
        print(f"actions: {len(report.get('actions', []))}")
        print(f"blockers: {len(report.get('blockers', []))}")
    return {"ok": 0, "degraded": 1, "error": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
