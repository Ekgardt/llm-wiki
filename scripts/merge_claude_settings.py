"""Merge LLM-wiki Claude Code hooks into user settings.json safely.

Does NOT wipe the user's existing hooks/permissions. Strategy:
  - Backup existing settings to settings.json.bak-llm-wiki-<timestamp>
  - For each hook event we own: drop prior entries whose command mentions
    our scripts, then append the template entries
  - Union permissions.allow / permissions.deny
  - Ensure env.LLM_WIKI_ROOT / LLM_WIKI_STATE_ROOT are set
  - Write merged JSON with trailing newline

Usage:
    uv run python scripts/merge_claude_settings.py
    uv run python scripts/merge_claude_settings.py --user-settings PATH --template PATH
    uv run python scripts/merge_claude_settings.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from integration_config_backup import publish_configuration

# Allow entries we shipped before 2026-09-10 that granted writes or execution
# under a read-only-looking name; the merge retires exactly these strings.
# Research: docs/research/2026-09-10-an-allowlist-that-reads-as-read-only-must-be-read-only.md
RETIRED_ALLOW = frozenset(
    {"Bash(sed *)", "Bash(xargs *)", "Bash(sort *)", "Bash(uv run --directory *)"}
)

OUR_SCRIPT_MARKERS = (
    "integration_adapter.py",
    "session_start_context.py",
    "precompact_capture.py",
    "session_end_capture.py",
    "user_prompt_capture.py",
    "post_tool_capture.py",
    "session_start_project_state.py",
    "session_end_project_tag.py",
)


def _default_template() -> Path:
    return Path(__file__).resolve().parent.parent / "integrations" / "claude-code" / "settings.json"


def _default_user_settings() -> Path:
    home = Path.home()
    return home / ".claude" / "settings.json"


def _load_json(path: Path, *, label: str, missing_ok: bool = False) -> dict:
    if not path.exists():
        if missing_ok:
            return {}
        raise ValueError(f"{label} is missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{label} could not be read: {path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a JSON object: {path}")
    return data


def _command_is_ours(command: str) -> bool:
    c = command or ""
    return any(m in c for m in OUR_SCRIPT_MARKERS)


def _strip_our_hooks(blocks: list) -> list:
    """Remove matcher-blocks that only (or partly) contain our commands."""
    out: list = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        hooks = block.get("hooks")
        if not isinstance(hooks, list):
            out.append(block)
            continue
        kept = [
            h
            for h in hooks
            if isinstance(h, dict) and not _command_is_ours(str(h.get("command") or ""))
        ]
        if kept:
            new_block = dict(block)
            new_block["hooks"] = kept
            out.append(new_block)
        # If all hooks were ours, drop the whole matcher block.
    return out


def merged_permission_list(key: str, existing: list, incoming: list) -> list[str]:
    """The user's entries first, ours appended once, our retired grants gone."""
    retired = RETIRED_ALLOW if key == "allow" else frozenset()
    merged: list[str] = []
    for item in list(existing) + list(incoming):
        text = str(item)
        if text not in merged and text not in retired:
            merged.append(text)
    return merged


def _dict_at(result: dict, key: str) -> dict:
    """The user's mapping under `key`, replaced by an empty one when it is not a dict."""
    value = result.setdefault(key, {})
    if isinstance(value, dict):
        return value
    result[key] = {}
    return result[key]


def _list_or_empty(mapping: dict, key: str) -> list:
    value = mapping.get(key)
    return list(value) if isinstance(value, list) else []


def _merge_defaults(result: dict, template: dict) -> None:
    """Schema and flag from the template only where the user set nothing."""
    if "$schema" in template and "$schema" not in result:
        result["$schema"] = template["$schema"]
    if template.get("autoMemoryEnabled") is not None and "autoMemoryEnabled" not in result:
        result["autoMemoryEnabled"] = template["autoMemoryEnabled"]


def _merge_permissions(result: dict, template: dict) -> None:
    incoming = template.get("permissions") or {}
    permissions = _dict_at(result, "permissions")
    for key in ("allow", "deny"):
        merged = merged_permission_list(
            key, _list_or_empty(permissions, key), _list_or_empty(incoming, key)
        )
        if merged:
            permissions[key] = merged


def _merge_hooks(result: dict, template: dict) -> None:
    """Per event: strip our blocks, then append the template's."""
    incoming = template.get("hooks") or {}
    hooks = _dict_at(result, "hooks")
    if not isinstance(incoming, dict):
        return
    for event, blocks in incoming.items():
        if not isinstance(blocks, list):
            continue
        hooks[event] = _strip_our_hooks(_list_or_empty(hooks, event)) + list(blocks)


def _merge_env(result: dict, vault_root: str, state_root: str) -> None:
    env = _dict_at(result, "env")
    if vault_root:
        env["LLM_WIKI_ROOT"] = vault_root
    if state_root:
        env["LLM_WIKI_STATE_ROOT"] = state_root


def merge_settings(user: dict, template: dict, vault_root: str, state_root: str) -> dict:
    """Return a new merged settings dict; the user's values are never clobbered."""
    result = json.loads(json.dumps(user))  # deep copy via JSON
    _merge_defaults(result, template)
    _merge_permissions(result, template)
    _merge_hooks(result, template)
    _merge_env(result, vault_root, state_root)
    return result


def apply_merge(
    user_settings: Path,
    template: Path,
    vault_root: str,
    state_root: str,
    dry_run: bool = False,
) -> dict:
    user = _load_json(user_settings, label="user settings", missing_ok=True)
    tmpl = _load_json(template, label="template")
    if not tmpl:
        raise ValueError(f"template is empty: {template}")

    merged = merge_settings(user, tmpl, vault_root, state_root)
    text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"

    if dry_run:
        print(text)
        return merged

    changed, bak = publish_configuration(user_settings, text.encode("utf-8"))
    if bak is not None:
        print(f"merge_claude_settings: backup → {bak}")
    if changed:
        print(f"merge_claude_settings: wrote {user_settings}")
    return merged


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user-settings", type=Path, default=None)
    p.add_argument("--template", type=Path, default=None)
    p.add_argument(
        "--vault-root",
        default=os.environ.get("LLM_WIKI_ROOT", ""),
        help="Value for env.LLM_WIKI_ROOT (default: $LLM_WIKI_ROOT)",
    )
    p.add_argument(
        "--state-root",
        default=os.environ.get("LLM_WIKI_STATE_ROOT", ""),
        help="Value for env.LLM_WIKI_STATE_ROOT",
    )
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    template = args.template or _default_template()
    user_settings = args.user_settings or _default_user_settings()
    vault = args.vault_root or str(Path(__file__).resolve().parent.parent)
    state = args.state_root
    if not state:
        state = str(Path(vault).resolve())

    apply_merge(
        user_settings=user_settings,
        template=template,
        vault_root=str(Path(vault).resolve()),
        state_root=str(Path(state).resolve()),
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
