# V4 Reliability Installer And CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make installation and CI fail closed, reproducible, additive, cross-platform, and truthful while preserving the 12-tool MCP surface, the three-zone layout, the local no-daemon model, and version `4.0.0`.

**Architecture:** Build on the production dependency profile, `scripts/install_smoke.py`, and initial CI hardening from `2026-08-05-v4-reliability-platform.md`; do not create parallel implementations. Keep Bash and Windows PowerShell as thin orchestration layers, put JSONC/OpenCode resolution in one focused Python module, and expose reliability-core installed-vault migration through one explicit check-by-default repair CLI. CI extends the shared immutable pins and clean environments with native installer coverage and retained timing evidence.

**Tech Stack:** Bash, Windows PowerShell 5.1-compatible PowerShell, Python 3.10-3.14 standard library, uv 0.12.1, pytest, Git, GitHub Actions, OpenCode 1.18.13, MCP stdio.

---

## Scope And Safety

**Prerequisites:** Complete and verify `docs/superpowers/plans/2026-08-05-v4-reliability-platform.md` first. This plan assumes its production MCP baseline, empty `mcp-server` compatibility alias, direct development dependencies, `scripts/install_smoke.py`, `tests/test_install_smoke.py`, preserved action majors, explicit runners, and clean CI lanes. Task 6 additionally waits for the queue plan's `scripts/installed_memory_repair.py` backend; the queue plan explicitly reserves the thin CLI for this plan. When plans touch one file, start from the prerequisite result and add only the behavior specified here. Do not use this plan's optional checkpoint commits while prerequisite changes remain uncommitted; use one separately reviewed combined commit only if the operator later requests it.

This plan closes the following approved findings:

| Finding | Tasks |
|---|---|
| I1 remote bootstrap and Git safety | 2 |
| I2 mandatory installer failures and bounded smoke | 5 |
| I3 XDG, OpenCode precedence, roots, and scheduler environment | 1, 3 |
| I4 locked additive environments and unattended uv calls | 4 |
| I5 worktree cleanup scope | 7 |
| I6 installed-vault repair CLI packaging | 6 |
| CI1 immutable CI inputs and measured timeouts | 9 |
| CI2 clean production lanes | 10 |
| ML1 no mutable/default reranker | 8 |
| D1 truthful synchronized public documentation | 11 |

Do not modify these protected pre-existing paths while executing this plan; several already contain unrelated worktree changes:

```text
.gitignore
AGENTS.md
CLAUDE.md
docs/STRUCTURE.md
knowledge/index.md
knowledge/log.md
docs/superpowers/specs/2026-08-05-v4-reliability-repair-design.md
docs/superpowers/plans/2026-08-05-v4-reliability-platform.md
docs/superpowers/plans/2026-08-05-v4-reliability-queue.md
knowledge/notes/v4-reliability-contracts-decision.md
```

Do not run any memory compile or flush command against this public source worktree. Do not bump `pyproject.toml` beyond `4.0.0`, create a release, create a tag, or publish a green badge. Do not commit unless the operator explicitly requests a commit. Each task includes an optional checkpoint command only for that explicit case.

Every behavior task follows the same gate:

1. Add a focused behavioral test.
2. Run that test and observe the named failure.
3. Add the smallest production change.
4. Run the focused test and observe a pass.
5. Refactor without changing behavior.
6. Run the focused suite again.

## Current-Source Basis

The implementation must cite and follow these sources in code comments or nearby documentation where the contract is not self-evident:

| Subject | Current source used by this plan |
|---|---|
| uv 0.12.1 install | <https://docs.astral.sh/uv/getting-started/installation/> |
| uv locked, inexact, no-default-groups, and no-sync behavior | <https://docs.astral.sh/uv/concepts/projects/sync/> |
| uv installer controls | <https://docs.astral.sh/uv/reference/installer/> |
| OpenCode JSON/JSONC and precedence | <https://opencode.ai/docs/config/> |
| OpenCode 1.18.13 loader behavior | <https://raw.githubusercontent.com/anomalyco/opencode/v1.18.13/packages/opencode/src/config/config.ts> |
| XDG absolute-path rule | <https://specifications.freedesktop.org/basedir-spec/latest/> |
| Git clone behavior | <https://git-scm.com/docs/git-clone> |
| Git worktree porcelain `-z` and primary ordering | <https://git-scm.com/docs/git-worktree> |
| PowerShell native exit status | <https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_automatic_variables> |
| GitHub full-SHA action hardening | <https://docs.github.com/en/actions/how-tos/security-for-github-actions/security-guides/security-hardening-for-github-actions> |
| Explicit hosted-runner labels | <https://docs.github.com/en/actions/reference/runners/github-hosted-runners> |

Preserve the action majors already qualified by the platform plan. Use these verified immutable revisions, with the release name in an end-of-line comment for Dependabot. `upload-artifact` is the only newly introduced action:

```yaml
actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0
astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86 # v5.4.2
gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e # v3.0.0
actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
```

Use `ubuntu-24.04`, `windows-2025`, and `macos-15`. Do not use a `-latest` runner label.

## File Map

| Path | Responsibility |
|---|---|
| `scripts/installer_config.py` | XDG resolution, JSONC parsing, user-level OpenCode merge, effective-entry comparison, and POSIX profile block updates |
| `scripts/install_smoke.py` | Existing 120-second production smoke, extended with the exact 12-name contract |
| `scripts/run-scheduled-task.ps1` | Exact root/state export and locked no-sync scheduled invocation on Windows |
| `scripts/repair_installed_memory.py` | Check-by-default CLI over reliability-core installed-vault validators |
| `scripts/ci_timing_report.py` | Deterministic CI job and per-test timing evidence compiler |
| `benchmark/ci-timeout-evidence-v1.schema.json` | Closed schema for retained timeout evidence |
| `benchmark/ci-timeout-evidence-2026-08-05.json` | Generated evidence for the selected CI ceilings |
| `tests/test_installer_config.py` | JSONC, XDG, precedence, profile, and idempotency behavior |
| `tests/test_installer_bootstrap.py` | Isolated Bash and PowerShell bootstrap, OID, CWD, remote, and native-exit behavior |
| `tests/test_install_smoke.py` | Existing aggregate deadline, import, Doctor, MCP, cleanup, and failure behavior |
| `tests/test_cleanup_worktrees.py` | NUL metadata parsing, primary detection, containment, and prune separation |
| `tests/test_repair_installed_memory.py` | Read-only default, explicit apply, offline adoption gate, and non-destructive behavior |
| `tests/test_dependency_contract.py` | Prerequisite production/development ownership contract that must remain green |
| `tests/test_dependency_environments.py` | Direct dependency ownership and unattended uv command contracts |
| `tests/test_ci_policy.py` | Action, tool, runner, timeout, and clean-lane workflow policy |
| `tests/test_ci_timing_report.py` | Timing report parsing, p95 calculation, and schema behavior |
| `install.sh`, `install.ps1` | Thin installer orchestration and final status |
| `scripts/install-scheduled-tasks.ps1` | Task Scheduler registration using the exact root/state runner |
| `scripts/cleanup_worktrees.py` | Scoped linked-worktree cleanup |
| `scripts/reranker.py` | Explicit immutable local-only reranker selection |
| `pyproject.toml`, `uv.lock` | Add missing direct reranker imports without reversing platform-plan ownership |
| `.github/workflows/tests.yml` | Pinned, measured, clean cross-platform CI |
| `integrations/claude-code/settings.json` | Locked no-sync Claude lifecycle commands |
| `integrations/codex/hooks.json` | Locked no-sync Codex lifecycle commands |
| `scripts/llm-wiki-memory-opencode.js` | Locked no-sync OpenCode lifecycle command |
| `scripts/codex-memory-wrapper.ps1` | Locked no-sync compatibility helper commands |
| `scripts/codex_memory.py` | Exact Codex MCP command equivalence |
| `scripts/merge_claude_settings.py` | Repeated installer ownership including `integration_adapter.py` |
| `scripts/doctor.py` | Exact installed MCP command diagnostics |
| `scripts/mcp_server.py` | Correct locked no-sync configuration example |
| `tests/test_integration_injection.py`, `tests/test_doctor.py`, `tests/test_sync_memory.py`, `tests/test_quality_guards.py` | Existing integration coverage; Tasks 3-5 and 11 replace named source-string checks with parsed-config or subprocess assertions |
| `README.md`, `README.ru.md`, `README.zh-CN.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `docs/USER-GUIDE.md`, `integrations/README.md` | Synchronized, non-release documentation |

### Task 1: Build Shared Installer Configuration Primitives

**Files:**
- Create: `scripts/installer_config.py`
- Create: `tests/test_installer_config.py`

- [ ] **Step 1: Write failing XDG, JSONC, profile, and effective-entry tests**

Add tests with these exact behavioral cases:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from installer_config import (
    PROFILE_END,
    PROFILE_START,
    expected_opencode_entry,
    merge_opencode_user_config,
    opencode_global_dir,
    parse_jsonc,
    replace_profile_block,
    verify_effective_entry,
)


@pytest.mark.parametrize("xdg", [None, "", "relative/config"])
def test_posix_xdg_unset_empty_or_relative_falls_back(tmp_path: Path, xdg: str | None) -> None:
    assert opencode_global_dir(tmp_path, xdg, platform="posix") == tmp_path / ".config" / "opencode"


def test_posix_absolute_xdg_is_used(tmp_path: Path) -> None:
    target = tmp_path / "xdg"
    assert opencode_global_dir(tmp_path, str(target), platform="posix") == target / "opencode"


def test_jsonc_merge_preserves_unrelated_values_and_is_idempotent(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    config = config_dir / "opencode.jsonc"
    config.write_text(
        '{\n  // retained by the backup\n  "model": "local/model",\n  "mcp": {"other": {"enabled": true,},},\n}\n',
        encoding="utf-8",
    )
    expected = expected_opencode_entry(tmp_path / "vault")

    first = merge_opencode_user_config(config_dir, expected)
    first_bytes = config.read_bytes()
    second = merge_opencode_user_config(config_dir, expected)

    assert first.changed is True
    assert second.changed is False
    assert config.read_bytes() == first_bytes
    assert parse_jsonc(config.read_text(encoding="utf-8"))["model"] == "local/model"
    assert parse_jsonc(config.read_text(encoding="utf-8"))["mcp"]["other"]["enabled"] is True
    assert parse_jsonc(config.read_text(encoding="utf-8"))["mcp"]["llm-wiki"] == expected
    assert first.backup is not None and first.backup.read_bytes().startswith(b"{\n  //")
    assert second.backup is None


def test_effective_entry_distinguishes_active_override_and_unverified(tmp_path: Path) -> None:
    expected = expected_opencode_entry(tmp_path / "vault")
    assert verify_effective_entry({"mcp": {"llm-wiki": expected}}, expected) == "active"
    assert verify_effective_entry(
        {"mcp": {"llm-wiki": {**expected, "enabled": False}}}, expected
    ) == "conflict"
    assert verify_effective_entry({}, expected) == "conflict"


def test_profile_rewrites_only_owned_block_and_keeps_custom_state(tmp_path: Path) -> None:
    profile = tmp_path / ".bashrc"
    profile.write_text("export USER_SETTING=yes\n", encoding="utf-8")
    replace_profile_block(profile, Path("/vault one"), Path("/state two"))
    first = profile.read_text(encoding="utf-8")
    replace_profile_block(profile, Path("/vault one"), Path("/state two"))
    assert profile.read_text(encoding="utf-8") == first
    assert first.count(PROFILE_START) == first.count(PROFILE_END) == 1
    assert "export USER_SETTING=yes" in first
    assert "LLM_WIKI_ROOT='/vault one'" in first
    assert "LLM_WIKI_STATE_ROOT='/state two'" in first
```

