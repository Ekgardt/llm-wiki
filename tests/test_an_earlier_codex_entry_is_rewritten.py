"""An earlier LLM-Wiki entry in Codex's config is rewritten, not left for a manual merge.

The installer classified every `[mcp_servers.llm-wiki]` entry that was not today's
exact form as a conflict and printed "Merge manually", including the entries its own
earlier releases wrote (`uv run --directory <vault> python scripts/mcp_server.py`,
before `--locked --no-sync`, or a vault in another directory). This product's own
shape is rewritten to this vault's, the operator's entry only when asked, and each
rewrite keeps a verified preimage. See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import codex_memory
import pytest

ROOT = Path(__file__).resolve().parent.parent
EARLIER = (
    '# my settings\nmodel = "gpt-5.6"\n\n'
    "[mcp_servers.llm-wiki]\n"
    'command = "uv"\n'
    'args = ["run", "--directory", "/srv/old-vault", "python", "scripts/mcp_server.py"]\n'
    "\n# the other server\n"
    "[mcp_servers.other]\n"
    'command = "other"\n'
)
FOREIGN = '[mcp_servers.llm-wiki]\ncommand = "node"\nargs = ["my-wiki.js"]\n'


def _config(tmp_path: Path, text: str, newline: str = "\n") -> Path:
    config = tmp_path / "config.toml"
    config.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return config


def _preimages(config: Path) -> list[bytes]:
    return [path.read_bytes() for path in config.parent.glob("config.toml.bak-llm-wiki-*")]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_an_entry_this_product_wrote_earlier_is_rewritten_with_its_preimage(tmp_path: Path, newline: str) -> None:
    config = _config(tmp_path, EARLIER, newline)
    original = config.read_bytes()
    assert codex_memory.codex_mcp_config_state(config, ROOT) == "stale"

    outcome = codex_memory.replace_codex_mcp_entry(config, ROOT, foreign=False)

    rewritten = config.read_text(encoding="utf-8")
    assert (outcome, codex_memory.codex_mcp_config_state(config, ROOT)) == ("replaced", "equivalent")
    _assert_the_rest_is_kept(rewritten)
    assert _preimages(config) == [original]


def _assert_the_rest_is_kept(rewritten: str) -> None:
    kept = (
        tomllib.loads(rewritten)["mcp_servers"]["other"],
        "# my settings" in rewritten,
        "# the other server" in rewritten,
    )
    assert kept == ({"command": "other"}, True, True)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_the_operators_entry_is_replaced_only_when_asked(tmp_path: Path, newline: str) -> None:
    config = _config(tmp_path, FOREIGN, newline)
    original = config.read_bytes()

    refused = codex_memory.replace_codex_mcp_entry(config, ROOT, foreign=False)
    unchanged = config.read_text(encoding="utf-8")
    replaced = codex_memory.replace_codex_mcp_entry(config, ROOT, foreign=True)

    assert (refused, unchanged, replaced) == ("refused-foreign", FOREIGN, "replaced")
    assert _preimages(config) == [original]


def test_an_inline_entry_is_not_rewritten(tmp_path: Path) -> None:
    inline = 'mcp_servers = { llm-wiki = { command = "uv", args = ["run"] } }\n'
    config = _config(tmp_path, inline)

    outcome = codex_memory.replace_codex_mcp_entry(config, ROOT, foreign=True)

    assert (outcome, config.read_text(encoding="utf-8")) == ("not-rewritable", inline)


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", source)
    assert match, f"{name} missing"
    return match.group(0)


def test_the_installer_rewrites_an_earlier_entry_and_reports_it_verified(tmp_path: Path) -> None:
    source = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = "\n".join(
        _shell_function(source, name)
        for name in ("codex_mcp_state_status", "replace_codex_mcp", "configure_codex_mcp")
    )
    config = _config(tmp_path, EARLIER)
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "set -euo pipefail\n"
        + functions
        + '\nuv() {\n  while [[ $# -gt 0 && $1 != config-state && $1 != config-replace ]]; do shift; done\n'
        + '  "$TEST_PYTHON" "$TEST_VAULT/scripts/codex_memory.py" "$@"\n}\n'
        + 'configure_codex_mcp "$TEST_VAULT" "$TEST_CONFIG"\n',
        encoding="utf-8", newline="\n",
    )
    env = {**os.environ, "TEST_VAULT": ROOT.as_posix(), "TEST_PYTHON": Path(sys.executable).as_posix(), "TEST_CONFIG": config.as_posix()}

    result = subprocess.run(["bash", "-x", str(runner)], env=env, capture_output=True, text=True, timeout=60, check=False)

    assert (result.returncode, codex_memory.codex_mcp_config_state(config, ROOT)) == (0, "equivalent"), {"stdout": result.stdout, "stderr": result.stderr}
    assert tomllib.loads(config.read_text(encoding="utf-8"))["model"] == "gpt-5.6"


def test_doctor_names_the_one_step_codex_trust_needs(tmp_path: Path) -> None:
    import doctor

    result = doctor._codex_degraded_result(tmp_path, tmp_path, "runtime_hooks_untrusted")

    assert result["message"].startswith("Codex has not been told to trust the LLM-Wiki hooks: ")
    assert "run /hooks" in result["message"]


def test_no_advice_asks_for_a_manual_merge() -> None:
    advice = " ".join(codex_memory.CODEX_MCP_ADVICE.values()).lower()
    assert ("merge manually" in advice, "--replace-codex-mcp" in advice) == (False, True)
