"""Existing Windows ACL application and verification, independent of executors."""
from __future__ import annotations

import codecs
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

from llm_wiki.windows_native import windows_kernel32


def windows_acl_identity() -> str:
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{username}" if domain else username


def run_acl_command(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=5,
    )


def acl_permission(path: Path, identity: str) -> str:
    if path.is_dir():
        return f"{identity}:(OI)(CI)(F)"
    return f"{identity}:(F)"


def acl_failure(path: Path, changed: subprocess.CompletedProcess[bytes]) -> None:
    detail = acl_output_text(changed.stderr).strip()
    if not detail:
        detail = "icacls failed"
    raise PermissionError(f"could not apply owner-only ACL to {path}: {detail}")


def apply_windows_acl(
    path: Path, permission: str
) -> subprocess.CompletedProcess[bytes]:
    try:
        changed = run_acl_command(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/remove:g",
                "*S-1-5-18",
                "*S-1-5-32-544",
                "*S-1-5-32-545",
                "*S-1-5-11",
                "*S-1-3-0",
                "*S-1-3-4",
                "/grant:r",
                permission,
            ]
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError(f"could not apply owner-only ACL to {path}: {exc}") from exc
    if changed.returncode != 0:
        acl_failure(path, changed)
    return read_windows_acl(path)


def read_windows_acl(path: Path) -> subprocess.CompletedProcess[bytes]:
    try:
        verified = run_acl_command(["icacls", str(path)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError(f"could not verify owner-only ACL on {path}: {exc}") from exc
    if verified.returncode != 0:
        acl_failure(path, verified)
    return verified


def verified_owner_acl_line(
    path: Path, owner_lines: list[str]
) -> str:
    if len(owner_lines) != 1:
        raise PermissionError(f"owner-only ACL verification failed for {path}")
    owner = owner_lines[0]
    if "(F)" not in owner or "(I)" in owner:
        raise PermissionError(f"owner-only ACL verification failed for {path}")
    return owner


def verify_no_other_acl(path: Path, acl_lines: list[str], identity: str) -> None:
    for line in acl_lines:
        if not acl_line_names_owner(line, identity):
            raise PermissionError(f"owner-only ACL verification failed for {path}")


def harden_windows_acl(path: Path) -> None:
    identity = windows_acl_identity()
    permission = acl_permission(path, identity)
    verified = apply_windows_acl(path, permission)
    require_verified_windows_owner_acl(path, verified, identity)


def verify_windows_owner_acl(path: Path) -> None:
    """Read and enforce the same explicit owner-only ACL used by initial hardening."""
    require_verified_windows_owner_acl(path, read_windows_acl(path), windows_acl_identity())


def require_verified_windows_owner_acl(path, verified, identity):
    acl_lines = acl_lines_naming(verified.stdout, identity)
    owner_lines = [line for line in acl_lines if acl_line_names_owner(line, identity)]
    verified_owner_acl_line(path, owner_lines)
    verify_no_other_acl(path, acl_lines, identity)


def console_code_page() -> tuple[str, ...]:
    """The console's output code page as a codec, when there is a console and a codec."""
    name = f"cp{windows_kernel32().GetConsoleOutputCP()}"
    try:
        codecs.lookup(name)
    except LookupError:
        return ()
    return (name,)


def acl_output_encodings() -> tuple[str, ...]:
    """The code pages `icacls` may have written its pipe in, most likely first.

    No API reports which one it used, and a user name need not be ASCII. See
    `docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md`.
    """
    if sys.platform != "win32":  # the `oem` and `mbcs` codecs exist on Windows only
        return ("utf-8",)
    return tuple(dict.fromkeys((*console_code_page(), "oem", "mbcs")))


def acl_output_text(value: bytes | str | None, encoding: str | None = None) -> str:
    if isinstance(value, bytes):
        return value.decode(encoding or acl_output_encodings()[0], errors="replace")
    return value or ""


def acl_entry_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if ":(" in line]


def acl_line_names_owner(line: str, identity: str) -> bool:
    """Match the complete ACE principal, following an optional icacls path prefix."""
    pattern = r"(?:^|\s)" + re.escape(identity.casefold()) + r":\("
    return re.search(pattern, line.casefold()) is not None


def names_identity(acl_lines: list[str], identity: str) -> bool:
    return any(acl_line_names_owner(line, identity) for line in acl_lines)


def acl_lines_naming(stdout: bytes | str | None, identity: str) -> list[str]:
    """The ACL entries, read under the first code page in which the owner is named."""
    readings = [
        acl_entry_lines(acl_output_text(stdout, encoding))
        for encoding in acl_output_encodings()
    ]
    named = [lines for lines in readings if names_identity(lines, identity)]
    return (named or readings)[0]


def windows_acl_lines(path: Path, identity: str) -> list[str] | None:
    """The access control entries icacls reports, or None when it refuses.

    Read under the code page in which `identity` is named: the wrong one drops
    the letters of a name that is not ASCII, and an owner-only file then reads
    as somebody else's. Research:
    docs/research/2026-09-17-the-last-three-windows-readers-of-a-name-and-a-handle.md
    """

    try:
        verified = run_acl_command(["icacls", str(path)])
    except Exception:  # noqa: BLE001 - validation is fail-closed
        return None
    if verified.returncode != 0:
        return None
    return acl_lines_naming(verified.stdout, identity)


def acl_owner_lines(acl_lines: list[str], folded: str) -> list[str]:

    return [line for line in acl_lines if acl_line_names_owner(line, folded)]


def acl_is_owner_only(acl_lines: list[str], identity: str) -> bool:
    """One full-control entry for us, and nothing else at all."""
    folded = identity.casefold()
    owner_lines = acl_owner_lines(acl_lines, folded)
    if len(owner_lines) != 1 or "(F)" not in owner_lines[0]:
        return False
    return len(owner_lines) == len(acl_lines)


def is_owner_only_windows(path: Path) -> bool:

    identity = windows_acl_identity()
    acl_lines = windows_acl_lines(path, identity)
    if acl_lines is None:
        return False
    return acl_is_owner_only(acl_lines, identity)