Also cover these cases in the same file: malformed JSONC causes no write; a symlinked selected config is rejected; existing `opencode.jsonc`, then `opencode.json`, then `config.json` is the selection order; a hash-named backup is create-only; duplicate or unbalanced profile markers fail without a write; `OPENCODE_CONFIG`, project config, `OPENCODE_CONFIG_DIR`, inline content, and managed output can each produce an effective conflict; an unrelated higher-precedence layer leaves the entry active; a hung or oversized `opencode debug config` result becomes `configured_unverified` after bounded cleanup and never active.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_installer_config.py -q
```

Expected: collection fails because `installer_config` does not exist.

- [ ] **Step 3: Implement a bounded stdlib JSONC parser and exact entry builder**

Create `scripts/installer_config.py`. Use a state machine, not a regular expression, so comment markers inside strings remain data. The core must have these interfaces and limits:

```python
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_DEBUG_BYTES = 4 * 1024 * 1024
DEBUG_TIMEOUT_SECONDS = 15.0
PROFILE_START = "# >>> LLM-Wiki installer >>>"
PROFILE_END = "# <<< LLM-Wiki installer <<<"
GLOBAL_CONFIG_NAMES = ("opencode.jsonc", "opencode.json", "config.json")


def _without_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        current = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if quoted:
            output.append(current)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                quoted = False
            index += 1
            continue
        if current == '"':
            quoted = True
            output.append(current)
            index += 1
            continue
        if current == "/" and following == "/":
            output.extend((" ", " "))
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if current == "/" and following == "*":
            output.extend((" ", " "))
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                output.append(text[index] if text[index] in "\r\n" else " ")
                index += 1
            if index + 1 >= len(text):
                raise ValueError("unterminated JSONC block comment")
            output.extend((" ", " "))
            index += 2
            continue
        output.append(current)
        index += 1
    if quoted:
        raise ValueError("unterminated JSON string")
    return "".join(output)


def _without_trailing_commas(text: str) -> str:
    output: list[str] = []
    quoted = False
    escaped = False
    for index, current in enumerate(text):
        if quoted:
            output.append(current)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                quoted = False
            continue
        if current == '"':
            quoted = True
            output.append(current)
            continue
        if current == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                continue
        output.append(current)
    return "".join(output)


def parse_jsonc(text: str) -> dict[str, Any]:
    value = json.loads(_without_trailing_commas(_without_comments(text.lstrip("\ufeff"))))
    if not isinstance(value, dict):
        raise ValueError("OpenCode config root must be an object")
    return value


def expected_opencode_entry(vault_root: Path) -> dict[str, Any]:
    return {
        "type": "local",
        "command": [
            "uv",
            "run",
            "--locked",
            "--no-sync",
            "--directory",
            str(vault_root),
            "python",
            "scripts/mcp_server.py",
        ],
        "enabled": True,
    }
```

Normalize the selected file to deterministic UTF-8 JSON after parsing. Hash the exact pre-edit bytes and use `config_path.with_name(f"{config_path.name}.llm-wiki.{digest}.bak")`, where `digest = hashlib.sha256(original_bytes).hexdigest()`. Create that backup with mode `xb`; if it already exists, require its bytes to equal `original_bytes` or fail without touching the config. Write the normalized bytes to a same-directory temporary file, flush and `os.fsync()` it, then publish with `os.replace()`. This is an explicit trade-off: the selected JSONC file may lose comments, but no source bytes are lost and reruns create no additional backup.

- [ ] **Step 4: Implement XDG, global-source selection, profile ownership, and effective comparison**

Use these exact rules:

```python
def opencode_global_dir(home: Path, xdg: str | None, *, platform: str) -> Path:
    if platform == "posix" and xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        base = home / ".config"
    return base / "opencode"


def selected_global_file(config_dir: Path) -> Path:
    for name in GLOBAL_CONFIG_NAMES:
        candidate = config_dir / name
        if candidate.exists():
            return candidate
    return config_dir / "opencode.jsonc"


def verify_effective_entry(config: Mapping[str, Any], expected: Mapping[str, Any]) -> str:
    mcp = config.get("mcp")
    actual = mcp.get("llm-wiki") if isinstance(mcp, Mapping) else None
    return "active" if actual == expected else "conflict"


def replace_profile_block(profile: Path, root: Path, state: Path) -> None:
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    start_count = existing.count(PROFILE_START)
    end_count = existing.count(PROFILE_END)
    if start_count != end_count or start_count > 1:
        raise ValueError("invalid LLM-Wiki profile block ownership")
    block = "\n".join(
        (
            PROFILE_START,
            f"export LLM_WIKI_ROOT={shlex.quote(str(root))}",
            f"export LLM_WIKI_STATE_ROOT={shlex.quote(str(state))}",
            PROFILE_END,
        )
    )
    if PROFILE_START in existing:
        start = existing.index(PROFILE_START)
        end = existing.index(PROFILE_END, start) + len(PROFILE_END)
        updated = existing[:start] + block + existing[end:]
    else:
        separator = "" if not existing else "\n" if existing.endswith("\n") else "\n\n"
        updated = existing + separator + block + "\n"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(updated, encoding="utf-8", newline="\n")
```

The `opencode` CLI subcommand must write only the selected global source, copy the plugin only to the resolved global `plugins/` directory, then run `opencode debug config` from the original caller directory. Enforce `DEBUG_TIMEOUT_SECONDS`, retain at most `MAX_DEBUG_BYTES`, and terminate the owned child on timeout or overflow. Return one JSON object with `config_file`, `plugin_file`, and status `active`, `conflict`, `configured_unverified`, or `not_detected`. A conflict, timeout, oversized output, or unavailable CLI must never emit an active status.

- [ ] **Step 5: Run RED-to-GREEN verification and refactor**

Run:

```bash
uv run --locked --no-sync pytest tests/test_installer_config.py -q
uv run --locked --no-sync ruff check scripts/installer_config.py tests/test_installer_config.py
```

Expected: both commands pass; repeated merges are byte-identical.

- [ ] **Step 6: Optional checkpoint only after explicit operator approval**

```bash
git add scripts/installer_config.py tests/test_installer_config.py
git commit -m "fix: centralize installer config resolution"
```

### Task 2: Make Bootstrap And Git Protection Exact

**Files:**
- Create: `tests/test_installer_bootstrap.py`
- Modify: `install.sh`
- Modify: `install.ps1`

- [ ] **Step 1: Write isolated pipe-mode and existing-checkout tests**

Use a temporary local bare repository containing `pyproject.toml`, `uv.lock`, `install.sh`, `install.ps1`, and `scripts/installer_config.py`. Drive extracted bootstrap functions through Bash and PowerShell so no network or real home directory is used.

The tests must prove:

```python
@pytest.mark.parametrize(
    "value",
    [None, "", "main", "v4.0.0", "abc123", "g" * 40, "a" * 39, "a" * 41],
)
def test_remote_bootstrap_rejects_non_full_oid(value: str | None) -> None:
    """Remote bootstrap exits nonzero before clone unless OID is exactly 40 hex."""


def test_pipe_mode_ignores_caller_checkout_and_verifies_exact_head() -> None:
    """A pyproject in caller CWD is never selected as the source checkout."""


def test_remote_bootstrap_rejects_wrong_head_or_missing_required_file() -> None:
    """Repository identity, exact HEAD, and required files are all mandatory."""


def test_installer_created_clone_disables_every_remote_push_url() -> None:
    """Every remote gets exactly one push URL, no-push, while fetch URLs stay unchanged."""


def test_existing_checkout_keeps_all_remote_urls_without_explicit_option() -> None:
    """A normal rerun has zero Git config mutation."""


def test_existing_checkout_protects_all_remotes_when_explicitly_requested() -> None:
    """--protect-push is opt-in for a checkout the installer did not create."""
```

Run both shells when available. Skip only the shell absent from the host; CI Task 10 makes each native platform mandatory.

- [ ] **Step 2: Run the bootstrap tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_installer_bootstrap.py -q
```

Expected: tests fail because pipe mode can use caller CWD, tags are accepted implicitly, and existing remotes are rewritten.

- [ ] **Step 3: Implement Bash bootstrap detection, exact fetch, and verified re-exec**

Add `--protect-push`, preserve the initial directory, and distinguish a script file from stdin:

```bash
REPOSITORY_URL="https://github.com/Ekgardt/llm-wiki.git"
CALLER_CWD="$(pwd -P)"
INSTALLER_CREATED_CLONE="${LLM_WIKI_INSTALLER_CREATED_CLONE:-0}"
PROTECT_PUSH=0

for argument in "$@"; do
  case "$argument" in
    --protect-push) PROTECT_PUSH=1 ;;
    *) fail "Unknown installer argument: $argument" ;;
  esac
done

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
else
  SCRIPT_DIR=""
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  VAULT_ROOT="${LLM_WIKI_ROOT:-$SCRIPT_DIR}"
else
  [[ "${LLM_WIKI_COMMIT:-}" =~ ^[0-9a-fA-F]{40}$ ]] || \
    fail "Remote bootstrap requires LLM_WIKI_COMMIT as a full 40-hex commit OID"
  LLM_WIKI_COMMIT_NORMALIZED="$(printf '%s' "$LLM_WIKI_COMMIT" | tr 'ABCDEF' 'abcdef')"
  INSTALL_DIR="$HOME/LLM-wiki"
  [[ ! -e "$INSTALL_DIR" ]] || fail "Remote install target already exists: $INSTALL_DIR"
  git init "$INSTALL_DIR"
  git -C "$INSTALL_DIR" remote add origin "$REPOSITORY_URL"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$LLM_WIKI_COMMIT_NORMALIZED"
  git -C "$INSTALL_DIR" checkout --detach "$LLM_WIKI_COMMIT_NORMALIZED"
  VAULT_ROOT="$(cd "$INSTALL_DIR" && pwd -P)"
  INSTALLER_CREATED_CLONE=1
  [[ "$(git -C "$VAULT_ROOT" rev-parse HEAD)" == "$LLM_WIKI_COMMIT_NORMALIZED" ]] || \
    fail "Checked-out commit does not match LLM_WIKI_COMMIT"
  [[ "$(git -C "$VAULT_ROOT" remote get-url origin)" == "$REPOSITORY_URL" ]] || \
    fail "Installed checkout repository identity does not match LLM-Wiki"
  for required in pyproject.toml uv.lock install.sh install.ps1 scripts/installer_config.py; do
    [[ -f "$VAULT_ROOT/$required" ]] || fail "Installed checkout is missing $required"
  done
  export LLM_WIKI_ROOT="$VAULT_ROOT"
  export LLM_WIKI_INSTALLER_CREATED_CLONE=1
  exec bash "$VAULT_ROOT/install.sh" "$@"
fi
```

The checked-out installer is always the one that performs installation. Pipe code does only clone, verification, and re-exec. Do not add a caller-settable re-exec flag: the checked-out script naturally takes the local-file branch, so no recursion guard is needed.

- [ ] **Step 4: Implement equivalent Windows PowerShell bootstrap**

Add a top-level parameter block and exact OID check compatible with Windows PowerShell 5.1:

