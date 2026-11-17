"""Turning install arguments into one request set and running it."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from install_state import (
    InstallControlError,
    ManagedResource,
    optional_install_record,
    replaced_whole,
    transaction_resources,
)
from llm_wiki import install_release, install_resource_builder, install_v2_transactions
from llm_wiki.install_rebuild import resources_from_record
from llm_wiki.install_record_reader import record_backend
from llm_wiki.install_records import (
    UNREADABLE_KINDS,
    ExplainedInstallError,
    Identity,
    adoptable,
    error_code,
    record_identity,
    records_by_identity,
    resource_identity,
    resources_by_identity,
)
from llm_wiki.install_scheduler_render import installer_stable_uv_path
from llm_wiki.install_uninstall_rollback import uninstall_resources
from llm_wiki.install_v2_transactions import transaction_settled, v2_nonterminal


def _requested_resources(args: argparse.Namespace, backend: str) -> list[ManagedResource]:
    resources = _built_resources(args, backend)
    return marked_adopted(resources, getattr(args, "adopt", None) or ())


def marked_adopted(
    resources: list[ManagedResource], adopt: Sequence[str]
) -> list[ManagedResource]:
    """The resources the operator named with `--adopt`; an unknown name is refused."""
    unknown = set(adopt) - {resource.resource_id for resource in resources}
    if unknown:
        raise InstallControlError("install_adopt_unknown_resource")
    return [_adopted(resource, adopt) for resource in resources]


def _adopted(resource: ManagedResource, adopt: Sequence[str]) -> ManagedResource:
    if resource.resource_id not in adopt:
        return resource
    if not adoptable(resource):
        raise InstallControlError("install_adopt_unsupported")
    return _taken_over(resource)


def _taken_over(resource: ManagedResource) -> ManagedResource:
    """A shared file is adopted as it is; a wholly owned one needs no flag to be replaced."""
    if replaced_whole(resource):
        return resource
    return replace(resource, adopt_current=True)


def _say_adopt_is_not_needed(resources: Sequence[ManagedResource], adopt: Sequence[str]) -> None:
    """`--adopt` of a wholly owned file still works, and says it is no longer needed."""
    for resource in resources:
        if resource.resource_id in adopt and replaced_whole(resource):
            print(
                f"install control: --adopt {resource.resource_id} is no longer needed; an "
                "update replaces this file and keeps the changed version.",
                file=sys.stderr,
            )


def _built_resources(args: argparse.Namespace, backend: str) -> list[ManagedResource]:
    return install_resource_builder.build_install_resources(
        backend=backend,
        root=args.root.resolve(),
        state_root=args.state_root.resolve(),
        uv_path=installer_stable_uv_path(args.uv_path),
        home=args.home.resolve(),
        profile=args.profile,
        powershell_path=args.powershell_path,
        opencode_plugin=args.opencode_plugin,
        claude_settings=args.claude_settings,
        codex_hooks=args.codex_hooks,
        ownership_metadata=None,
    )


def _record_is_requested(
    requested: Mapping[Identity, ManagedResource], record: Mapping[str, object]
) -> bool:
    return record_identity(record) in requested


def _settled_manifest(state_root: Path) -> dict[str, object] | None:
    install_root = state_root / "run" / "install"
    transaction = optional_install_record(
        install_root / "transaction.json", "install-transaction/"
    )
    if not transaction_settled(transaction):
        return None
    return optional_install_record(install_root / "manifest.json", "install-manifest/")


def _outgrown_manifest(
    state_root: Path, resources: Sequence[ManagedResource]
) -> dict[str, object] | None:
    """The active manifest, when it names something the new request no longer carries.

    An update can only grow: `_active_resource` fails closed on a recorded resource the
    request cannot supply. A request that dropped one, swapped the scheduler, or moved
    the profile to another file is a replacement, not an update. An unsettled
    transaction is left to the ordinary resume path.
    See docs/research/2026-09-17-a-rerun-may-ask-for-fewer-things.md.
    """
    manifest = _settled_manifest(state_root)
    if manifest is None:
        return None
    requested = resources_by_identity(resources)
    records = transaction_resources(manifest)
    if all(_record_is_requested(requested, record) for record in records):
        return None
    return manifest


def _install_request_set(
    args: argparse.Namespace, backend: str, released: Sequence[ManagedResource] = ()
) -> tuple[bool, dict[str, object]]:
    """Install the request; (whether it replaced a different recorded set, the manifest).

    A different set is one update: the new set is written first, what it no longer owns
    (a dropped agent, a profile that moved to another file) is taken back after it, and
    a failure reverts both. A cron table entry or a Windows task cannot be read back as
    it is, so a set change that involves one is still taken back first and put back if
    its replacement fails (docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md).
    """
    requested = _requested_resources(args, backend)
    manifest = _outgrown_manifest(args.state_root.resolve(), requested)
    if manifest is None:
        extras = _unfinished_extras(args, requested)
        return False, _install_requested(args, backend, requested, extras, released)
    retiring = _not_requested(resources_from_record(args, manifest), requested)
    unreadable = _unreadable([*retiring, *_not_recorded(requested, manifest)])
    if not unreadable:
        return True, _install_requested(args, backend, requested, retiring, released)
    return True, _take_back_first(args, backend, manifest, unreadable, released)


def _unreadable(resources: Sequence[ManagedResource]) -> list[ManagedResource]:
    return [resource for resource in resources if resource.kind in UNREADABLE_KINDS]


def _not_requested(
    recorded: Sequence[ManagedResource], requested: Sequence[ManagedResource]
) -> list[ManagedResource]:
    names = {resource_identity(resource) for resource in requested}
    return [resource for resource in recorded if resource_identity(resource) not in names]


def _not_recorded(
    requested: Sequence[ManagedResource], manifest: Mapping[str, object]
) -> list[ManagedResource]:
    recorded = records_by_identity(transaction_resources(manifest))
    return [resource for resource in requested if resource_identity(resource) not in recorded]


def _take_back_first(
    args: argparse.Namespace,
    backend: str,
    manifest: Mapping[str, object],
    unreadable: Sequence[ManagedResource],
    released: Sequence[ManagedResource],
) -> dict[str, object]:
    """The previous set is uninstalled before the new one, and put back if that fails."""
    named = ", ".join(f"{resource.resource_id} ({resource.locator})" for resource in unreadable)
    print(
        f"install control: {named} cannot be read back as it is, so the previous install is "
        "taken back before the new one is written, and put back if that fails.",
        file=sys.stderr,
    )
    try:
        uninstall_resources(
            state_root=args.state_root.resolve(), resources=resources_from_record(args, manifest)
        )
    except InstallControlError as error:
        raise ExplainedInstallError(error_code(error), _take_back_refusal(args, named)) from error
    return _install_or_restore(args, backend, manifest, released)


def _take_back_refusal(args: argparse.Namespace, named: str) -> str:
    return (
        f"the previous install could not be taken back before {named} replaces it. Restore "
        "what it names by hand, or run `uv run python scripts/install_control.py uninstall "
        f"--root {args.root} --state-root {args.state_root} --uv-path {args.uv_path} "
        f"--home {args.home}`, then run the installer again"
    )


def _unfinished_extras(
    args: argparse.Namespace, requested: Sequence[ManagedResource]
) -> list[ManagedResource]:
    """What an interrupted update recorded beyond the request, so the rerun can finish it."""
    transaction = optional_install_record(
        args.state_root.resolve() / "run" / "install" / "transaction.json", "install-transaction/"
    )
    if not v2_nonterminal(transaction) or transaction.get("operation") != "update":
        return []
    return _not_requested(resources_from_record(args, transaction), requested)


def _install_requested(
    args: argparse.Namespace,
    backend: str,
    requested: Sequence[ManagedResource],
    retiring: Sequence[ManagedResource] = (),
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    root = args.root.resolve()
    return install_v2_transactions.install_resources(
        state_root=args.state_root.resolve(),
        vault_root=root,
        release=install_release.build_release_identity(root),
        scheduler_backend=backend,
        resources=requested,
        control_version=2,
        retiring=retiring,
        released=released,
    )


def _restore_replaced(args: argparse.Namespace, replaced: Mapping[str, object]) -> None:
    """Put the previous install back after its replacement failed.

    The old set was uninstalled first, and a failed new install left the machine
    with no scheduler and no hooks (audit B-31,
    docs/research/2026-09-25-a-failed-reinstall-puts-the-old-one-back.md).
    """
    root = args.root.resolve()
    install_v2_transactions.install_resources(
        state_root=args.state_root.resolve(),
        vault_root=root,
        release=install_release.build_release_identity(root),
        scheduler_backend=record_backend(replaced),
        resources=resources_from_record(args, replaced),
        control_version=2,
    )


def _install_or_restore(
    args: argparse.Namespace,
    backend: str,
    replaced: Mapping[str, object],
    released: Sequence[ManagedResource] = (),
) -> dict[str, object]:
    """Install the request after the old set was taken back; a failure puts it back.

    A restore that fails itself raises with the install's failure as its context.
    """
    try:
        # Built after the old set is taken back: a resource records what it found on disk.
        requested = _requested_resources(args, backend)
        return _install_requested(args, backend, requested, released=released)
    except Exception:
        _restore_replaced(args, replaced)
        raise


def install_from_args(args: argparse.Namespace) -> dict[str, object]:
    from install_takeover import (
        discard_unneeded_copies,
        drop_in_resources,
        keep_changed_whole_files,
        report_takeovers,
    )

    backend = install_resource_builder.selected_backend(args.scheduler)
    requested = _requested_resources(args, backend)
    _say_adopt_is_not_needed(requested, getattr(args, "adopt", None) or ())
    takeovers = keep_changed_whole_files(args.state_root.resolve(), requested)
    released = drop_in_resources(takeovers, requested)
    try:
        replaced, manifest = _install_request_set(args, backend, released)
    except Exception:
        discard_unneeded_copies(takeovers, requested)
        raise
    report_takeovers(takeovers)
    return {
        "displaced": {takeover.resource_id: takeover.as_json() for takeover in takeovers},
        "replaced": replaced,
        "scheduler_backend": backend,
        "status": "committed",
        "transaction_id": manifest["transaction_id"],
    }
