"""The shell profile block the installer owns."""

from __future__ import annotations

import shlex
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from install_state import (
    MAX_PREIMAGE_BYTES,
    InstallControlError,
    ManagedResource,
    atomic_external_write,
)
from llm_wiki.install_records import PROFILE_END, PROFILE_START
from llm_wiki.install_scheduler_render import provider_items
from reliable_memory import fsync_directory


def _profile_path_exists(profile: Path) -> bool:
    if profile.is_symlink():
        raise InstallControlError("install_profile_symlink")
    return _regular_profile_exists(profile)


def _regular_profile_exists(profile: Path) -> bool:
    if not profile.exists():
        return False
    if not profile.is_file():
        raise InstallControlError("install_profile_invalid")
    return True


def _read_profile(profile: Path) -> bytes:
    if not _profile_path_exists(profile):
        return b""
    return _read_bounded_profile(profile)


def _read_bounded_profile(profile: Path) -> bytes:
    if profile.stat().st_size > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_profile_invalid")
    value = profile.read_bytes()
    if len(value) > MAX_PREIMAGE_BYTES:
        raise InstallControlError("install_profile_invalid")
    return value


def _marker_bounds(content: bytes) -> tuple[int, int] | None:
    start_count = content.count(PROFILE_START)
    end_count = content.count(PROFILE_END)
    if start_count == end_count == 0:
        return None
    if start_count != 1 or end_count != 1:
        raise InstallControlError("install_profile_ownership_invalid")
    start = content.index(PROFILE_START)
    end = content.index(PROFILE_END, start) + len(PROFILE_END)
    return start, end


def _extract_profile_block(content: bytes) -> bytes | None:
    bounds = _marker_bounds(content)
    if bounds is None:
        return None
    start, end = bounds
    starts_on_line = (b"\n" + content[:start]).endswith(b"\n")
    ends_on_line = content[end : end + 1] in {b"", b"\n", b"\r"}
    if not starts_on_line or not ends_on_line:
        raise InstallControlError("install_profile_ownership_invalid")
    return content[start:end]


def _absolute_export(line: str, name: str) -> bool:
    prefix = f"export {name}="
    if not line.startswith(prefix):
        return False
    try:
        values = shlex.split(line[len(prefix) :])
    except ValueError:
        return False
    return len(values) == 1 and Path(values[0]).is_absolute()


def _decode_profile_lines(block: bytes) -> list[str] | None:
    try:
        return block.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _profile_markers_match(lines: Sequence[str]) -> bool:
    if len(lines) != 4:
        return False
    return lines[0].encode() == PROFILE_START and lines[3].encode() == PROFILE_END


def _recognized_profile(block: bytes) -> bool:
    lines = _decode_profile_lines(block)
    if lines is None or not _profile_markers_match(lines):
        return False
    return _absolute_export(lines[1], "LLM_WIKI_ROOT") and _absolute_export(
        lines[2], "LLM_WIKI_STATE_ROOT"
    )


def _replace_profile_block(content: bytes, updated: bytes) -> bytes:
    start, end = _marker_bounds(content) or (0, 0)
    return content[:start] + updated + content[end:]


def profile_separator(content: bytes) -> bytes:
    if not content or content.endswith(b"\n"):
        return b""
    return b"\n"


def after_profile_block(content: bytes, end: int) -> int:
    if content[end : end + 2] == b"\r\n":
        return end + 2
    if content[end : end + 1] == b"\n":
        return end + 1
    return end


def _remove_profile_block(content: bytes, separator: str) -> bytes:
    start, end = _marker_bounds(content) or (0, 0)
    if separator == "newline" and start > 0:
        start -= 1
    return content[:start] + content[after_profile_block(content, end) :]


def _profile_output(
    content: bytes,
    current: bytes | None,
    updated: bytes | None,
    separator: str,
) -> bytes:
    if current is not None:
        return _rewritten_profile_block(content, updated, separator)
    if updated is None:
        return content
    return content + profile_separator(content) + updated + b"\n"


def _rewritten_profile_block(content: bytes, updated: bytes | None, separator: str) -> bytes:
    """The profile with its existing managed block replaced, or removed when `updated` is None."""
    if updated is None:
        return _remove_profile_block(content, separator)
    return _replace_profile_block(content, updated)


def _restore_absent_profile(profile: Path, output: bytes, existed: bool) -> bool:
    if existed or output:
        return False
    profile.unlink(missing_ok=True)
    fsync_directory(profile.parent)
    return True


def _profile_file_metadata(profile: Path) -> tuple[int, tuple[int, int] | None]:
    try:
        metadata = profile.stat()
    except FileNotFoundError:
        return 0o600, None
    owner = (metadata.st_uid, metadata.st_gid)
    return stat.S_IMODE(metadata.st_mode), owner


def _write_profile(profile: Path, updated: bytes | None, metadata: Mapping[str, object]) -> None:
    content = _read_profile(profile)
    current = _extract_profile_block(content)
    separator = str(metadata.get("profile_insert_separator", "none"))
    output = _profile_output(content, current, updated, separator)
    if output == content:
        return
    existed = bool(metadata.get("profile_existed", True))
    if updated is None and _restore_absent_profile(profile, output, existed):
        return
    mode, owner = _profile_file_metadata(profile)
    profile.parent.mkdir(parents=True, exist_ok=True)
    atomic_external_write(profile, output, mode, owner)


def _profile_insert_separator(content: bytes) -> str:
    if content and not content.endswith(b"\n"):
        return "newline"
    return "none"


def profile_resource(
    profile: Path,
    vault_root: Path,
    state_root: Path,
    ownership_metadata: Mapping[str, object] | None = None,
) -> ManagedResource:
    profile = Path(profile).resolve(strict=False)
    content = _read_profile(profile)
    separator = _profile_insert_separator(content)
    metadata = dict(ownership_metadata or {})
    if ownership_metadata is None:
        metadata = {
            "profile_existed": profile.exists(),
            "profile_insert_separator": separator,
        }
    desired = b"\n".join(
        (
            PROFILE_START,
            f"export LLM_WIKI_ROOT={shlex.quote(str(Path(vault_root).resolve()))}".encode(),
            f"export LLM_WIKI_STATE_ROOT={shlex.quote(str(Path(state_root).resolve()))}".encode(),
            *(f"export {key}={shlex.quote(value)}".encode() for key, value in provider_items(vault_root)),
            PROFILE_END,
        )
    )
    return ManagedResource(
        resource_id="unix-profile",
        kind="profile_fragment",
        locator=str(profile),
        desired=desired,
        read_owned=lambda: _extract_profile_block(_read_profile(profile)),
        write_owned=lambda value: _write_profile(profile, value, metadata),
        recognizes=_recognized_profile,
        metadata=metadata,
        adopt_as_absent=True,
    )