```powershell
[CmdletBinding()]
param(
    [switch]$ProtectPush
)

function Invoke-NativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int[]]$AllowedExitCodes = @(0),
        [switch]$CaptureOutput,
        [switch]$ReturnResult
    )
    if ($CaptureOutput) {
        $output = @(& $FilePath @ArgumentList)
    } else {
        & $FilePath @ArgumentList
    }
    $nativeExit = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $nativeExit) {
        throw "$FilePath failed with exit code $nativeExit"
    }
    if ($ReturnResult) {
        return [pscustomobject]@{
            ExitCode = $nativeExit
            Output = if ($CaptureOutput) { $output -join [Environment]::NewLine } else { $null }
        }
    }
    if ($CaptureOutput) { return ($output -join [Environment]::NewLine) }
}

$repositoryUrl = "https://github.com/Ekgardt/llm-wiki.git"
$callerDirectory = (Get-Location).Path
$installerCreatedClone = $env:LLM_WIKI_INSTALLER_CREATED_CLONE -eq "1"
$scriptDirectory = if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) { $null } else { $PSScriptRoot }

if ($scriptDirectory -and (Test-Path -LiteralPath (Join-Path $scriptDirectory "pyproject.toml"))) {
    $VAULT_ROOT = if ($env:LLM_WIKI_ROOT) { $env:LLM_WIKI_ROOT } else { $scriptDirectory }
} else {
    if ($env:LLM_WIKI_COMMIT -notmatch '^[0-9a-fA-F]{40}$') {
        Fail "Remote bootstrap requires LLM_WIKI_COMMIT as a full 40-hex commit OID"
    }
    $commit = $env:LLM_WIKI_COMMIT.ToLowerInvariant()
    $VAULT_ROOT = Join-Path $env:USERPROFILE "LLM-wiki"
    if (Test-Path -LiteralPath $VAULT_ROOT) { Fail "Remote install target already exists: $VAULT_ROOT" }
    Invoke-NativeCommand git @("init", $VAULT_ROOT)
    Invoke-NativeCommand git @("-C", $VAULT_ROOT, "remote", "add", "origin", $repositoryUrl)
    Invoke-NativeCommand git @("-C", $VAULT_ROOT, "fetch", "--depth", "1", "origin", $commit)
    Invoke-NativeCommand git @("-C", $VAULT_ROOT, "checkout", "--detach", $commit)
    $installerCreatedClone = $true
    $head = (Invoke-NativeCommand git @("-C", $VAULT_ROOT, "rev-parse", "HEAD") -CaptureOutput).Trim()
    if ($head -ne $commit) { Fail "Checked-out commit does not match LLM_WIKI_COMMIT" }
    $origin = (Invoke-NativeCommand git @("-C", $VAULT_ROOT, "remote", "get-url", "origin") -CaptureOutput).Trim()
    if ($origin -ne $repositoryUrl) { Fail "Installed checkout repository identity does not match LLM-Wiki" }
    foreach ($required in @("pyproject.toml", "uv.lock", "install.sh", "install.ps1", "scripts\installer_config.py")) {
        if (-not (Test-Path -LiteralPath (Join-Path $VAULT_ROOT $required) -PathType Leaf)) {
            Fail "Installed checkout is missing $required"
        }
    }
    $env:LLM_WIKI_ROOT = $VAULT_ROOT
    $env:LLM_WIKI_INSTALLER_CREATED_CLONE = "1"
    $hostExecutable = if ($PSVersionTable.PSEdition -eq "Core") {
        Join-Path $PSHOME "pwsh.exe"
    } else {
        Join-Path $PSHOME "powershell.exe"
    }
    $reexecArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Join-Path $VAULT_ROOT "install.ps1")
    )
    if ($ProtectPush) { $reexecArguments += "-ProtectPush" }
    try {
        & $hostExecutable @reexecArguments
        $nativeExit = $LASTEXITCODE
    } finally {
        Remove-Item Env:LLM_WIKI_INSTALLER_CREATED_CLONE -ErrorAction SilentlyContinue
    }
    if ($nativeExit -ne 0) {
        throw "Checked-out installer failed with exit code $nativeExit"
    }
    return
}
```

Do not move re-exec ahead of these repository and required-file checks.

- [ ] **Step 5: Protect push URLs only when authorized**

Use a function in each installer that enumerates every configured remote, clears every explicit push URL, sets exactly one `no-push`, then verifies `git remote get-url --all --push "$remote"` returns only `no-push`. Call it when `LLM_WIKI_INSTALLER_CREATED_CLONE=1` or `--protect-push` was supplied. Never alter a fetch URL.

Bash core:

```bash
protect_push_urls() {
  local remote remotes status urls
  remotes="$(git -C "$VAULT_ROOT" remote)" || fail "Could not enumerate Git remotes"
  while IFS= read -r remote; do
    [[ -n "$remote" ]] || continue
    if git -C "$VAULT_ROOT" config --get-all "remote.$remote.pushurl" >/dev/null 2>&1; then
      git -C "$VAULT_ROOT" config --unset-all "remote.$remote.pushurl" || \
        fail "Could not clear push URLs for remote $remote"
    else
      status=$?
      [[ "$status" -eq 1 ]] || fail "Could not inspect push URLs for remote $remote"
    fi
    git -C "$VAULT_ROOT" config --add "remote.$remote.pushurl" no-push || \
      fail "Could not protect push URLs for remote $remote"
    urls="$(git -C "$VAULT_ROOT" remote get-url --all --push "$remote")" || \
      fail "Could not verify push URLs for remote $remote"
    [[ "$urls" == "no-push" ]] || fail "Could not protect push URLs for remote $remote"
  done <<< "$remotes"
}
```

PowerShell uses the same helper and exact branching:

```powershell
function Protect-PushUrls([string]$VaultRoot) {
    $remoteResult = Invoke-NativeCommand git @("-C", $VaultRoot, "remote") -CaptureOutput -ReturnResult
    $remotes = @($remoteResult.Output -split "`r?`n" | Where-Object { $_ })
    foreach ($remote in $remotes) {
        $key = "remote.$remote.pushurl"
        $probe = Invoke-NativeCommand git @("-C", $VaultRoot, "config", "--get-all", $key) `
            -AllowedExitCodes @(0, 1) -CaptureOutput -ReturnResult
        if ($probe.ExitCode -eq 0) {
            Invoke-NativeCommand git @("-C", $VaultRoot, "config", "--unset-all", $key)
        }
        Invoke-NativeCommand git @("-C", $VaultRoot, "config", "--add", $key, "no-push")
        $verify = Invoke-NativeCommand git @("-C", $VaultRoot, "remote", "get-url", "--all", "--push", $remote) `
            -CaptureOutput -ReturnResult
        $urls = @($verify.Output -split "`r?`n" | Where-Object { $_ })
        if ($urls.Count -ne 1 -or $urls[0] -ne "no-push") {
            throw "Could not protect push URLs for remote $remote"
        }
    }
}
```

Call `Protect-PushUrls` only when `$installerCreatedClone -or $ProtectPush`. Exit code 1 from `--get-all` is the only tolerated absent-key result. Never suppress a generic native-command failure.

- [ ] **Step 6: Verify bootstrap and syntax GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_installer_bootstrap.py -q
bash -n install.sh
git diff --check -- install.sh install.ps1 tests/test_installer_bootstrap.py
```

On Windows also run:

```powershell
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path .\install.ps1), [ref]$tokens, [ref]$errors
)
if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }
```

Expected: tests and parsers pass; local checkout remote configuration is unchanged unless explicitly protected.

- [ ] **Step 7: Optional checkpoint only after explicit operator approval**

```bash
git add install.sh install.ps1 tests/test_installer_bootstrap.py
git commit -m "fix: pin remote installer bootstrap"
```

### Task 3: Persist Exact Roots And Resolve Effective OpenCode Configuration

**Files:**
- Create: `scripts/run-scheduled-task.ps1`
- Modify: `install.sh`
- Modify: `install.ps1`
- Modify: `scripts/install-scheduled-tasks.ps1`
- Modify: `tests/test_installer_config.py`
- Modify: `tests/test_integration_injection.py`

- [ ] **Step 1: Add failing root, scheduler, and precedence integration tests**

Add behavioral tests for these cases:

| Case | Required result |
|---|---|
| No state override | root and state both resolve to the absolute vault path |
| Process state override | exact absolute custom state path survives process, profile/user env, scheduler, and summary |
| Existing Windows user override | exact prior user state path is retained and rewritten explicitly |
| Relative root or state input | resolve once to an absolute path before any child command |
| POSIX profile rerun | one owned block with current exact values |
| Cron | both environment values plus `uv run --locked --no-sync --directory` are present; no `cd` dependency |
| Cron quoting | root, state, uv, and log paths containing spaces and apostrophes round-trip through `/bin/sh` |
| Task Scheduler | decoded command carries exact root, state, uv path, and selected maintenance kind |
| OpenCode JSONC | global entry is structurally merged and plugin goes to resolved XDG global directory |
| Project/custom/inline/managed override | installer reports `conflict` and never says OpenCode is active |
| OpenCode absent | installer reports `not_detected` without creating config |

Use a fake `opencode` executable that emits controlled `debug config` JSON. Run it from a temporary caller project containing an overriding `opencode.json` to prove the original caller directory, not the vault, controls project precedence.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_installer_config.py tests/test_integration_injection.py -q -k "xdg or root or state or scheduler or opencode"
```

Expected: failures show state-root clobbering, substring-based OpenCode detection, and missing scheduler environment.

- [ ] **Step 3: Resolve and export root/state before dependency or integration work**

In Bash, resolve existing directories through physical paths, preserve a custom state root, and export both values immediately:

```bash
VAULT_ROOT="$(cd "$VAULT_ROOT" && pwd -P)"
STATE_ROOT_INPUT="${LLM_WIKI_STATE_ROOT:-$VAULT_ROOT}"
mkdir -p "$STATE_ROOT_INPUT"
STATE_ROOT="$(cd "$STATE_ROOT_INPUT" && pwd -P)"
export LLM_WIKI_ROOT="$VAULT_ROOT"
export LLM_WIKI_STATE_ROOT="$STATE_ROOT"
python3 "$VAULT_ROOT/scripts/installer_config.py" profile \
  --profile "$PROFILE" --root "$VAULT_ROOT" --state-root "$STATE_ROOT"
```

In PowerShell, call `[System.IO.Path]::GetFullPath`, create the state directory, resolve it, set both process variables, and always persist both exact user values:

```powershell
$VAULT_ROOT = [System.IO.Path]::GetFullPath($VAULT_ROOT)
$userState = [Environment]::GetEnvironmentVariable("LLM_WIKI_STATE_ROOT", "User")
$stateInput = Resolve-StateRoot -ProcessState $env:LLM_WIKI_STATE_ROOT -UserState $userState -VaultRoot $VAULT_ROOT
New-Item -ItemType Directory -Path $stateInput -Force | Out-Null
$STATE_ROOT = (Resolve-Path -LiteralPath $stateInput).Path
$env:LLM_WIKI_ROOT = $VAULT_ROOT
$env:LLM_WIKI_STATE_ROOT = $STATE_ROOT
[Environment]::SetEnvironmentVariable("LLM_WIKI_ROOT", $VAULT_ROOT, "User")
[Environment]::SetEnvironmentVariable("LLM_WIKI_STATE_ROOT", $STATE_ROOT, "User")
```

Print `$STATE_ROOT`, not `$VAULT_ROOT`, in the Windows summary.

- [ ] **Step 4: Pass exact roots through cron and Task Scheduler**

Build POSIX cron lines with a shell-quoting helper and no working-directory assumption:

```bash
shell_quote() {
  local escaped="${1//\'/\'\\\'\'}"
  printf "'%s'" "$escaped"
}

CRON_NIGHTLY="0 3 * * * env LLM_WIKI_ROOT=$(shell_quote "$VAULT_ROOT") LLM_WIKI_STATE_ROOT=$(shell_quote "$STATE_ROOT") $(shell_quote "$UV_PATH") run --locked --no-sync --directory $(shell_quote "$VAULT_ROOT") python scripts/scheduled_nightly.py >> $(shell_quote "$STATE_ROOT/logs/cron-nightly.log") 2>&1"
CRON_WEEKLY="0 4 * * 0 env LLM_WIKI_ROOT=$(shell_quote "$VAULT_ROOT") LLM_WIKI_STATE_ROOT=$(shell_quote "$STATE_ROOT") $(shell_quote "$UV_PATH") run --locked --no-sync --directory $(shell_quote "$VAULT_ROOT") python scripts/scheduled_weekly.py >> $(shell_quote "$STATE_ROOT/logs/cron-weekly.log") 2>&1"
```

