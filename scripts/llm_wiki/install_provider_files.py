"""Provider file identity and opening."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from install_state import (
    InstallControlError,
    active_install_health_for_schema,
    transaction_resources,
)
from llm_wiki.install_record_reader import require_record_roots
from llm_wiki.install_resource_builder import codex_hooks_destination


def provider_security_identity(handle):
    if os.name != "nt":
        return None
    import msvcrt

    import pywintypes
    import win32security
    import windows_workspace

    native_handle = msvcrt.get_osfhandle(handle.fileno())
    file_identity = windows_workspace.identity(native_handle, directory=False)
    flags = (win32security.OWNER_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION
             | win32security.DACL_SECURITY_INFORMATION)
    try:
        descriptor = win32security.GetSecurityInfo(
            native_handle, win32security.SE_FILE_OBJECT, flags)
    except pywintypes.error as exc:
        raise InstallControlError("install_provider_security_unverified") from exc
    return file_identity, bytes(descriptor)


def provider_handle_identity(handle):
    info = os.fstat(handle.fileno())
    metadata = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return metadata, provider_security_identity(handle)


def require_provider_identity(actual, expected):
    if actual != expected:
        raise InstallControlError("install_provider_snapshot_changed")


def provider_binary_file(path):
    from llm_wiki.generation_descriptors import open_read_descriptor

    descriptor = open_read_descriptor(path)
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def provider_open(stack, path):
    handle = stack.enter_context(provider_binary_file(path))
    if path.is_symlink() or not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        raise InstallControlError("install_provider_snapshot_changed")
    return handle


def qualified_codex_desired_record(install_root: Path, manifest: Mapping[str, object],
                                    transaction: Mapping[str, object], root: Path,
                                    state_root: Path, home: Path) -> dict[str, object]:
    require_record_roots(manifest, root.resolve(), state_root.resolve())
    health = active_install_health_for_schema(install_root, manifest, transaction)
    if health["status"] != "active" or health["health"] != "ok":
        raise InstallControlError("install_provider_state_unverified")
    return _single_codex_desired_record(manifest, home)


def _single_codex_desired_record(manifest: Mapping[str, object], home: Path) -> dict[str, object]:
    records = [record for record in transaction_resources(manifest)
               if record.get("id") == "codex-user-hooks"]
    if len(records) != 1:
        raise InstallControlError("install_provider_resource_absent")
    return _qualified_codex_resource(records[0], home)


def _qualified_codex_resource(record: dict[str, object], home: Path) -> dict[str, object]:
    expected = str(codex_hooks_destination(home.resolve()))
    if record.get("kind") != "codex_hooks_fragment" or record.get("locator") != expected:
        raise InstallControlError("install_provider_resource_mismatch")
    if record.get("state") != "verified" or not isinstance(record.get("desired"), Mapping):
        raise InstallControlError("install_provider_resource_unverified")
    return record


def codex_desired_bundle(desired: bytes | None, root: Path) -> dict[str, str]:
    from integration_hook_config import codex_projection_bundle

    if desired is None:
        raise InstallControlError("install_provider_desired_absent")
    try:
        return codex_projection_bundle(desired, root)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise InstallControlError("install_provider_desired_invalid") from exc
