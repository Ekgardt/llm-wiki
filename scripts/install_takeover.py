"""What an update does with a wholly owned file that was changed outside the installer.

The update replaces the file (`install_control`, `_WHOLLY_OWNED_KINDS`); the changed
version stays the transaction's rollback point. Before that happens, this module keeps
a readable copy of the changed version under `run/install/displaced/`, plans a drop-in
`<unit>.d/50-local.conf` for the lines added by hand to a systemd unit, and says what
it moved and what the new version does not carry. The drop-in is written by the same
transaction as the unit and handed over when it commits, so the rollback of that update
removes it and nothing later touches it. launchd and the OpenCode plugin have no
drop-ins: their copy and the report are what keeps the edit. Copies older than the
setting `retention.config_backup_days` go, unless the current rollback point is one.
See docs/research/2026-09-28-an-update-replaces-what-it-owns.md and
docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md.

A line added to a unit section is appended to that section by a drop-in, because
systemd parses drop-ins after the unit file. A line that replaced one of ours with the
same key is not moved: in a drop-in it would add to ours (a second `ExecStart=` runs a
second command) instead of replacing it, so it is reported instead.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import install_control as control
from settings import setting_value

DROP_IN_NAME = "50-local.conf"


@dataclass(frozen=True, slots=True)
class Takeover:
    """One wholly owned resource whose changed version this update replaces."""

    resource_id: str
    locator: str
    kept: str
    moved: list[str] = field(default_factory=list)
    unmoved: list[str] = field(default_factory=list)
    undone: list[str] = field(default_factory=list)
    drop_ins: list[str] = field(default_factory=list)
    planned: dict[str, bytes] = field(default_factory=dict, repr=False)

    def as_json(self) -> dict[str, object]:
        return {
            "drop_ins": self.drop_ins,
            "kept": self.kept,
            "locator": self.locator,
            "moved": self.moved,
            "undone": self.undone,
            "unmoved": self.unmoved,
        }


@dataclass(frozen=True, slots=True)
class _Line:
    section: str
    text: str

    @property
    def key(self) -> str:
        return self.text.split("=", 1)[0].strip()


def keep_changed_whole_files(
    state_root: Path, resources: Sequence[control.ManagedResource]
) -> list[Takeover]:
    """Keep and carry over each wholly owned file this update is about to replace."""
    retire_displaced_copies(Path(state_root))
    installed = _installed_values(Path(state_root))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    found = (_changed(resource, installed) for resource in resources)
    return [
        _take_over(Path(state_root), stamp, resource, current, baseline)
        for resource, current, baseline in found
        if current is not None
    ]


def drop_in_resources(
    takeovers: Iterable[Takeover], resources: Sequence[control.ManagedResource]
) -> list[control.ManagedResource]:
    """The planned drop-ins as resources the install transaction writes and hands over."""
    by_id = {resource.resource_id: resource for resource in resources}
    return [
        by_id[takeover.resource_id].make_drop_in(Path(path), text)
        for takeover in takeovers
        for path, text in takeover.planned.items()
    ]


def report_takeovers(takeovers: Iterable[Takeover]) -> None:
    for takeover in takeovers:
        print(_report(takeover), file=sys.stderr)


def _report(takeover: Takeover) -> str:
    parts = [
        f"install control: {takeover.resource_id} ({takeover.locator}) was changed outside "
        f"the installer and is replaced by this version. The changed version is kept in "
        f"{takeover.kept}, and `install_control.py rollback` puts it back."
    ]
    parts.extend(_report_lines(takeover))
    return " ".join(parts)


def _report_lines(takeover: Takeover) -> list[str]:
    said = [
        ("Moved to " + ", ".join(takeover.drop_ins) + ":", takeover.moved),
        ("Not carried over, set them again in a drop-in or by hand:", takeover.unmoved),
        ("Restored after being removed or changed by hand:", takeover.undone),
    ]
    return [f"{title} {'; '.join(lines)}." for title, lines in said if lines]


# --- What changed -------------------------------------------------------------------


def _installed_values(state_root: Path) -> dict[tuple[str, str, str], bytes]:
    """What the active manifest says each resource was installed as, where it can be read."""
    install_root = state_root / "run" / "install"
    manifest = control._optional_install_record(install_root / "manifest.json", "install-manifest/")
    if manifest is None:
        return {}
    records = control._transaction_resources(manifest)
    values = ((_identity(record), _installed_value(install_root, record)) for record in records)
    return {identity: value for identity, value in values if value is not None}


def _identity(record: Mapping[str, object]) -> tuple[str, str, str]:
    return (str(record.get("id")), str(record.get("kind")), str(record.get("locator")))


def _installed_value(install_root: Path, record: Mapping[str, object]) -> bytes | None:
    desired = control._record_desired(record)
    if not isinstance(desired.get("preimage"), str):
        return None
    return control._read_origin(install_root, desired)


def _changed(
    resource: control.ManagedResource, installed: Mapping[tuple[str, str, str], bytes]
) -> tuple[control.ManagedResource, bytes | None, bytes]:
    """(resource, the changed value or None, what it is compared with)."""
    if not control._replaced_whole(resource):
        return resource, None, resource.desired
    identity = (resource.resource_id, resource.kind, resource.locator)
    baseline = installed.get(identity, resource.desired)
    current = control._adopted_value(resource)
    if current in (None, baseline, resource.desired):
        return resource, None, baseline
    return resource, current, baseline


# --- Keeping and carrying over --------------------------------------------------------


def _take_over(
    state_root: Path,
    stamp: str,
    resource: control.ManagedResource,
    current: bytes,
    baseline: bytes,
) -> Takeover:
    files = _files(resource, current)
    kept = _keep_copy(_displaced(state_root) / f"{stamp}-{resource.resource_id}", files, current)
    changes = [_file_changes(name, _files(resource, baseline).get(name, b""), value) for name, value in files.items()]
    takeover = Takeover(resource_id=resource.resource_id, locator=resource.locator, kept=str(kept))
    for name, added, removed in changes:
        _carry_over(takeover, resource, name, added, removed)
    return takeover


def _files(resource: control.ManagedResource, value: bytes) -> dict[str, bytes]:
    """The value as the files it stands for: a scheduler's unit set, or the one file."""
    if resource.read_current is None:
        return {Path(resource.locator).name: value}
    texts = control._strict_json_object(value).get("definitions", {})
    return {name: control._decode_definition_value(text) for name, text in texts.items()}