Create `scripts/run-scheduled-task.ps1` with explicit inputs and immediate native-exit propagation:

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("nightly", "weekly")]
    [string]$Kind,
    [Parameter(Mandatory = $true)][string]$VaultRoot,
    [Parameter(Mandatory = $true)][string]$StateRoot,
    [Parameter(Mandatory = $true)][string]$UvPath
)

$ErrorActionPreference = "Stop"
$env:LLM_WIKI_ROOT = [System.IO.Path]::GetFullPath($VaultRoot)
$env:LLM_WIKI_STATE_ROOT = [System.IO.Path]::GetFullPath($StateRoot)
$scriptName = if ($Kind -eq "nightly") { "scheduled_nightly.py" } else { "scheduled_weekly.py" }
& $UvPath run --locked --no-sync --directory $env:LLM_WIKI_ROOT python (Join-Path $env:LLM_WIKI_ROOT "scripts\$scriptName")
$nativeExit = $LASTEXITCODE
if ($nativeExit -ne 0) { exit $nativeExit }
exit 0
```

Change `scripts/install-scheduled-tasks.ps1` to accept mandatory `-VaultRoot`, `-StateRoot`, and `-UvPath`. Build a UTF-16LE `-EncodedCommand` that invokes the runner with single-quoted, doubled-quote-safe literal values. This avoids Task Scheduler argument splitting and makes the persisted values testable by decoding the command.

- [ ] **Step 5: Replace OpenCode substring handling with the shared helper**

After the locked MCP environment exists, invoke:

```bash
OPENCODE_RESULT="$(uv run --locked --no-sync --directory "$VAULT_ROOT" python \
  "$VAULT_ROOT/scripts/installer_config.py" opencode \
  --root "$VAULT_ROOT" --state-root "$STATE_ROOT" --cwd "$CALLER_CWD")"
```

PowerShell must invoke the same subcommand and parse it with `ConvertFrom-Json`. Only status `active` may append `OpenCode(active)` to the active-agent summary. `conflict` and `configured_unverified` are warnings with their exact status. Remove both old direct OpenCode config blocks from each installer.

- [ ] **Step 6: Run focused suites and parser checks**

Run:

```bash
uv run --locked --no-sync pytest tests/test_installer_config.py tests/test_integration_injection.py -q -k "xdg or root or state or scheduler or opencode"
uv run --locked --no-sync ruff check scripts/installer_config.py tests/test_installer_config.py tests/test_integration_injection.py
bash -n install.sh
```

On Windows, parse `install.ps1`, `scripts/install-scheduled-tasks.ps1`, and `scripts/run-scheduled-task.ps1` with `System.Management.Automation.Language.Parser`.

Expected: tests and parsers pass; the exact custom state path appears at every unattended boundary.

- [ ] **Step 7: Optional checkpoint only after explicit operator approval**

```bash
git add install.sh install.ps1 scripts/installer_config.py scripts/install-scheduled-tasks.ps1 scripts/run-scheduled-task.ps1 tests/test_installer_config.py tests/test_integration_injection.py
git commit -m "fix: persist installer roots and effective config"
```

### Task 4: Make Dependency Provisioning Locked, Additive, And Non-Mutating At Runtime

**Files:**
- Create: `tests/test_dependency_environments.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `install.sh`
- Modify: `install.ps1`
- Modify: `integrations/claude-code/settings.json`
- Modify: `integrations/codex/hooks.json`
- Modify: `scripts/llm-wiki-memory-opencode.js`
- Modify: `scripts/codex-memory-wrapper.ps1`
- Modify: `scripts/codex_memory.py`
- Modify: `scripts/merge_claude_settings.py`
- Modify: `scripts/doctor.py`
- Modify: `scripts/mcp_server.py`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_doctor.py`
- Modify: `tests/test_sync_memory.py`

- [ ] **Step 1: Write failing dependency ownership and command-shape tests**

Tests must parse TOML/JSON and exercise generated config, not search for an arbitrary substring. Assert:

```python
EXPECTED_MCP_ARGS = [
    "run",
    "--locked",
    "--no-sync",
    "--directory",
    "ROOT",
    "python",
    "scripts/mcp_server.py",
]


def test_reranker_extra_owns_every_direct_import() -> None:
    """The prerequisite profile stays intact and reranker owns all six imported packages."""


def test_fresh_install_is_locked_without_default_groups() -> None:
    """Fresh provisioning uses the exact production baseline from the platform plan."""


def test_reinstall_is_locked_inexact_and_preserves_selected_extras() -> None:
    """A reranker or code-graph package present before rerun remains installed."""


def test_every_unattended_command_is_locked_and_no_sync() -> None:
    """Hooks, MCP entries, plugin commands, scheduler, and wrapper commands use both flags."""
```

The repeated-environment test must create both relative and absolute temporary `UV_PROJECT_ENVIRONMENT` paths, sync the production baseline plus `code-graph`, rerun the installer dependency function, and compare `uv pip list --format json` before and after. It must prove a code-graph-only package remains present and that uv resolves a relative override from the workspace root. Also prove that an existing custom directory without `pyvenv.cfg` is rejected instead of being treated as a disposable fresh environment.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_dependency_contract.py tests/test_dependency_environments.py tests/test_integration_injection.py tests/test_doctor.py tests/test_sync_memory.py -q -k "dependency or locked or no_sync or mcp_config or unattended"
```

Expected: tests fail on custom-environment rerun detection, missing direct reranker requirements, and unattended commands that can mutate `.venv` or `uv.lock`.

- [ ] **Step 3: Declare direct dependencies in their owning environments**

Preserve the prerequisite production, hybrid, code-graph, development, `mcp-server = []`, and `[tool.uv].required-version` declarations byte-for-byte except for normal formatter movement. Add only the missing reranker direct imports and keep the version unchanged:

```toml
[project]
version = "4.0.0"

[project.optional-dependencies]
reranker = [
    "onnxruntime>=1.18,<1.24; python_version < '3.11'",
    "onnxruntime>=1.18,<2; python_version >= '3.11'",
    "optimum>=1.20,<2",
    "tokenizers>=0.19,<1",
    "torch>=2.2,<3",
    "transformers>=4.44,<6",
]
```

Keep model packages out of the production baseline. Regenerate and check the lock with the prerequisite uv contract:

```bash
uv lock
uv lock --check --no-python-downloads
```

- [ ] **Step 4: Make fresh sync exact and rerun sync additive**

Bash:

```bash
if [[ -n "${UV_PROJECT_ENVIRONMENT:-}" ]]; then
  case "$UV_PROJECT_ENVIRONMENT" in
    /*) PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" ;;
    *) PROJECT_ENVIRONMENT="$VAULT_ROOT/$UV_PROJECT_ENVIRONMENT" ;;
  esac
else
  PROJECT_ENVIRONMENT="$VAULT_ROOT/.venv"
fi

if [[ -e "$PROJECT_ENVIRONMENT" && ! -f "$PROJECT_ENVIRONMENT/pyvenv.cfg" ]]; then
  fail "Selected uv project environment is not a virtual environment: $PROJECT_ENVIRONMENT"
fi
SYNC_ARGS=(sync --locked --no-default-groups --quiet)
if [[ -f "$PROJECT_ENVIRONMENT/pyvenv.cfg" ]]; then
  SYNC_ARGS+=(--inexact)
fi
uv --directory "$VAULT_ROOT" "${SYNC_ARGS[@]}"
```

PowerShell:

```powershell
$projectEnvironment = if ($env:UV_PROJECT_ENVIRONMENT) {
    if ([System.IO.Path]::IsPathRooted($env:UV_PROJECT_ENVIRONMENT)) {
        [System.IO.Path]::GetFullPath($env:UV_PROJECT_ENVIRONMENT)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $VAULT_ROOT $env:UV_PROJECT_ENVIRONMENT))
    }
} else {
    Join-Path $VAULT_ROOT ".venv"
}
if ((Test-Path -LiteralPath $projectEnvironment) -and
    -not (Test-Path -LiteralPath (Join-Path $projectEnvironment "pyvenv.cfg"))) {
    Fail "Selected uv project environment is not a virtual environment: $projectEnvironment"
}
$syncArgs = @("sync", "--locked", "--no-default-groups", "--quiet")
if (Test-Path -LiteralPath (Join-Path $projectEnvironment "pyvenv.cfg")) { $syncArgs += "--inexact" }
Invoke-NativeCommand uv (@("--directory", $VAULT_ROOT) + $syncArgs)
```

Optional-extra instructions added in Task 11 must use these concrete additive forms:

```bash
uv sync --locked --no-default-groups --inexact --extra hybrid
uv sync --locked --no-default-groups --inexact --extra code-graph
uv sync --locked --no-default-groups --inexact --extra reranker
```

- [ ] **Step 5: Freeze every unattended `uv run`**

Use this exact ordering everywhere an agent, hook, MCP host, scheduler, or compatibility wrapper launches automatically:

```text
uv run --locked --no-sync --directory "$LLM_WIKI_ROOT" python scripts/mcp_server.py
```

Update JSON command arrays structurally. Update `codex_memory.codex_mcp_config_state()` and Doctor to require the exact argument list. Add `integration_adapter.py` to `OUR_SCRIPT_MARKERS` in `merge_claude_settings.py` so repeated installation replaces prior generated hooks rather than duplicating them.

Do not rewrite interactive examples that are not an unattended boundary merely to make a global text search pass.

- [ ] **Step 6: Run dependency and integration suites GREEN**

Run:

```bash
uv lock --check --no-python-downloads
uv run --locked --no-sync pytest tests/test_dependency_contract.py tests/test_dependency_environments.py tests/test_integration_injection.py tests/test_doctor.py tests/test_sync_memory.py -q
uv run --locked --no-sync ruff check scripts/codex_memory.py scripts/merge_claude_settings.py scripts/doctor.py scripts/mcp_server.py tests/test_dependency_environments.py tests/test_integration_injection.py tests/test_doctor.py tests/test_sync_memory.py
node --check scripts/llm-wiki-memory-opencode.js
```

Expected: all pass; the repeated temporary environment retains optional packages.

- [ ] **Step 7: Optional checkpoint only after explicit operator approval**

```bash
git add pyproject.toml uv.lock install.sh install.ps1 integrations/claude-code/settings.json integrations/codex/hooks.json scripts/llm-wiki-memory-opencode.js scripts/codex-memory-wrapper.ps1 scripts/codex_memory.py scripts/merge_claude_settings.py scripts/doctor.py scripts/mcp_server.py tests/test_dependency_environments.py tests/test_integration_injection.py tests/test_doctor.py tests/test_sync_memory.py
git commit -m "fix: preserve locked installer environments"
```

### Task 5: Harden The Shared 120-Second Installer Smoke And Native Failure Gate

**Files:**
- Modify: `scripts/install_smoke.py`
- Modify: `tests/test_install_smoke.py`
- Modify: `install.sh`
- Modify: `install.ps1`
- Modify: `tests/test_integration_injection.py`
- Modify: `tests/test_quality_guards.py`

- [ ] **Step 1: Write failing aggregate-deadline and native-failure tests**

Cover these outcomes:

| Stage | Accepted | Rejected |
|---|---|---|
| imports | all required imports succeed | any import error |
| Doctor | valid JSON with `ok` or `degraded`; degraded becomes installer warning | timeout, invalid JSON, status `error`, exit greater than 1 |
| MCP | initialized stdio session returns exactly the existing 12 names | timeout, server exit, malformed response, any other count |
| cleanup | client and server process close on success or failure | surviving child process |
| aggregate budget | all stages finish within one 120-second monotonic deadline | each stage independently receiving 120 seconds |
| final installer output | success only after smoke completes | any later success line after mandatory failure |

