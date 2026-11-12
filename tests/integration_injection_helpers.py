"""Helpers shared by test_integration_injection.py and the test files importing them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


_ALIVE_SHELL = """alive() {
  local state
  kill -0 "$1" 2>/dev/null || return 1
  state="$(ps -o stat= -p "$1" 2>/dev/null)" || return 1
  case "$state" in *Z*) return 1 ;; esac
}
"""


def _ensure_scripts_on_path() -> None:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def _codex_mcp_toml(vault: Path, *, quoted: bool, conflicting: bool = False) -> str:
    table = '[mcp_servers."llm-wiki"]' if quoted else "[mcp_servers.llm-wiki]"
    _ensure_scripts_on_path()
    from integration_hook_config import codex_launch_args

    args = codex_launch_args(vault, "mcp_server.py", (), relative_target=True)
    if conflicting:
        args = ["python", "other.py"]
    return f'{table}\ncommand = "uv"\nargs = {json.dumps(args)}\n'
