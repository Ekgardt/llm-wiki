"""Bounded install ownership and recovery control plane."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from install_state import InstallControlError as InstallControlError
from install_state import ManagedResource as ManagedResource
from llm_wiki.formats.canonical import canonical_json_bytes as canonical_json_bytes
from llm_wiki.install_cron import cron_scheduler_resource as cron_scheduler_resource
from llm_wiki.install_cron import render_cron_block as render_cron_block
from llm_wiki.install_inspection import inspect_install_state as inspect_install_state
from llm_wiki.install_inspection import installed_at as installed_at
from llm_wiki.install_inspection import unowned_agent_registrations as unowned_agent_registrations
from llm_wiki.install_launchd import LAUNCHD_SERVICE_NOT_FOUND as LAUNCHD_SERVICE_NOT_FOUND
from llm_wiki.install_launchd import bootout_if_loaded as bootout_if_loaded
from llm_wiki.install_launchd import launchd_scheduler_resource as launchd_scheduler_resource
from llm_wiki.install_launchd import uninstall_launchd as uninstall_launchd
from llm_wiki.install_profile import profile_resource as profile_resource
from llm_wiki.install_provider_files import codex_desired_bundle as codex_desired_bundle
from llm_wiki.install_provider_files import provider_binary_file as provider_binary_file
from llm_wiki.install_provider_files import provider_handle_identity as provider_handle_identity
from llm_wiki.install_provider_files import provider_open as provider_open
from llm_wiki.install_provider_files import provider_security_identity as provider_security_identity
from llm_wiki.install_provider_files import require_provider_identity as require_provider_identity
from llm_wiki.install_provider_proof import (
    installed_codex_provider_bundle as installed_codex_provider_bundle,
)
from llm_wiki.install_provider_proof import provider_snapshot_proof as provider_snapshot_proof
from llm_wiki.install_rebuild import resources_from_existing_args as resources_from_existing_args
from llm_wiki.install_record_reader import existing_record as existing_record
from llm_wiki.install_records import PROFILE_END as PROFILE_END
from llm_wiki.install_records import install_lock as install_lock
from llm_wiki.install_release import build_release_identity as build_release_identity
from llm_wiki.install_requests import install_from_args as install_from_args
from llm_wiki.install_requests import marked_adopted as marked_adopted
from llm_wiki.install_resource_builder import base_install_resources as base_install_resources
from llm_wiki.install_resource_builder import build_install_resources as build_install_resources
from llm_wiki.install_resource_builder import (
    opencode_plugin_destination as opencode_plugin_destination,
)
from llm_wiki.install_resource_builder import posix_scheduler_resource as posix_scheduler_resource
from llm_wiki.install_resource_builder import selected_backend as selected_backend
from llm_wiki.install_scheduler_projection import definition_digest as definition_digest
from llm_wiki.install_scheduler_render import SCHEDULER_LIMIT_HOURS as SCHEDULER_LIMIT_HOURS
from llm_wiki.install_scheduler_render import launchd_job as launchd_job
from llm_wiki.install_scheduler_render import (
    render_launchd_definitions as render_launchd_definitions,
)
from llm_wiki.install_scheduler_render import (
    render_systemd_definitions as render_systemd_definitions,
)
from llm_wiki.install_scheduler_render import scheduled_arguments as scheduled_arguments
from llm_wiki.install_scheduler_render import scheduler_limit_hours as scheduler_limit_hours
from llm_wiki.install_scheduler_render import select_scheduler_backend as select_scheduler_backend
from llm_wiki.install_scheduler_render import systemd_service as systemd_service
from llm_wiki.install_systemd import systemd_scheduler_resource as systemd_scheduler_resource
from llm_wiki.install_transactions import set_transaction_state as set_transaction_state
from llm_wiki.install_uninstall_rollback import rollback_resources as rollback_resources
from llm_wiki.install_uninstall_rollback import uninstall_resources as uninstall_resources
from llm_wiki.install_v2_mutation import v2_mutate_resources as v2_mutate_resources
from llm_wiki.install_v2_transactions import install_resources as install_resources
from llm_wiki.install_windows import WINDOWS_TASK_LIMIT_HOURS as WINDOWS_TASK_LIMIT_HOURS
from llm_wiki.install_windows import decode_windows_task_spec as decode_windows_task_spec
from llm_wiki.install_windows import render_windows_task_spec as render_windows_task_spec
from llm_wiki.install_windows import windows_environment_resources as windows_environment_resources
from llm_wiki.install_windows import (
    windows_task_command_from_spec as windows_task_command_from_spec,
)
from llm_wiki.install_windows import (
    windows_task_scheduler_resource as windows_task_scheduler_resource,
)


def rollback_from_args(args: argparse.Namespace) -> dict[str, object]:
    result = rollback_resources(
        state_root=args.state_root.resolve(),
        resources=resources_from_existing_args(args, "rollback"),
    )
    return {"state": result["state"], "status": "rolled_back"}


def uninstall_from_args(args: argparse.Namespace) -> dict[str, object]:
    result = uninstall_resources(
        state_root=args.state_root.resolve(),
        resources=resources_from_existing_args(args, "uninstall"),
    )
    return {
        "left_behind": unowned_agent_registrations(args.home.resolve()),
        "state": result["state"],
        "status": "uninstalled",
    }


def _status_from_args(args: argparse.Namespace) -> dict[str, object]:
    state = inspect_install_state(args.state_root, getattr(args, "root", None))
    home = getattr(args, "home", None)
    if home is None:
        return state
    return {**state, "agent_registrations": unowned_agent_registrations(home.resolve())}


def _add_existing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--uv-path", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--powershell-path")


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--state-root", type=Path, required=True)
    status.add_argument("--home", type=Path)
    install = subparsers.add_parser("install")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--state-root", type=Path, required=True)
    install.add_argument("--uv-path", type=Path, required=True)
    install.add_argument("--home", type=Path, required=True)
    install.add_argument("--scheduler", choices=("native", "cron"), default="native")
    install.add_argument("--profile", type=Path)
    install.add_argument("--powershell-path")
    install.add_argument("--opencode-plugin", action="store_true")
    install.add_argument("--claude-settings", action="store_true")
    install.add_argument("--codex-hooks", action="store_true")
    install.add_argument(
        "--adopt",
        action="append",
        default=[],
        metavar="RESOURCE_ID",
        help="take over this resource as it is now, keeping its content as the rollback point",
    )
    _add_existing_arguments(subparsers.add_parser("rollback"))
    _add_existing_arguments(subparsers.add_parser("uninstall"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        handlers: dict[str, Callable[[argparse.Namespace], Mapping[str, object]]] = {
            "install": install_from_args,
            "rollback": rollback_from_args,
            "status": _status_from_args,
            "uninstall": uninstall_from_args,
        }
        result = handlers[args.command](args)
        print(canonical_json_bytes(result).decode("utf-8"))
        return 0
    except (InstallControlError, OSError, UnicodeError, ValueError) as exc:
        print(f"install control failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