Inject Bash and PowerShell native command failures for Git, uv sync, scheduled-task registration, runtime sync exit 2, and smoke exit 2. Verify nonzero installer exit and absence of `installed successfully`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_install_smoke.py tests/test_integration_injection.py tests/test_quality_guards.py -q -k "smoke or mandatory or native or installer"
```

Expected: the platform-plan smoke tests remain green, while new exact-name and injected native-failure assertions fail until this task is implemented.

- [ ] **Step 3: Extend the shared smoke without creating a second runner**

Keep the prerequisite `run_smoke(root, state_root, *, deadline_seconds=120.0)` implementation, `sys.executable` stdio child, one monotonic deadline, bounded diagnostics, and CLI `--deadline-seconds` contract. Add the exact stable tool-name contract:

```python
EXPECTED_TOOL_NAMES = (
    "recall",
    "read_page",
    "wiki_overview",
    "vault_status",
    "get_decisions",
    "get_context",
    "check_contradiction",
    "log_decision",
    "compile",
    "find_dead_code",
    "get_architecture",
    "doctor",
)


def validate_tool_contract(tools: tuple[str, ...]) -> None:
    if len(tools) != len(EXPECTED_TOOL_NAMES) or set(tools) != set(EXPECTED_TOOL_NAMES):
        raise RuntimeError("MCP smoke returned an unexpected tool contract")
```

Call `validate_tool_contract()` on the real list-tools result. A schema-valid Doctor `degraded` report remains a successful smoke result with `status: degraded`; the installer parses that result and records a warning. Import, timeout, malformed Doctor, Doctor `error`, MCP, or cleanup failure remains nonzero with no private traceback.

- [ ] **Step 4: Centralize mandatory native-command handling**

Bash must use `set -euo pipefail` plus explicit branches where exit 1 has defined warning semantics. PowerShell must call one Windows PowerShell 5.1-compatible helper and capture `$LASTEXITCODE` immediately:

```powershell
function Invoke-NativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int[]]$AllowedExitCodes = @(0),
        [switch]$CaptureOutput,
        [switch]$ReturnResult
    )
    if ($CaptureOutput) {
        $output = @(& $FilePath @ArgumentList)
    } else {
        & $FilePath @ArgumentList
    }
    $nativeExit = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $nativeExit) {
        throw "$FilePath failed with exit code $nativeExit"
    }
    if ($ReturnResult) {
        return [pscustomobject]@{
            ExitCode = $nativeExit
            Output = if ($CaptureOutput) { $output -join [Environment]::NewLine } else { $null }
        }
    }
    if ($CaptureOutput) { return ($output -join [Environment]::NewLine) }
}
```

Mandatory failures are Git bootstrap/verification, requested push protection, pinned uv installation verification, dependency sync, profile or user-env persistence, runtime directory creation, scheduler registration, runtime sync exit 2, and installer smoke. Runtime sync exit 1 and Doctor `degraded` are warnings. Optional agent absence or config conflict is a warning and must be named in the summary as inactive or conflicted.

- [ ] **Step 5: Reorder the installer and remove full-suite execution**

The final order is:

1. bootstrap and verify source;
2. resolve/export/persist roots;
3. verify prerequisites and pinned uv;
4. locked production sync;
5. create runtime directories and register maintenance;
6. install optional detected integrations without false active claims;
7. run bounded `sync_memory.py` with defined 0/1/other semantics;
8. run `install_smoke.py --deadline-seconds 120` under `uv run --locked --no-sync` and parse its JSON status;
9. print success or success-with-warnings.

Keep the platform plan's release-specific uv endpoints:

```text
https://releases.astral.sh/github/uv/releases/download/0.12.1/uv-installer.sh
https://releases.astral.sh/github/uv/releases/download/0.12.1/uv-installer.ps1
```

After installation, require `uv --version` to report `uv 0.12.1`. An already installed different uv version is rejected with a clear instruction rather than silently changing the lock tool.

- [ ] **Step 6: Run focused suites GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_install_smoke.py tests/test_installer_bootstrap.py tests/test_installer_config.py tests/test_integration_injection.py tests/test_quality_guards.py tests/test_sync_memory.py -q
uv run --locked --no-sync ruff check scripts/install_smoke.py tests/test_install_smoke.py tests/test_installer_bootstrap.py tests/test_installer_config.py tests/test_integration_injection.py tests/test_quality_guards.py tests/test_sync_memory.py
bash -n install.sh
```

Expected: all pass; no installer test invokes the full suite as an installation step.

- [ ] **Step 7: Optional checkpoint only after explicit operator approval**

```bash
git add scripts/install_smoke.py tests/test_install_smoke.py install.sh install.ps1 tests/test_integration_injection.py tests/test_quality_guards.py
git commit -m "fix: fail closed on installer smoke"
```

### Task 6: Package The Installed-Vault Repair Surface

**Files:**
- Create: `scripts/repair_installed_memory.py`
- Create: `tests/test_repair_installed_memory.py`

This task is an integration task over `docs/superpowers/plans/2026-08-05-v4-reliability-queue.md`. Execute it only after that plan exposes `inspect_installed_vault()` and `repair_installed_vault()` from `installed_memory_repair`. Do not duplicate transaction, ownership, capture-intent, queue, receipt, or adoption validators in the CLI.

The required backend interface is:

```python
def inspect_installed_vault(*, root: Path, state_root: Path) -> dict[str, object]:
    """Run shared runtime validators without mutation."""


def repair_installed_vault(
    *,
    root: Path,
    state_root: Path,
    adopt_ownership_v3: bool,
    confirm_all_agents_stopped: bool,
) -> dict[str, object]:
    """Resume safe repairs or perform the separately gated offline adoption."""
```

- [ ] **Step 1: Write failing CLI safety tests**

Test real temporary-vault fixtures and assert:

```python
def test_default_mode_is_read_only_check(tmp_path: Path) -> None:
    """No flag means check, and every file/hash/remote remains unchanged."""


def test_check_and_apply_are_mutually_exclusive(tmp_path: Path) -> None:
    """The parser exits 2 before backend work."""


def test_apply_is_required_for_every_mutating_backend_call(tmp_path: Path) -> None:
    """Adoption or repair selection without --apply exits 2."""


def test_adoption_requires_explicit_stopped_agent_confirmation(tmp_path: Path) -> None:
    """--apply --adopt-ownership-v3 alone cannot cross the offline gate."""


def test_apply_never_changes_git_or_deletes_knowledge_run_or_legacy_state(tmp_path: Path) -> None:
    """Compare Git config, knowledge hashes, run inventory, caches, and markers before/after."""


def test_json_report_has_closed_mode_status_actions_and_blockers(tmp_path: Path) -> None:
    """Output is parseable and does not expose private exception text."""
```

Also assert `--help` documents that default mode is read-only, `--apply` is required, adoption is offline, and the command never removes `run/`, knowledge, legacy caches, or compatibility markers.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_repair_installed_memory.py -q
```

Expected: collection fails because the repair CLI is absent.

- [ ] **Step 3: Implement the thin check-by-default CLI**

Use this parser and dispatch shape:

```python
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from installed_memory_repair import inspect_installed_vault, repair_installed_vault


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Check or explicitly repair an installed LLM-Wiki vault.")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only validation; this is the default")
    mode.add_argument("--apply", action="store_true", help="permit the selected resumable repair")
    result.add_argument("--adopt-ownership-v3", action="store_true", help="perform the offline v3 adoption")
    result.add_argument(
        "--confirm-all-agents-stopped",
        action="store_true",
        help="confirm every process using this vault is stopped for offline adoption",
    )
    result.add_argument("--root", type=Path, default=None)
    result.add_argument("--state-root", type=Path, default=None)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.adopt_ownership_v3 and not args.apply:
        argument_parser.error("--adopt-ownership-v3 requires --apply")
    if args.confirm_all_agents_stopped and not args.adopt_ownership_v3:
        argument_parser.error("--confirm-all-agents-stopped requires --adopt-ownership-v3")
    if args.adopt_ownership_v3 and not args.confirm_all_agents_stopped:
        argument_parser.error("offline adoption requires --confirm-all-agents-stopped")
    root_input = args.root or os.environ.get("LLM_WIKI_ROOT")
    if root_input is None:
        argument_parser.error("--root or LLM_WIKI_ROOT is required")
    mode_name = "apply" if args.apply else "check"
    try:
        root = Path(root_input).resolve()
        state_root = Path(args.state_root or os.environ.get("LLM_WIKI_STATE_ROOT", root)).resolve()
        report = (
            repair_installed_vault(
                root=root,
                state_root=state_root,
                adopt_ownership_v3=args.adopt_ownership_v3,
                confirm_all_agents_stopped=args.confirm_all_agents_stopped,
            )
            if args.apply
            else inspect_installed_vault(root=root, state_root=state_root)
        )
        status = str(report.get("overall_status", ""))
        if status not in {"ok", "degraded", "error"}:
            raise ValueError("backend returned an invalid status")
        payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except Exception:
        status = "error"
        report = {
            "mode": mode_name,
            "overall_status": status,
            "actions": [],
            "blockers": [{"code": "repair_backend_error"}],
        }
        payload = json.dumps(report, indent=2, sort_keys=True)
    if args.json:
        print(payload)
    else:
        print(f"{mode_name}: {status}")
        print(f"actions: {len(report.get('actions', []))}")
        print(f"blockers: {len(report.get('blockers', []))}")
    return {"ok": 0, "degraded": 1, "error": 2}[status]
```

The broad exception boundary is only a redacted CLI boundary; it must never reinterpret failure as success or expose exception text. Tests inject a backend exception containing a private path and require only `repair_backend_error`. The CLI itself must not import `subprocess`, invoke Git, unlink files, remove directories, or implement a second validator. The backend enforces canonical `repair` ownership and the separate offline adoption gate.

- [ ] **Step 4: Run focused tests and non-destructive checks GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_repair_installed_memory.py -q
uv run --locked --no-sync ruff check scripts/repair_installed_memory.py tests/test_repair_installed_memory.py
uv run --locked --no-sync python scripts/repair_installed_memory.py --help
```

Expected: tests pass and help returns 0. The subprocess tests invoke `--check --json` only with explicit temporary `--root` and `--state-root` fixture paths; no verification command defaults to the operator's installed `LLM_WIKI_ROOT` or points at this public worktree.

- [ ] **Step 5: Optional checkpoint only after explicit operator approval**

```bash
git add scripts/repair_installed_memory.py tests/test_repair_installed_memory.py
git commit -m "feat: expose installed vault repair checks"
```

### Task 7: Restrict Worktree Cleanup To Approved Agent Roots

**Files:**
- Create: `tests/test_cleanup_worktrees.py`
- Modify: `scripts/cleanup_worktrees.py`

- [ ] **Step 1: Write failing metadata and containment tests**

Use real temporary Git worktrees with paths containing spaces and, on POSIX, a newline. Assert:

```python
def test_nul_porcelain_parser_preserves_unusual_paths() -> None:
    """`git worktree list --porcelain -z` records are decoded without line parsing."""


def test_primary_worktree_is_first_git_record_not_caller_cwd() -> None:
    """Calling from a linked worktree still keeps the actual primary worktree."""


def test_only_resolved_descendants_of_exact_agent_roots_are_reported() -> None:
    """Substring lookalikes and sibling worktrees remain wholly out of scope."""


def test_normal_apply_never_invokes_global_prune() -> None:
    """`--apply` removes only eligible in-scope worktrees."""


def test_prune_requires_separate_named_action_and_apply() -> None:
    """Dry run reports; mutation requires both --prune-stale-metadata and --apply."""


def test_interactive_force_delete_cannot_escape_approved_roots() -> None:
    """Confirmation cannot expand the initial candidate set."""
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_cleanup_worktrees.py -q
```

