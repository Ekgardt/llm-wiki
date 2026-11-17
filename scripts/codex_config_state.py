"""What the Codex config says about this vault's MCP server."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

# Codex's config.toml and hooks.json are read whole before a merge; 256 KiB refuses a file that is
# not a hand-kept configuration. Basis unknown: value predates measurement; review when a real
# Codex config nears it.
MAX_HOOK_CONFIG_BYTES = 256 * 1024


def require_safe_config(path: Path, error: str) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HOOK_CONFIG_BYTES:
        raise ValueError(error)


def read_codex_toml(config: Path) -> dict[str, Any]:
    require_safe_config(config, "invalid Codex config")
    try:
        document = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("invalid Codex config") from exc
    if not isinstance(document, dict):
        raise ValueError("invalid Codex config")
    return document


def _mcp_servers(document: dict[str, Any]) -> dict[str, Any] | str:
    servers = document.get("mcp_servers")
    if servers is None:
        return "absent"
    if not isinstance(servers, dict):
        return "conflict"
    return servers


def _mcp_entry(document: dict[str, Any]) -> dict[str, Any] | str:
    servers = _mcp_servers(document)
    if isinstance(servers, str):
        return servers
    return _llm_wiki_table(servers.get("llm-wiki"))


def _llm_wiki_table(table: object) -> dict[str, Any] | str:
    if table is None:
        return "absent"
    if not isinstance(table, dict):
        return "conflict"
    return table


def mcp_expected_args(vault_root: Path, provider_bundle: dict[str, str] | None = None) -> list[str]:
    from integration_hook_config import codex_launch_args

    return codex_launch_args(vault_root, "mcp_server.py", (), provider_bundle, relative_target=True)


# The keys this product has ever written into its Codex entry, and the tail every
# form of its arguments ended with: `uv run [--locked --no-sync] --directory <vault>
# python scripts/mcp_server.py` (the pre-bootstrap form retained for migration).
# An entry of that shape
# with other arguments is this product's own earlier entry, `stale`, and the installer
# rewrites it; anything else is the operator's (`conflict`), and an entry switched
# off with `enabled = false` is the operator's choice (`disabled`).
# See docs/research/2026-09-28-a-check-names-its-cause.md.
_OUR_ENTRY_KEYS = frozenset({"command", "args", "enabled"})
_OUR_ENTRY_TAIL = ["python", "scripts/mcp_server.py"]


def _mcp_entry_state(table: dict[str, Any], vault_root: Path,
                     provider_bundle: dict[str, str] | None = None) -> str:
    """The first state whose test the entry meets, in the order they are listed."""
    if _switched_off(table, vault_root):
        return "disabled"
    if table.get("command") == "uv" and table.get("args") == mcp_expected_args(vault_root, provider_bundle):
        return "equivalent"
    return "stale" if _written_by_this_product(table, vault_root) else "conflict"


def _switched_off(table: dict[str, Any], _vault_root: Path) -> bool:
    return table.get("enabled", True) is False


def _written_by_this_product(table: dict[str, Any], _vault_root: Path) -> bool:
    args = table.get("args")
    if table.get("command") != "uv" or not isinstance(args, list):
        return False
    return set(table) <= _OUR_ENTRY_KEYS and (_our_argument_shape(args) or _owned_bootstrap_mcp(args, _vault_root))


def _owned_bootstrap_mcp(args: list[object], vault_root: Path) -> bool:
    from integration_hook_config import codex_launch_bundle

    try:
        codex_launch_bundle(args, vault_root)
        return args[9:] == ["scripts/mcp_server.py"]
    except (ValueError, TypeError, UnicodeError):
        return False


def _our_argument_shape(args: list[object]) -> bool:
    return args[:1] == ["run"] and args[-2:] == _OUR_ENTRY_TAIL and "--directory" in args


def _read_codex_document(config: Path) -> dict[str, Any] | str:
    try:
        return read_codex_toml(config)
    except ValueError:
        return "invalid"


def codex_mcp_config_state(config: Path, vault_root: Path,
                           provider_bundle: dict[str, str] | None = None) -> str:
    """Classify the existing Codex MCP entry without modifying TOML."""
    if not config.exists():
        return "absent"
    return _document_mcp_state(_read_codex_document(config), vault_root, provider_bundle)


def _document_mcp_state(document: dict[str, Any] | str, vault_root: Path,
                        provider_bundle: dict[str, str] | None = None) -> str:
    if isinstance(document, str):
        return document
    entry = _mcp_entry(document)
    if isinstance(entry, str):
        return entry
    return _mcp_entry_state(entry, vault_root, provider_bundle)