def _keep_copy(directory: Path, files: Mapping[str, bytes], value: bytes) -> Path:
    """The files, and the digest that tells a retirement whether a rollback still needs them."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in files.items():
        control._atomic_write(directory / name, content)
    control._atomic_write(directory / _COPY_RECORD, json.dumps({"sha256": control._sha256(value)}).encode())
    return directory


# --- Retiring old copies ----------------------------------------------------------------

_COPY_RECORD = "copy.json"
# Seconds per day: the unit `retention.config_backup_days` is stated in, a conversion
# and not a limit; the limit is the setting.
_DAY_SECONDS = 86_400


def _displaced(state_root: Path) -> Path:
    return Path(state_root) / "run" / "install" / "displaced"


def retire_displaced_copies(state_root: Path, now: float | None = None) -> list[Path]:
    """Remove copies older than `retention.config_backup_days` that no rollback needs.

    The same window as the agent-config backups, for the same reason: both undo an
    installer rewrite. A copy whose digest the active manifest or transaction names is
    the current rollback point and stays whatever its age. Every install runs this, and
    only an install writes copies, so the directory holds the edits of one window.
    """
    root = _displaced(state_root)
    if not root.is_dir():
        return []
    cutoff = _cutoff(now)
    needed = _referenced_digests(Path(state_root))
    retired = [copy for copy in root.iterdir() if _retirable(copy, cutoff, needed)]
    for copy in retired:
        shutil.rmtree(copy)
    return retired


def _cutoff(now: float | None) -> float:
    moment = time.time() if now is None else now
    return moment - setting_value("retention.config_backup_days") * _DAY_SECONDS


def _referenced_digests(state_root: Path) -> set[str]:
    install_root = state_root / "run" / "install"
    names = (("manifest.json", "install-manifest/"), ("transaction.json", "install-transaction/"))
    records = [control._optional_install_record(install_root / name, kind) for name, kind in names]
    references = set().union(*(control._preimage_references(record) for record in records if record))
    return {PurePosixPath(reference).stem for reference in references}


def _retirable(copy: Path, cutoff: float, needed: set[str]) -> bool:
    if copy.is_symlink() or not copy.is_dir():
        return False
    return copy.stat().st_mtime < cutoff and _copy_digest(copy) not in needed


def _copy_digest(copy: Path) -> str | None:
    record = copy / _COPY_RECORD
    if not record.is_file():
        return None
    return json.loads(record.read_bytes()).get("sha256")


def _lines(value: bytes) -> list[_Line]:
    section = ""
    found = []
    for raw in value.decode("utf-8", "replace").splitlines():
        text = raw.strip()
        section = _section(text, section)
        found.append(_Line(section, text))
    return [line for line in found if _setting(line.text)]


def _section(text: str, current: str) -> str:
    if text.startswith("[") and text.endswith("]"):
        return text
    return current


def _setting(text: str) -> bool:
    """A line that says something: not blank, not a comment, not a section header."""
    return bool(text) and not text.startswith(("#", ";", "["))


def _file_changes(name: str, baseline: bytes, current: bytes) -> tuple[str, list[_Line], list[_Line]]:
    before, after = _lines(baseline), _lines(current)
    added = [line for line in after if line not in before]
    removed = [line for line in before if line not in after]
    return name, added, removed


def _carry_over(
    takeover: Takeover,
    resource: control.ManagedResource,
    name: str,
    added: list[_Line],
    removed: list[_Line],
) -> None:
    moved = _moved_to_drop_in(takeover, resource, name, _movable(added, removed))
    takeover.moved.extend(_named(name, moved))
    takeover.unmoved.extend(_named(name, _without(added, moved)))
    takeover.undone.extend(_named(name, removed))


def _movable(added: list[_Line], removed: list[_Line]) -> list[_Line]:
    """Added lines that did not replace one of ours with the same key."""
    replaced = {(line.section, line.key) for line in removed}
    return [line for line in added if (line.section, line.key) not in replaced]


def _without(lines: list[_Line], taken: list[_Line]) -> list[_Line]:
    return [line for line in lines if line not in taken]


def _named(name: str, lines: list[_Line]) -> list[str]:
    return [f"{name}: {line.text}" for line in lines]


def _moved_to_drop_in(
    takeover: Takeover,
    resource: control.ManagedResource,
    name: str,
    lines: list[_Line],
) -> list[_Line]:
    """Plan the lines into the unit's drop-in; the lines it will hold, or none."""
    if resource.kind != "systemd_scheduler" or not lines:
        return []
    path = Path(str(resource.metadata["unit_directory"])) / f"{name}.d" / DROP_IN_NAME
    if path.exists() or path.is_symlink():
        return []
    takeover.drop_ins.append(str(path))
    takeover.planned[str(path)] = _drop_in_text(name, lines)
    return lines


def _drop_in_text(name: str, lines: Sequence[_Line]) -> bytes:
    header = [
        f"# Moved here by the llm-wiki installer from {name}, where it was added by hand.",
        f"# The installer replaces {name} on every update and leaves this file to you;",
        "# only a rollback of the update that wrote it removes it.",
    ]
    body: list[str] = []
    for section in dict.fromkeys(line.section for line in lines):
        body.extend(["", section, *(line.text for line in lines if line.section == section)])
    return ("\n".join(header + body) + "\n").encode("utf-8")