Expected: tests fail because metadata is line-delimited, main follows caller CWD, substring checks admit lookalikes, and normal apply prunes globally.

- [ ] **Step 3: Parse NUL-delimited metadata and derive scope from Git**

Replace text execution for worktree listing with bytes and parse records explicitly:

```python
def parse_worktree_porcelain(raw: bytes) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for field in raw.split(b"\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        text = field.decode("utf-8", errors="surrogateescape")
        key, separator, value = text.partition(" ")
        current[key] = value if separator else True
    if current:
        records.append(current)
    return records


def approved_roots(primary: Path) -> tuple[Path, ...]:
    return tuple(
        (primary / name / "worktrees").resolve(strict=False)
        for name in (".claude", ".codex", ".opencode")
    )


def in_approved_root(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved != root and resolved.is_relative_to(root) for root in roots)
```

Per Git documentation, the first `worktree` record is the primary worktree. Mark that record as primary regardless of the caller. Filter candidates to approved roots before clean/merged checks, reporting, prompts, or deletion.

- [ ] **Step 4: Separate stale metadata pruning**

Add `--prune-stale-metadata`. Without `--apply`, run `git worktree prune --dry-run --verbose` and report only. With both flags, run `git worktree prune --verbose`. Remove the unconditional prune from normal `--apply`.

Pass `--` before worktree paths and branch names where Git supports it. Keep clean plus merged as the automatic deletion condition. Keep locked, missing, detached-unmerged, dirty, and unknown records.

- [ ] **Step 5: Run focused and quality suites GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_cleanup_worktrees.py tests/test_quality_guards.py -q
uv run --locked --no-sync ruff check scripts/cleanup_worktrees.py tests/test_cleanup_worktrees.py
```

Expected: all pass; ordinary cleanup never calls `git worktree prune`.

- [ ] **Step 6: Optional checkpoint only after explicit operator approval**

```bash
git add scripts/cleanup_worktrees.py tests/test_cleanup_worktrees.py
git commit -m "fix: scope agent worktree cleanup"
```

### Task 8: Remove The Mutable Reranker Default

**Files:**
- Modify: `scripts/reranker.py`
- Modify: `tests/test_reranker.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing explicit-identity and local-only tests**

Add tests that reload the module under controlled environment variables:

```python
def test_no_environment_configuration_means_no_reranker_bundle(monkeypatch) -> None:
    monkeypatch.delenv("LLMWIKI_RERANKER_MODEL", raising=False)
    monkeypatch.delenv("LLMWIKI_RERANKER_REVISION", raising=False)
    module = importlib.reload(reranker)
    assert module.configured_reranker_identity() is None
    assert module._get_reranker_bundle() is None


@pytest.mark.parametrize(
    ("model", "revision"),
    [("model", ""), ("", "a" * 40), ("model", "main"), ("model", "v1"), ("model", "A" * 40)],
)
def test_partial_or_mutable_identity_is_rejected(monkeypatch, model: str, revision: str) -> None:
    """Both values and one lowercase 40-hex revision are required."""


def test_explicit_immutable_model_load_is_local_only(monkeypatch) -> None:
    """Model and tokenizer receive the same ID/revision, local_files_only=True, trust_remote_code=False."""


def test_missing_local_artifact_degrades_without_network(monkeypatch) -> None:
    """A cache miss returns unavailable and does not retry a network-enabled call."""
```

Keep deterministic scorer tests independent of model configuration.

- [ ] **Step 2: Run reranker tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_reranker.py -q
```

Expected: tests fail because `BAAI/bge-reranker-base@main` is still implicit and `from_pretrained` can download.

- [ ] **Step 3: Require an explicit immutable local identity**

Replace the mutable constants with:

```python
IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$")


def configured_reranker_identity() -> tuple[str, str] | None:
    model = os.environ.get("LLMWIKI_RERANKER_MODEL", "").strip()
    revision = os.environ.get("LLMWIKI_RERANKER_REVISION", "").strip()
    if not model and not revision:
        return None
    if not model or not IMMUTABLE_REVISION.fullmatch(revision):
        return None
    return model, revision
```

Return unavailable before dependency probing when identity is absent or invalid. Load both artifacts with:

```python
model = ORTModelForSequenceClassification.from_pretrained(
    model_name,
    file_name="onnx/model.onnx",
    revision=revision,
    local_files_only=True,
    trust_remote_code=False,
)
tokenizer = AutoTokenizer.from_pretrained(
    model_name,
    revision=revision,
    local_files_only=True,
    trust_remote_code=False,
)
```

The command-line probe must say either `not configured`, `configured but not present locally`, or `loaded locally`. Remove every claim that first use downloads a model.

- [ ] **Step 4: Run reranker and retrieval integration tests GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_reranker.py tests/test_search_memory.py tests/test_retrieval.py -q -k "rerank or reranker"
uv run --locked --no-sync ruff check scripts/reranker.py tests/test_reranker.py
uv lock --check --no-python-downloads
```

Expected: all pass without network access or a model cache.

- [ ] **Step 5: Optional checkpoint only after explicit operator approval**

```bash
git add scripts/reranker.py tests/test_reranker.py pyproject.toml uv.lock
git commit -m "fix: require an immutable local reranker"
```

### Task 9: Pin CI Inputs And Retain Timeout Evidence

**Files:**
- Modify: `tests/test_ci_policy.py`
- Create: `scripts/ci_timing_report.py`
- Create: `tests/test_ci_timing_report.py`
- Create: `benchmark/ci-timeout-evidence-v1.schema.json`
- Create after a successful measured run: `benchmark/ci-timeout-evidence-2026-08-05.json`
- Modify: `.github/workflows/tests.yml`

- [ ] **Step 1: Write failing workflow policy tests**

Parse workflow YAML and assert behavior, including expression-based timeout values:

```python
FULL_SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")
RUNNERS = {"ubuntu-24.04", "windows-2025", "macos-15"}


def test_every_external_action_is_full_sha_pinned() -> None:
    """Every `uses` value is immutable and has a nearby release comment."""


def test_every_runner_is_an_explicit_supported_generation() -> None:
    """No scalar or matrix runner contains `latest`."""


def test_uv_is_pinned_to_0_12_1_in_every_setup_step() -> None:
    """The setup action and the uv binary version are independently pinned."""


def test_python_matrix_contains_3_10_and_3_14() -> None:
    """Linux/macOS cover endpoints, Windows retains 3.10-3.14, and clean lanes cover endpoints."""


def test_job_timeouts_match_approved_classes() -> None:
    """Focused=15, clean/installer=20, Linux/macOS full=45, Windows full=60."""


def test_ruff_covers_scripts_tests_and_benchmark() -> None:
    """Static analysis cannot omit benchmark code."""
```

- [ ] **Step 2: Write failing timing compiler tests**

Provide fixture GitHub job JSON plus JUnit XML for successful attempts 1 through 5 of one workflow run at one exact head SHA. Assert rejection of fewer than five attempts, noncontiguous or duplicate attempts, mixed run IDs, absent classes, failed or timed-out jobs, and mismatched SHAs. Assert nearest-rank p95 across independent job-runtime samples, UTC timestamps, per-test durations, ceilings, and no nonfinite number. The output must validate against `benchmark/ci-timeout-evidence-v1.schema.json` through `reliable_memory.validate_schema`.

- [ ] **Step 3: Run policy and timing tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_ci_policy.py tests/test_ci_timing_report.py -q
```

Expected: prerequisite pin, runner, and Python assertions stay green; failures identify the absent timing compiler, upload evidence, and native installer timing class.

- [ ] **Step 4: Implement the timing evidence compiler**

The CLI accepts paired repeated `--run-json` and `--junit-root` arguments, plus `--head-sha` and `--output`. Require equal pair counts, exactly one workflow run ID with successful attempts `[1, 2, 3, 4, 5]`, the exact requested head SHA in every attempt, and at least five job-runtime samples in every timeout class. Calculate inclusive p95 without interpolation ambiguity:

```python
def nearest_rank_p95(values: list[float]) -> float:
    if not values:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]
```

The closed Draft 2020-12 report schema requires these exact fields and rejects additional properties at every object level:

| Field | Constraint |
|---|---|
| `schema_version` | constant `ci-timeout-evidence/v1` |
| `head_sha` | string matching `^[0-9a-f]{40}$` |
| `generated_at` | canonical UTC `YYYY-MM-DDTHH:MM:SSZ` string |
| `workflow_attempts` | exactly five objects for one positive run ID, attempts 1-5, and the same `head_sha` |
| `jobs` | non-empty objects with run ID, attempt, positive job ID, closed class, exact name, conclusion, UTC start/end, and finite nonnegative runtime seconds |
| `classes` | exactly `focused`, `clean`, `installer`, `linux_full`, `windows_full`, and `macos_full` |
| class summary | exact approved ceiling, finite nonnegative `p95_seconds`, and non-empty typed sample objects |
| `tests` | non-empty full-suite records with run ID, attempt, job ID, artifact name, node ID, and finite nonnegative duration seconds |

Each class sample records workflow run ID, run attempt, job ID, and runtime seconds. Require every observed p95 to be below its ceiling with at least 20 percent headroom. A timeout, missing conclusion, missing JUnit artifact for a full job, duplicate run-attempt pair, fewer than five class samples, or mismatched head SHA invalidates the report.

Classify jobs only through a closed three-field job name emitted by the workflow, such as `timing::linux_full::py3.10`; do not infer classes from runner strings or substring fragments. The middle field must be exactly one of the six schema keys. Full-suite artifact names include the run attempt to remain immutable across reruns, for example `pytest-timings-linux_full-py3.10-attempt-1`, and contain one JUnit XML file whose suite duration reconciles with its job window. Reject unknown prefixes, duplicate job IDs, duplicate artifact identities, negative durations, and timestamps where completion precedes start.

- [ ] **Step 5: Replace workflow mutability and split timeout classes**

Use the verified action revisions from Current-Source Basis. Every checkout sets `persist-credentials: false`. Every setup-uv step includes:

```yaml
with:
  version: "0.12.1"
  enable-cache: true
```

Preserve the platform plan's job split, Gitleaks 10-minute timeout, 15-minute lint/Pyright ceilings, 20-minute clean ceilings, full Python coverage, and action major versions. Add the native installer class and timing artifacts without collapsing jobs. The full-suite matrix remains:

```yaml
matrix:
  include:
    - os: ubuntu-24.04
      python: "3.10"
      timeout: 45
      class: linux_full
    - os: ubuntu-24.04
      python: "3.14"
      timeout: 45
      class: linux_full
    - os: windows-2025
      python: "3.10"
      timeout: 60
      class: windows_full
    - os: windows-2025
      python: "3.11"
      timeout: 60
      class: windows_full
    - os: windows-2025
      python: "3.12"
      timeout: 60
      class: windows_full
    - os: windows-2025
      python: "3.13"
      timeout: 60
      class: windows_full
    - os: windows-2025
      python: "3.14"
      timeout: 60
      class: windows_full
    - os: macos-15
      python: "3.10"
      timeout: 45
      class: macos_full
    - os: macos-15
      python: "3.14"
      timeout: 45
      class: macos_full
```

Run full suites with `--junitxml` and `--durations=0`, then upload the XML even on failure through the pinned upload action. Keep `fail-fast: false`; a timeout remains a failed job and has no retry waiver.

Give every timed job an explicit name such as `timing::linux_full::py3.10`, `timing::clean::production-py3.14`, or `timing::installer::windows`. Artifact names use the matching class and Python value plus `attempt-${{ github.run_attempt }}`. Policy tests parse these names and require every expected matrix identity exactly once.

- [ ] **Step 6: Run local policy tests GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_ci_policy.py tests/test_ci_timing_report.py tests/test_quality_guards.py -q
uv run --locked --no-sync ruff check scripts/ci_timing_report.py tests/test_ci_policy.py tests/test_ci_timing_report.py
```

