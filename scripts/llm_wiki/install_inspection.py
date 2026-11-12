"""What an install left behind and what it does not own."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from install_state import (
    InstallControlError,
    optional_install_record,
)
from llm_wiki.install_resource_builder import opencode_plugin_destination


def installed_at(state_root: Path) -> datetime | None:
    """When the committed install began (`run/install/manifest.json`), or None.

    Doctor grades "never ran" and "no snapshot yet" from this instant: before
    the first pass is due they are pending, after it they are findings. See
    `docs/research/2026-09-27-what-is-not-yet-due-is-not-a-warning.md`.
    """
    path = Path(state_root) / "run" / "install" / "manifest.json"
    try:
        manifest = optional_install_record(path, "install-manifest/")
    except (InstallControlError, OSError, UnicodeError, ValueError):
        return None
    return _committed_instant(manifest)


def _committed_instant(manifest: Mapping[str, object] | None) -> datetime | None:
    stamp = (manifest or {}).get("committed_at")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


# What an install puts on a machine without owning it. Owning these would mean
# uninstall deleting them, and deleting the checkout deletes the vault — the
# operator's knowledge. So they are reported instead of owned: after an
# uninstall the operator is told exactly what is still there, rather than
# finding it later or losing it silently.
_UNOWNED_PATHS = (("checkout", "."), ("virtualenv", ".venv"))


def unowned_install_paths(root: Path) -> list[dict[str, str]]:
    """Paths the install created but will never remove, and whether they exist."""
    base = Path(root)
    return [
        {
            "kind": kind,
            "path": str((base / relative).resolve()),
            "state": "present" if (base / relative).exists() else "absent",
        }
        for kind, relative in _UNOWNED_PATHS
    ]


# Each agent keeps its own MCP registry, and the installers add `llm-wiki` to it outside
# the ownership transaction: `~/.claude.json` is Claude Code's live state, rewritten while
# it runs, so restoring a preimage would put a stale copy over newer state. They are
# reported like the paths above, with the way to remove the entry, never rewritten here.
# See docs/research/2026-09-17-an-uninstall-names-what-it-leaves.md.
MAX_REGISTRATION_BYTES = 64 * 1024 * 1024


def _agent_registrations(home: Path) -> list[tuple[str, Path, str, str]]:
    from installer_config import GLOBAL_CONFIG_NAMES

    opencode = opencode_plugin_destination(home).parent.parent
    return [
        ("claude", home / ".claude.json", '"llm-wiki"', "claude mcp remove --scope user llm-wiki"),
        (
            "codex",
            home / ".codex" / "config.toml",
            "[mcp_servers.llm-wiki]",
            "delete the [mcp_servers.llm-wiki] table",
        ),
        *(
            ("opencode", opencode / name, '"llm-wiki"', 'delete "llm-wiki" under "mcp"')
            for name in GLOBAL_CONFIG_NAMES
        ),
    ]


def _file_mentions(path: Path, marker: str) -> bool:
    if not path.is_file() or path.stat().st_size > MAX_REGISTRATION_BYTES:
        return False
    return marker in path.read_text(encoding="utf-8", errors="replace")


def unowned_agent_registrations(home: Path) -> list[dict[str, str]]:
    """Agent MCP entries the installers wrote and no uninstall takes back."""
    return [
        {"agent": agent, "path": str(path), "remove_with": remedy}
        for agent, path, marker, remedy in _agent_registrations(Path(home))
        if _file_mentions(path, marker)
    ]


def inspect_install_state(state_root: Path, root: Path | None = None) -> dict[str, object]:
    install_root = Path(state_root) / "run" / "install"
    manifest = "present" if (install_root / "manifest.json").is_file() else "absent"
    transaction = "present" if (install_root / "transaction.json").is_file() else "absent"
    status = "absent" if manifest == transaction == "absent" else "present"
    return {
        "manifest": manifest,
        "status": status,
        "transaction": transaction,
        "unowned": unowned_install_paths(root if root is not None else state_root),
    }