Expected: policy and timing tests pass before remote CI is requested.

- [ ] **Step 7: Stop before remote evidence until every CI class exists**

Do not create `benchmark/ci-timeout-evidence-2026-08-05.json` yet. Task 10 adds the native installer class and then performs the exact-head measurement. A hand-authored or partial report is a failure.

- [ ] **Step 8: Optional local checkpoint only after explicit operator approval**

```bash
git add .github/workflows/tests.yml scripts/ci_timing_report.py tests/test_ci_policy.py tests/test_ci_timing_report.py benchmark/ci-timeout-evidence-v1.schema.json
git commit -m "ci: compile timeout evidence"
```

### Task 10: Verify Clean Profiles And Add Native Installer CI Lanes

**Files:**
- Modify: `.github/workflows/tests.yml`
- Modify: `tests/test_ci_policy.py`
- Modify: `tests/test_dependency_environments.py`

- [ ] **Step 1: Add failing clean-lane workflow tests**

Assert the workflow contains these independent environments:

| Lane | Python | Sync | Proof |
|---|---|---|---|
| production MCP | 3.10 and 3.14 | `--locked --no-default-groups` | pytest absent, imports pass, Doctor JSON, exactly 12 stdio tools |
| hybrid | 3.10 and 3.14 | `--locked --no-default-groups --extra hybrid` | pytest absent, NumPy import, vector create/read smoke |
| code graph | 3.10 and 3.14 | `--locked --no-default-groups --extra code-graph` | Jedi, tree-sitter core/all grammars, and fixture indexing |
| full development | Linux/macOS 3.10 and 3.14; Windows 3.10-3.14 | `--locked` | full suite |
| installer | native Linux and Windows | full development test environment | bootstrap/config/smoke/native-failure tests |
| Pyright | all OS families, Python 3.10, Node 22.23.1 | full development environment | explicit state path and existing qualification gates |

Each clean lane gets a unique `UV_PROJECT_ENVIRONMENT` under `${{ runner.temp }}` so no prior step supplies accidental packages.

- [ ] **Step 2: Run workflow tests and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_ci_policy.py tests/test_dependency_environments.py -q
```

Expected: prerequisite clean-profile assertions stay green; failures identify the absent Linux/Windows native installer matrix and any lane that does not use its own environment.

- [ ] **Step 3: Preserve and strengthen the production MCP matrix**

Use an explicit Ubuntu runner and 20-minute ceiling:

```yaml
clean-production:
  runs-on: ubuntu-24.04
  timeout-minutes: 20
  strategy:
    fail-fast: false
    matrix:
      python: ["3.10", "3.14"]
  env:
    UV_PROJECT_ENVIRONMENT: ${{ runner.temp }}/llm-wiki-production-${{ matrix.python }}
    LLM_WIKI_ROOT: ${{ github.workspace }}
    LLM_WIKI_STATE_ROOT: ${{ runner.temp }}/llm-wiki-state-${{ matrix.python }}
    MEMORY_LLM_PROVIDER: fake
  steps:
    - name: Checkout
      uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      with:
        persist-credentials: false
    - name: Setup uv
      uses: astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86 # v5.4.2
      with:
        version: "0.12.1"
        enable-cache: true
    - name: Install Python
      run: uv python install ${{ matrix.python }}
    - name: Pin Python
      run: uv python pin ${{ matrix.python }}
    - name: Sync production MCP environment
      run: uv sync --locked --no-default-groups
    - name: Prove development packages are absent
      run: uv run --locked --no-sync python -c "import importlib.util; assert importlib.util.find_spec('pytest') is None"
    - name: Production installer smoke
      run: uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
```

The smoke itself performs Doctor and MCP. Do not replace it with imports only.

- [ ] **Step 4: Add clean code-graph and native installer lanes**

The existing code-graph lane uses a unique `UV_PROJECT_ENVIRONMENT` and these exact no-pytest commands after its explicit sync:

```bash
uv run --locked --no-sync python -c "import importlib, importlib.util; modules=('jedi','tree_sitter','tree_sitter_python','tree_sitter_javascript','tree_sitter_typescript','tree_sitter_go','tree_sitter_rust','tree_sitter_java','tree_sitter_c','tree_sitter_cpp','tree_sitter_ruby','tree_sitter_php','tree_sitter_c_sharp','tree_sitter_bash'); assert importlib.util.find_spec('pytest') is None; [importlib.import_module(name) for name in modules]"
uv run --locked --no-sync python scripts/code_graph.py tests/fixtures/code_kernel/python
```

Set `LLM_WIKI_STATE_ROOT` to that job's `${{ runner.temp }}` directory so fixture indexing cannot write runtime state into the checkout.

The installer matrix uses `ubuntu-24.04` and `windows-2025`, a 20-minute ceiling, and runs:

```text
tests/test_installer_bootstrap.py
tests/test_installer_config.py
tests/test_install_smoke.py
tests/test_integration_injection.py -k installer
tests/test_dependency_environments.py -k installer
```

Do not skip Windows PowerShell 5.1 tests on `windows-2025`.

- [ ] **Step 5: Make Pyright state passing shell-independent**

Keep Node `22.23.1`, but replace shell variable expansion in `run` commands with GitHub expressions:

```yaml
- name: Explicit Pyright install
  run: uv run --locked --no-sync python scripts/install_pyright.py --state-root "${{ runner.temp }}/llm-wiki-state"

- name: Protocol, process-tree, and security tests
  env:
    LLM_WIKI_STATE_ROOT: ${{ runner.temp }}/llm-wiki-state
  run: uv run --locked --no-sync pytest tests/test_lsp_protocol.py tests/test_lsp_process.py tests/test_lsp_process_tree.py tests/test_lsp_security.py tests/test_pyright_session.py tests/test_code_navigation.py tests/test_code_navigation_renderer.py -q
```

Pin setup-node to the full SHA listed above.

- [ ] **Step 6: Run local workflow policy and lock checks GREEN**

Run:

```bash
uv lock --check --no-python-downloads
uv run --locked --no-sync pytest tests/test_ci_policy.py tests/test_dependency_environments.py -q
```

Expected: both pass; clean jobs have no dependency on the full-development environment.

- [ ] **Step 7: Generate retained timing evidence after every CI class exists**

After pushing is explicitly authorized and the exact-head workflow succeeds once, keep the branch unchanged. Collect that attempt, rerun the complete workflow four times, and collect each new attempt before starting the next one. `gh run watch --exit-status` makes any failed or timed-out attempt stop the sequence; do not replace it with a successful retry as a waiver.

```bash
set -euo pipefail
HEAD_SHA="$(git rev-parse HEAD)"
BRANCH="$(git branch --show-current)"
[[ -n "$BRANCH" ]] || { echo "timing evidence requires a named branch" >&2; exit 1; }
TIMING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/llm-wiki-ci-timings.XXXXXX")"
trap 'rm -rf "$TIMING_ROOT"' EXIT
RUN_ID="$(gh run list --workflow tests.yml --branch "$BRANCH" --commit "$HEAD_SHA" --status success --limit 20 --json databaseId,attempt --jq '[.[] | select(.attempt == 1)][0].databaseId // ""')"
[[ -n "$RUN_ID" ]] || { echo "one successful exact-head first attempt is required" >&2; exit 1; }
REPORT_ARGS=()
RUN_ATTEMPT=""
for SAMPLE_NUMBER in 1 2 3 4 5; do
  if [[ "$SAMPLE_NUMBER" -gt 1 ]]; then
    PREVIOUS_ATTEMPT="$RUN_ATTEMPT"
    gh run rerun "$RUN_ID"
    RUN_ATTEMPT=""
    for POLL_NUMBER in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      RUN_ATTEMPT="$(gh run view "$RUN_ID" --json attempt --jq '.attempt')"
      [[ "$RUN_ATTEMPT" -gt "$PREVIOUS_ATTEMPT" ]] && break
      sleep 2
    done
    [[ "$RUN_ATTEMPT" -gt "$PREVIOUS_ATTEMPT" ]] || { echo "rerun attempt did not start" >&2; exit 1; }
    gh run watch "$RUN_ID" --exit-status
  else
    RUN_ATTEMPT="$(gh run view "$RUN_ID" --json attempt --jq '.attempt')"
  fi
  [[ "$RUN_ATTEMPT" -eq "$SAMPLE_NUMBER" ]] || { echo "workflow attempts must be contiguous from 1 through 5" >&2; exit 1; }
  COMPLETED_ATTEMPT="$(gh run view "$RUN_ID" --json attempt --jq '.attempt')"
  [[ "$COMPLETED_ATTEMPT" -eq "$RUN_ATTEMPT" ]] || { echo "workflow attempt changed during collection" >&2; exit 1; }
  RUN_ROOT="$TIMING_ROOT/$RUN_ID-$RUN_ATTEMPT"
  mkdir -p "$RUN_ROOT/junit"
  gh run view "$RUN_ID" --attempt "$RUN_ATTEMPT" --json databaseId,attempt,conclusion,headSha,jobs > "$RUN_ROOT/run.json"
  gh run download "$RUN_ID" --pattern "pytest-timings-*-attempt-$RUN_ATTEMPT" --dir "$RUN_ROOT/junit"
  REPORT_ARGS+=(--run-json "$RUN_ROOT/run.json" --junit-root "$RUN_ROOT/junit")
done
uv run --locked --no-sync python scripts/ci_timing_report.py \
  "${REPORT_ARGS[@]}" \
  --head-sha "$HEAD_SHA" \
  --output benchmark/ci-timeout-evidence-2026-08-05.json
uv run --locked --no-sync pytest tests/test_ci_timing_report.py -q
```

Expected: the generated report validates, binds five complete successful attempts of one current-commit run, includes every required class and full matrix job, and proves headroom for each selected ceiling. If it does not, adjust job decomposition rather than weakening tests.

- [ ] **Step 8: Optional checkpoint only after explicit operator approval**

```bash
git add .github/workflows/tests.yml tests/test_ci_policy.py tests/test_dependency_environments.py benchmark/ci-timeout-evidence-2026-08-05.json
git commit -m "ci: retain installer timing evidence"
```

### Task 11: Synchronize Truthful Installer, CI, Repair, And Reranker Documentation

**Files:**
- Modify: `tests/test_readme_i18n.py`
- Modify: `tests/test_quality_guards.py`
- Modify: `README.md`
- Modify: `README.ru.md`
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/USER-GUIDE.md`
- Modify: `integrations/README.md`

- [ ] **Step 1: Write failing claim-evidence and translation-parity tests**

Extend the i18n guard so each README contains equivalent facts in its own language:

```python
SHARED_COMMANDS = (
    "uv sync --locked --no-default-groups",
    "uv sync --locked --no-default-groups --inexact --extra hybrid",
    "uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120",
    "uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json",
)


def test_readmes_do_not_advertise_unpublished_release_installers() -> None:
    for path, text in _readmes():
        assert "/v4.0.0/install." not in text
        assert "CI green" not in text
        assert "brightgreen.svg" not in text


def test_readmes_require_full_oid_for_remote_bootstrap() -> None:
    for path, text in _readmes():
        assert "LLM_WIKI_COMMIT" in text
        assert "40" in text
        assert "raw.githubusercontent.com/Ekgardt/llm-wiki/main/install." not in text


def test_readmes_share_no_default_local_only_reranker_contract() -> None:
    for path, text in _readmes():
        for token in (
            "LLMWIKI_RERANKER_MODEL",
            "LLMWIKI_RERANKER_REVISION",
            "local_files_only=True",
            "trust_remote_code=False",
        ):
            assert token in text, f"{path} is missing {token}"
        assert "BAAI/bge-reranker-base" not in text
```

Add a quality guard that fails if workflow runner labels contain `latest`, action references are not full SHAs, any public document calls CI green in this non-release repair, or `pyproject.toml` changes from `4.0.0`.

- [ ] **Step 2: Run documentation guards and verify RED**

Run:

```bash
uv run --locked --no-sync pytest tests/test_readme_i18n.py tests/test_quality_guards.py -q
```

Expected: prerequisite baseline documentation assertions remain green; failures identify missing full-OID bootstrap, repair, exact integration status, measured CI evidence, or local-only reranker facts.

- [ ] **Step 3: Update all three READMEs in one change**

Document remote bootstrap without embedding an unpublished commit:

```bash
read -r -p "Reviewed 40-character commit OID: " LLM_WIKI_COMMIT
[[ "$LLM_WIKI_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || { echo "A full 40-hex commit OID is required" >&2; exit 1; }
LLM_WIKI_COMMIT="$(printf '%s' "$LLM_WIKI_COMMIT" | tr 'ABCDEF' 'abcdef')"
export LLM_WIKI_COMMIT
( set -o pipefail; curl -fsSL "https://raw.githubusercontent.com/Ekgardt/llm-wiki/$LLM_WIKI_COMMIT/install.sh" | bash )
```

```powershell
$env:LLM_WIKI_COMMIT = Read-Host "Reviewed 40-character commit OID"
if ($env:LLM_WIKI_COMMIT -notmatch '^[0-9a-fA-F]{40}$') { throw "A full 40-hex commit OID is required" }
$env:LLM_WIKI_COMMIT = $env:LLM_WIKI_COMMIT.ToLowerInvariant()
$installerPath = Join-Path $env:TEMP ("llm-wiki-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
$windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
try {
    Invoke-WebRequest "https://raw.githubusercontent.com/Ekgardt/llm-wiki/$($env:LLM_WIKI_COMMIT)/install.ps1" -UseBasicParsing -OutFile $installerPath -ErrorAction Stop
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $installerPath
    $installerExit = $LASTEXITCODE
    if ($installerExit -ne 0) { throw "Installer failed with exit code $installerExit" }
} finally {
    Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
}
```

State plainly that the reviewed full-OID URL selects immutable bootstrap bytes and the installer independently verifies the cloned checkout OID. Remove mutable `main` bootstrap commands and unpublished `v4.0.0` installer URLs. Keep the version badge at `4.0.0` because this is not a release.

Replace the green test badge with a neutral informational badge. Replace `CI green` with a factual statement that the workflow uses explicit Ubuntu/macOS generations on Python 3.10 and 3.14 plus Windows 2025 on Python 3.10-3.14, and that GitHub Actions results are the evidence.

Document the actual installer steps: pinned uv 0.12.1, locked production MCP baseline without default groups, exact roots, scheduler propagation, effective integration status, runtime sync, and 120-second smoke instead of the full suite.

Document manual optional additions with locked inexact commands. Explain that reranking has no selected default, requires both environment variables, requires a lowercase 40-hex immutable revision, and never downloads during a query. Document read-only repair check and the separately gated apply/adoption form.

- [ ] **Step 4: Update supporting docs and Unreleased changelog**

Update developer setup to distinguish:

```bash
uv sync --locked
uv run --locked --no-sync pytest -q
```

Update integration examples to use locked no-sync command arrays. Append one separate `Fixed` bullet under the existing `CHANGELOG.md` `[Unreleased]` section covering I1-I6, CI1-CI2, ML1, and D1; do not rewrite or duplicate the platform plan's B1-B9 bullet. Do not add a release heading, release date, tag, or version change.

- [ ] **Step 5: Run documentation and structure-adjacent guards GREEN**

Run:

```bash
uv run --locked --no-sync pytest tests/test_readme_i18n.py tests/test_quality_guards.py tests/test_structure.py -q
uv run --locked --no-sync python -m compileall -q -x "tests[\\/]fixtures[\\/]code_kernel[\\/]python[\\/]pkg[\\/]broken[.]py" scripts tests benchmark
git diff --check
```

Expected: all pass; EN/RU/ZH describe the same install, CI, repair, and reranker contracts.

- [ ] **Step 6: Optional checkpoint only after explicit operator approval**

```bash
git add README.md README.ru.md README.zh-CN.md CHANGELOG.md CONTRIBUTING.md docs/USER-GUIDE.md integrations/README.md tests/test_readme_i18n.py tests/test_quality_guards.py
git commit -m "docs: describe reliable installation evidence"
```

### Task 12: Run Final Cross-Platform Verification

**Files:**
- Verify every path listed in File Map
- Do not modify the protected pre-existing dirty paths

- [ ] **Step 1: Run lock, focused, lint, and syntax gates**

Run:

```bash
uv lock --check --no-python-downloads
uv run --locked --no-sync pytest tests/test_dependency_contract.py tests/test_installer_bootstrap.py tests/test_installer_config.py tests/test_install_smoke.py tests/test_dependency_environments.py tests/test_cleanup_worktrees.py tests/test_repair_installed_memory.py tests/test_reranker.py tests/test_ci_policy.py tests/test_ci_timing_report.py tests/test_integration_injection.py tests/test_doctor.py tests/test_sync_memory.py tests/test_readme_i18n.py tests/test_quality_guards.py tests/test_structure.py -q
uv run --locked --no-sync ruff check scripts/ tests/ benchmark/
uv run --locked --no-sync python -m compileall -q -x "tests[\\/]fixtures[\\/]code_kernel[\\/]python[\\/]pkg[\\/]broken[.]py" scripts tests benchmark
bash -n install.sh
node --check scripts/llm-wiki-memory-opencode.js
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Parse every PowerShell surface with Windows PowerShell 5.1**

Run on Windows:

```powershell
$paths = @(
    ".\install.ps1",
    ".\scripts\install-scheduled-tasks.ps1",
    ".\scripts\run-scheduled-task.ps1",
    ".\scripts\codex-memory-wrapper.ps1"
)
foreach ($path in $paths) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        (Resolve-Path $path), [ref]$tokens, [ref]$errors
    )
    if ($errors.Count) {
        $errors | ForEach-Object { Write-Error $_ }
        exit 1
    }
}
```

Expected: exit 0 under `powershell.exe`, not only `pwsh`.

- [ ] **Step 3: Prove a clean production environment without default groups locally**

Use a disposable environment outside the repository. Run the POSIX form on Linux/macOS:

```bash
(
set -euo pipefail
VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/llm-wiki-prod-verify.XXXXXX")"
export UV_PROJECT_ENVIRONMENT="$VERIFY_ROOT/venv"
VERIFY_STATE="$VERIFY_ROOT/state"
export LLM_WIKI_ROOT="$(pwd -P)"
export LLM_WIKI_STATE_ROOT="$VERIFY_STATE"
trap 'rm -rf "$VERIFY_ROOT"' EXIT
uv sync --locked --no-default-groups
uv run --locked --no-sync python -c "import importlib.util; assert importlib.util.find_spec('pytest') is None"
uv run --locked --no-sync python -c "import os; from pathlib import Path; Path(os.environ['LLM_WIKI_STATE_ROOT']).mkdir(parents=True, exist_ok=True)"
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
rm -rf "$VERIFY_ROOT"
trap - EXIT
)
```

Run the native form on Windows:

```powershell
$verifyRoot = Join-Path $env:TEMP ("llm-wiki-prod-verify-" + [guid]::NewGuid().ToString("N"))
$previousRoot = [Environment]::GetEnvironmentVariable("LLM_WIKI_ROOT", "Process")
$previousState = [Environment]::GetEnvironmentVariable("LLM_WIKI_STATE_ROOT", "Process")
$previousProjectEnvironment = [Environment]::GetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", "Process")
$env:UV_PROJECT_ENVIRONMENT = Join-Path $verifyRoot "venv"
$verifyState = Join-Path $verifyRoot "state"
$vaultRoot = (Get-Location).Path
$env:LLM_WIKI_ROOT = $vaultRoot
$env:LLM_WIKI_STATE_ROOT = $verifyState
New-Item -ItemType Directory -Path $verifyRoot -Force | Out-Null
try {
    & uv sync --locked --no-default-groups
    $nativeExit = $LASTEXITCODE
    if ($nativeExit -ne 0) { throw "uv sync failed with exit code $nativeExit" }
    & uv run --locked --no-sync python -c "import importlib.util; assert importlib.util.find_spec('pytest') is None"
    $nativeExit = $LASTEXITCODE
    if ($nativeExit -ne 0) { throw "production import probe failed with exit code $nativeExit" }
    New-Item -ItemType Directory -Path $verifyState -Force | Out-Null
    & uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
    $nativeExit = $LASTEXITCODE
    if ($nativeExit -ne 0) { throw "installer smoke failed with exit code $nativeExit" }
} finally {
    Remove-Item -LiteralPath $verifyRoot -Recurse -Force -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("LLM_WIKI_ROOT", $previousRoot, "Process")
    [Environment]::SetEnvironmentVariable("LLM_WIKI_STATE_ROOT", $previousState, "Process")
    [Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", $previousProjectEnvironment, "Process")
}
```

Expected: smoke returns 0 with structured status `ok` or `degraded`; the installer turns `degraded` into a warning, MCP returns the exact 12 tool names, and pytest remains absent.

- [ ] **Step 4: Run the full local regression suite**

Restore the development environment, then run:

```bash
uv sync --locked
uv run --locked --no-sync pytest -q
```

Expected: the complete suite passes. On Windows this is the mandatory local full-suite evidence. Linux clean and full matrices plus macOS confirmation come from CI.

- [ ] **Step 5: Inspect only intended changes and protected paths**

Run:

```bash
git status --short
git diff --name-only
git diff -- .gitignore AGENTS.md CLAUDE.md docs/STRUCTURE.md knowledge/index.md knowledge/log.md docs/superpowers/specs/2026-08-05-v4-reliability-repair-design.md docs/superpowers/plans/2026-08-05-v4-reliability-platform.md docs/superpowers/plans/2026-08-05-v4-reliability-queue.md knowledge/notes/v4-reliability-contracts-decision.md
```

Expected: the protected-path diff is byte-for-byte identical to the baseline seen before implementation. Do not restore, stage, or edit those paths.

- [ ] **Step 6: Review requirement closure**

Confirm the final evidence maps exactly:

| Finding | Required closing evidence |
|---|---|
| I1 | isolated full-OID pipe bootstrap, exact HEAD/repository/files, caller CWD exclusion, remote preservation/protection tests |
| I2 | 120-second real smoke, PowerShell native helper injection, no false success summary |
| I3 | XDG matrix, JSONC/effective OpenCode conflicts, exact profile/user env and scheduler values |
| I4 | clean exact no-default-groups MCP sync, repeated inexact inventory preservation, locked no-sync unattended commands |
| I5 | NUL parsing, Git-primary identity, exact resolved containment, no normal global prune |
| I6 | default check, explicit apply, offline confirmation, shared validators, non-destructive fixture hashes |
| CI1 | full action pins, explicit runners, uv 0.12.1, approved ceilings, retained valid timing report |
| CI2 | Python 3.10/3.14 no-default-groups MCP smoke and isolated optional code-graph lane |
| ML1 | no default, immutable explicit revision, local-only load, no query download |
| D1 | synchronized EN/RU/ZH, neutral evidence wording, no unpublished installer tag URL, Unreleased changelog only |

- [ ] **Step 7: Stop before Git mutation unless explicitly requested**

Report test and CI evidence, remaining warnings, and the exact changed-file list. Do not run `git add`, `git commit`, `git push`, create a tag, or create a release without a new explicit operator request.
