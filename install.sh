#!/usr/bin/env bash
# LLM-Wiki one-command installer for macOS, Linux, and WSL2.
#
# Usage from an inspected checkout:
#   git clone https://github.com/Ekgardt/llm-wiki.git
#   cd llm-wiki
#   LLM_WIKI_ROOT="$(pwd)" bash ./install.sh
#
# Remote bootstrap requires LLM_WIKI_COMMIT to be a full commit OID.
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs the locked production dependency baseline
#   3. Runs a bounded production smoke
#   4. Persists roots through the resumable install control plane
#   5. Sets up native maintenance (cron is explicit degraded fallback)
#   6. Detects installed agents and wires them up
#   7. Prints next steps
#
# Safe to re-run. Idempotent. Never overwrites user config without backup.

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
UV_VERSION="0.12.3"
REPOSITORY_URL="https://github.com/Ekgardt/llm-wiki.git"
CALLER_CWD="$(pwd -P)"
INSTALLER_CREATED_CLONE="${LLM_WIKI_INSTALLER_CREATED_CLONE:-0}"
PROTECT_PUSH=0
SCHEDULER_MODE=native
EXPECT_SCHEDULER_VALUE=0

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

for argument in "$@"; do
  if [[ "$EXPECT_SCHEDULER_VALUE" -eq 1 ]]; then
    SCHEDULER_MODE="$argument"
    EXPECT_SCHEDULER_VALUE=0
    continue
  fi
  case "$argument" in
    --protect-push) PROTECT_PUSH=1 ;;
    --scheduler) EXPECT_SCHEDULER_VALUE=1 ;;
    --scheduler=*) SCHEDULER_MODE="${argument#--scheduler=}" ;;
    *) fail "Unknown installer argument: $argument" ;;
  esac
done
[[ "$EXPECT_SCHEDULER_VALUE" -eq 0 ]] || fail "--scheduler requires native or cron"
case "$SCHEDULER_MODE" in
  native|cron) ;;
  *) fail "--scheduler requires native or cron" ;;
esac

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

protect_push_urls_if_authorized() {
  if [[ "$INSTALLER_CREATED_CLONE" == "1" || "$PROTECT_PUSH" == "1" ]]; then
    protect_push_urls
  fi
}

install_codex_hooks() {
  local vault_root="$1"
  local codex_dir="$2"
  local hook_exit
  if uv run --directory "$vault_root" python "$vault_root/scripts/codex_memory.py" \
    merge-hooks \
    --source "$vault_root/integrations/codex/hooks.json" \
    --destination "$codex_dir/hooks.json" \
    --config "$codex_dir/config.toml"; then
    return 0
  else
    hook_exit=$?
  fi
  if [ "$hook_exit" -eq 4 ]; then
    echo "Codex lifecycle hooks are disabled. Set [features] hooks = true in config.toml and rerun the installer; hooks.json was not changed." >&2
  fi
  return "$hook_exit"
}

configure_codex_mcp() {
  local vault_root="$1"
  local config="$2"
  local state vault_json block
  state="$(uv run --directory "$vault_root" python "$vault_root/scripts/codex_memory.py" \
    config-state --config "$config" --vault-root "$vault_root")" || return 1
  case "$state" in
    equivalent)
      return 0
      ;;
    absent)
      mkdir -p "$(dirname "$config")"
      vault_json="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$vault_root")"
      block="$(printf '%s\n' \
        '[mcp_servers.llm-wiki]' \
        'command = "uv"' \
        "args = [\"run\", \"--locked\", \"--no-sync\", \"--directory\", $vault_json, \"python\", \"scripts/mcp_server.py\"]")"
      if [ -f "$config" ]; then
        cp -p "$config" "$config.bak"
        if [ -s "$config" ]; then
          printf '\n%s\n' "$block" >> "$config"
        else
          printf '%s\n' "$block" >> "$config"
        fi
      else
        printf '%s\n' "$block" > "$config"
      fi
      return 0
      ;;
    conflict|invalid)
      return 2
      ;;
    *)
      return 1
      ;;
  esac
}

# ─── 1. Resolve vault root ──────────────────────────────────────────

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
else
  SCRIPT_DIR=""
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  VAULT_ROOT="${LLM_WIKI_ROOT:-$SCRIPT_DIR}"
  REQUESTED_ROOT="$(cd "$VAULT_ROOT" 2>/dev/null && pwd -P)" || \
    fail "LLM_WIKI_ROOT does not identify an accessible checkout."
  if [[ "$REQUESTED_ROOT" != "$SCRIPT_DIR" ]]; then
    fail "LLM_WIKI_ROOT points to a different checkout than this installer."
  fi
  VAULT_ROOT="$REQUESTED_ROOT"
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
  for required in pyproject.toml uv.lock install.sh install.ps1 scripts/installer_config.py scripts/install_control.py; do
    [[ -f "$VAULT_ROOT/$required" ]] || fail "Installed checkout is missing $required"
  done
  export LLM_WIKI_ROOT="$VAULT_ROOT"
  export LLM_WIKI_INSTALLER_CREATED_CLONE=1
  exec bash "$VAULT_ROOT/install.sh" "$@"
fi

INHERITED_STATE_ROOT="${LLM_WIKI_STATE_ROOT:-}"
STATE_ROOT_INPUT="${LLM_WIKI_STATE_ROOT:-$VAULT_ROOT}"
mkdir -p "$STATE_ROOT_INPUT"
STATE_ROOT="$(cd "$STATE_ROOT_INPUT" && pwd -P)"
export LLM_WIKI_ROOT="$VAULT_ROOT"
export LLM_WIKI_STATE_ROOT="$STATE_ROOT"

# Detect shell profile before any child process observes the installation roots.
if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
  PROFILE="${HOME}/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
  PROFILE="${HOME}/.bashrc"
else
  PROFILE="${HOME}/.profile"
fi
cd "$VAULT_ROOT"
info "Vault root: $VAULT_ROOT"
info "State root: $STATE_ROOT"
# A state root left over from another vault silently splits the installation
# across two directories, so say so rather than letting it pass as a default.
if [ -n "$INHERITED_STATE_ROOT" ] && [ "$STATE_ROOT" != "$VAULT_ROOT" ]; then
  warn "Runtime state will live outside this vault, in $STATE_ROOT"
  warn "That came from LLM_WIKI_STATE_ROOT in your environment, not from this vault."
  warn "Unset it and re-run to keep run/, logs/ and cache/ inside $VAULT_ROOT"
fi

# ─── 2. Check prerequisites ────────────────────────────────────────

info "Checking prerequisites..."

# Python 3.10+
if ! command -v python3 &>/dev/null; then
  fail "Python 3 is required but not installed. Install from https://python.org"
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || ([[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 10 ]]); then
  fail "Python 3.10+ required, found $PY_VERSION"
fi
ok "Python $PY_VERSION"

# git
if ! command -v git &>/dev/null; then
  fail "git is required but not installed."
fi
ok "git $(git --version)"

# uv
if ! command -v uv &>/dev/null; then
  fail "uv is required at version ${UV_VERSION}. Install it from https://astral.sh/uv/${UV_VERSION}/install.sh and rerun the installer."
fi
installedUvVersion="$(uv --version | awk '{print $2}')"
if [ "$installedUvVersion" != "$UV_VERSION" ]; then
  fail "uv is required at version ${UV_VERSION}, found ${installedUvVersion}. Upgrade uv explicitly and rerun the installer."
fi
ok "uv ${installedUvVersion}"

# ─── 3. Install dependencies ───────────────────────────────────────

info "Installing locked production dependencies..."
SYNC_PLAN="$(python3 "$VAULT_ROOT/scripts/installer_config.py" sync-args \
  --root "$VAULT_ROOT" --environment "${UV_PROJECT_ENVIRONMENT:-}")"
PROJECT_ENVIRONMENT="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["environment"])' "$SYNC_PLAN")"
SYNC_ARGS=()
mapfile -t SYNC_ARGS < <(python3 -c 'import json, sys; print(*json.loads(sys.argv[1])["arguments"], sep="\n")' "$SYNC_PLAN")
export UV_PROJECT_ENVIRONMENT="$PROJECT_ENVIRONMENT"
uv "${SYNC_ARGS[@]}"
ok "Production dependencies installed (MCP included)"

# ─── 4. Run production smoke ───────────────────────────────────────

info "Running production smoke..."
testTimeoutSeconds="${LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS:-180}"
case "$testTimeoutSeconds" in
  ""|*[!0-9]*|0) fail "LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS must be a positive integer" ;;
esac
testPid=""
testPgid=""
testTimerPid=""
testTimedOut=0
testMonitorMode=""
restore_test_monitor_mode() {
  case "$testMonitorMode" in
    on) set -m ;;
    off) set +m ;;
  esac
  testMonitorMode=""
}
test_tree_alive() {
  if [ -z "$testPid" ]; then
    return 1
  fi
  case "$testPgid" in
    ""|*[!0-9]*|0|1|"$$") kill -0 "$testPid" 2>/dev/null ;;
    *) kill -0 -- "-$testPgid" 2>/dev/null ;;
  esac
}
stop_test_child() {
  local attempt
  if [ -z "$testPid" ]; then
    return
  fi
  case "$testPgid" in
    ""|*[!0-9]*|0|1|"$$")
      if test_tree_alive; then
        if kill -s TERM "$testPid" 2>/dev/null; then :; fi
        if kill -s CONT "$testPid" 2>/dev/null; then :; fi
      fi
      ;;
    *)
      if test_tree_alive; then
        if kill -s TERM -- "-$testPgid" 2>/dev/null; then :; fi
        if kill -s CONT -- "-$testPgid" 2>/dev/null; then :; fi
        attempt=0
        while [ "$attempt" -lt 5 ] && test_tree_alive; do
          sleep 0.1
          attempt=$((attempt + 1))
        done
        if test_tree_alive; then
          if kill -s KILL -- "-$testPgid" 2>/dev/null; then :; fi
        fi
      fi
      ;;
  esac
  if wait "$testPid" 2>/dev/null; then :; fi
  testPid=""
  testPgid=""
}
stop_test_timer() {
  if [ -z "$testTimerPid" ]; then
    return
  fi
  case "$testTimerPid" in
    *[!0-9]*|0|1|"$$") if kill -s TERM "$testTimerPid" 2>/dev/null; then :; fi ;;
    *)
      if kill -s TERM -- "-$testTimerPid" 2>/dev/null; then :; fi
      if kill -s CONT -- "-$testTimerPid" 2>/dev/null; then :; fi
      ;;
  esac
  if wait "$testTimerPid" 2>/dev/null; then :; fi
  testTimerPid=""
}
wait_test_child() {
  local status
  if wait "$testPid"; then
    status=0
  else
    status=$?
  fi
  stop_test_timer
  if test_tree_alive; then
    stop_test_child
  else
    testPid=""
    testPgid=""
  fi
  restore_test_monitor_mode
  return "$status"
}
start_test_child() {
  case "$-" in
    *m*) testMonitorMode=on ;;
    *) testMonitorMode=off; set -m ;;
  esac
  uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120 &
  testPid=$! testPgid=$!
  (
    trap 'exit 0' HUP INT TERM
    sleep "$testTimeoutSeconds"
    kill -s USR1 "$$"
  ) 2>/dev/null &
  testTimerPid=$!
}
handle_test_timeout() {
  trap - USR1
  testTimedOut=1
  stop_test_timer
  stop_test_child
}
handle_test_signal() {
  local status="$1"
  trap - HUP INT TERM USR1
  stop_test_timer
  stop_test_child
  restore_test_monitor_mode
  exit "$status"
}
trap 'stop_test_timer; stop_test_child; restore_test_monitor_mode' EXIT
trap 'handle_test_signal 129' HUP
trap 'handle_test_signal 130' INT
trap 'handle_test_signal 143' TERM
trap 'handle_test_timeout' USR1
start_test_child
if wait_test_child; then
  testExit=0
else
  testExit=$?
fi
trap - EXIT HUP INT TERM USR1
if [ "$testTimedOut" -eq 1 ]; then
  fail "Production smoke timed out after ${testTimeoutSeconds}s; installation aborted"
elif [ "$testExit" -ne 0 ]; then
  fail "Production smoke failed; installation aborted"
fi
ok "Production smoke passed"

# ─── 5. Set environment variables ──────────────────────────────────

# External configuration starts only after the mandatory test gate.
protect_push_urls_if_authorized
if [[ "$INSTALLER_CREATED_CLONE" == "1" || "$PROTECT_PUSH" == "1" ]]; then
  ok "Push disabled (no-push) for every configured remote"
fi

# Create runtime dirs inside the vault (gitignored)
mkdir -p "$STATE_ROOT/run" "$STATE_ROOT/run/queue" "$STATE_ROOT/logs" "$STATE_ROOT/cache"
ok "Runtime dirs: $STATE_ROOT/{run,logs,cache} (gitignored)"

CURSOR_HOOKS=0
ANTIGRAVITY_HOOKS=0
OPENCODE_PLUGIN=0
if [ -d "$HOME/.cursor" ] || command -v cursor &>/dev/null; then
  CURSOR_HOOKS=1
fi
if [ -d "$HOME/.gemini/antigravity-ide" ] || command -v agy &>/dev/null; then
  ANTIGRAVITY_HOOKS=1
fi
# Detected here rather than in step 7 so the plugin is written by the ownership
# transaction: an uninstall has to be able to take back exactly what it wrote.
if [ -d "$HOME/.config/opencode" ] || command -v opencode &>/dev/null; then
  OPENCODE_PLUGIN=1
fi
IDE_HOOK_ARGS=()
if [ "$CURSOR_HOOKS" -eq 1 ]; then
  IDE_HOOK_ARGS+=(--cursor-hooks)
fi
if [ "$ANTIGRAVITY_HOOKS" -eq 1 ]; then
  IDE_HOOK_ARGS+=(--antigravity-hooks)
fi
if [ "$OPENCODE_PLUGIN" -eq 1 ]; then
  IDE_HOOK_ARGS+=(--opencode-plugin)
fi

# ─── 6. Set up scheduled maintenance ────────────────────────────────

info "Setting up scheduled maintenance..."

UV_PATH="$(command -v uv)"
INSTALL_CONTROL_RESULT="$(uv run --locked --no-sync --directory "$VAULT_ROOT" python \
  "$VAULT_ROOT/scripts/install_control.py" install \
  --root "$VAULT_ROOT" \
  --state-root "$STATE_ROOT" \
  --uv-path "$UV_PATH" \
  --home "$HOME" \
  --scheduler "$SCHEDULER_MODE" \
  --profile "$PROFILE" \
  "${IDE_HOOK_ARGS[@]}")" || fail "Install ownership transaction failed"
SCHEDULER_BACKEND="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["scheduler_backend"])' "$INSTALL_CONTROL_RESULT")"
case "$SCHEDULER_BACKEND" in
  launchd|systemd_user|cron) ;;
  *) fail "Install control returned an invalid scheduler backend" ;;
esac
ok "Environment roots and $SCHEDULER_BACKEND maintenance are verified"

# ─── 7. Detect and wire up agents ──────────────────────────────────

info "Detecting installed agents..."

AGENT_STATUSES=()
VAULT_JSON=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$VAULT_ROOT")

# OpenCode configuration is merged structurally and verified after all precedence layers.
OPENCODE_RESULT="$(uv run --locked --no-sync --directory "$VAULT_ROOT" python \
  "$VAULT_ROOT/scripts/installer_config.py" opencode \
  --root "$VAULT_ROOT" --state-root "$STATE_ROOT" --cwd "$CALLER_CWD")"
OPENCODE_STATUS="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["status"])' "$OPENCODE_RESULT")"
case "$OPENCODE_STATUS" in
  active)
    AGENT_STATUSES+=("OpenCode: active automatic")
    ok "OpenCode configuration is active"
    uv run --locked --no-sync --directory "$VAULT_ROOT" python \
      "$VAULT_ROOT/scripts/session_start_context.py" \
      --output-file "$STATE_ROOT/cache/session-context.md" 2>/dev/null || true
    ;;
  conflict)
    AGENT_STATUSES+=("OpenCode: conflict")
    warn "OpenCode configuration status: conflict"
    ;;
  configured_unverified)
    AGENT_STATUSES+=("OpenCode: configured unverified")
    warn "OpenCode configuration status: configured_unverified"
    ;;
  not_detected) : ;;
  *) fail "OpenCode configuration helper returned an invalid status" ;;
esac

# Codex CLI
if command -v codex &>/dev/null; then
  CODEX_MCP_READY=0
  CODEX_HOOKS_READY=0
  CODEX_CONFIG="$HOME/.codex/config.toml"
  mkdir -p "$HOME/.codex"
  if configure_codex_mcp "$VAULT_ROOT" "$CODEX_CONFIG"; then
    CODEX_MCP_READY=1
    ok "Codex MCP config verified: $CODEX_CONFIG"
  else
    mcp_exit=$?
    if [ "$mcp_exit" -eq 2 ]; then
      warn "Existing Codex MCP entry conflicts with LLM-Wiki; config.toml was not changed. Merge manually."
    else
      warn "Codex MCP config could not be verified; config.toml was not changed."
    fi
  fi
  CODEX_HOOKS="$HOME/.codex/hooks.json"
  if install_codex_hooks "$VAULT_ROOT" "$HOME/.codex"; then
    CODEX_HOOKS_READY=1
    ok "Codex official hooks merged: $CODEX_HOOKS"
    info "Open /hooks in Codex to review and trust the LLM-Wiki commands."
  else
    hook_exit=$?
    if [ "$hook_exit" -eq 2 ]; then
      warn "Active inline Codex hooks require manual merge and /hooks trust review; hooks.json was not changed."
    elif [ "$hook_exit" -eq 3 ]; then
      CODEX_HOOKS_READY=1
      ok "Equivalent LLM-Wiki hooks are already configured inline; hooks.json was not changed."
      info "Open /hooks in Codex to review and trust the inline LLM-Wiki commands."
    elif [ "$hook_exit" -eq 4 ]; then
      : # install_codex_hooks already printed the manual enable instruction.
    else
      warn "Codex hooks were not changed; review the existing hooks configuration manually."
    fi
  fi
  if [ "$CODEX_MCP_READY" -eq 1 ] && [ "$CODEX_HOOKS_READY" -eq 1 ]; then
    AGENT_STATUSES+=("Codex: manual /hooks trust review required")
  else
    AGENT_STATUSES+=("Codex: conflict or unverified")
  fi
fi

# Cursor
if [ "$CURSOR_HOOKS" -eq 1 ]; then
  AGENT_STATUSES+=("Cursor: active automatic local hooks")
  ok "Cursor local user hooks are active"
  info "Cursor cloud agents do not load user-level hooks."
fi

# Antigravity
if [ "$ANTIGRAVITY_HOOKS" -eq 1 ]; then
  AGENT_STATUSES+=("Antigravity: active automatic local hooks")
  ok "Antigravity local user hooks are active"
fi

# Claude Code — merge hooks if CLI or config dir present (safe: backup + non-destructive)
if command -v claude &>/dev/null || [ -d "$HOME/.claude" ] || [ -f "$HOME/.claude.json" ]; then
  CLAUDE_AUTOMATIC=0
  info "Merging LLM-wiki hooks into Claude user settings (backup first)..."
  if uv run python "$VAULT_ROOT/scripts/merge_claude_settings.py" \
      --vault-root "$VAULT_ROOT" \
      --state-root "$STATE_ROOT"; then
    CLAUDE_AUTOMATIC=1
    ok "Claude settings merged → ~/.claude/settings.json"
  else
    warn "Claude settings merge failed — run: uv run python scripts/merge_claude_settings.py"
  fi
  # v4.0: MCP server config for Claude Code
  CLAUDE_MCP="$HOME/.claude.json"
  if [ ! -f "$CLAUDE_MCP" ]; then
    info "Adding MCP server config for Claude Code..."
    printf '%s\n' '{"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--locked","--no-sync","--directory",'"$VAULT_JSON"',"python","scripts/mcp_server.py"]}}}' > "$CLAUDE_MCP"
    ok "Claude MCP config: ~/.claude.json"
  elif ! grep -q '"llm-wiki"' "$CLAUDE_MCP" 2>/dev/null; then
    # `~/.claude.json` is Claude Code's live state file and it writes to it
    # while running, so this must not read-modify-write it. Its own CLI adds
    # the entry safely; without the CLI the only honest option is to say what
    # to add.
    if command -v claude &>/dev/null && claude mcp add --scope user llm-wiki \
        -- uv run --locked --no-sync --directory "$VAULT_ROOT" python scripts/mcp_server.py \
        >/dev/null 2>&1; then
      ok "Claude MCP server registered: llm-wiki"
    else
      warn "Existing ~/.claude.json found without llm-wiki; add it with:"
      warn "  claude mcp add --scope user llm-wiki -- uv run --locked --no-sync --directory $VAULT_ROOT python scripts/mcp_server.py"
    fi
  fi
  if [ "$CLAUDE_AUTOMATIC" -eq 1 ]; then
    AGENT_STATUSES+=("Claude Code: active automatic")
  else
    AGENT_STATUSES+=("Claude Code: conflict or unverified")
  fi
fi

if [ "${#AGENT_STATUSES[@]}" -eq 0 ]; then
  warn "No supported agents detected. Install OpenCode, Codex CLI, Claude Code, Cursor, or Antigravity."
else
  ok "Agent integrations:"
  printf '  - %s\n' "${AGENT_STATUSES[@]}"
fi

# ─── 8. Bounded runtime sync ───────────────────────────────────────

info "Synchronizing runtime state and derived indexes..."
SYNC_EXIT=0
SYNC_WARNING=0
uv run --locked --no-sync python "$VAULT_ROOT/scripts/sync_memory.py" --apply || SYNC_EXIT=$?
case "$SYNC_EXIT" in
  0) ok "Runtime state synchronized" ;;
  1) SYNC_WARNING=1; warn "Runtime synchronization completed with warnings" ;;
  *) fail "Runtime synchronization failed" ;;
esac

# ─── 9. Optional: semantic + hybrid search ─────────────────────────

info "Optional: install hybrid search (BM25 + vector + reranker)?"
info "  uv sync --locked --no-default-groups --inexact --extra hybrid"
info "  uv sync --locked --no-default-groups --inexact --extra code-graph"
info "  uv sync --locked --no-default-groups --inexact --extra reranker"

# ─── 10. Print summary ─────────────────────────────────────────────

echo ""
echo "=============================================="
if [ "$SYNC_WARNING" -eq 1 ]; then
  echo -e "${YELLOW}  LLM-Wiki installed with warnings${NC}"
else
  echo -e "${GREEN}  LLM-Wiki installed successfully!${NC}"
fi
echo "=============================================="
echo ""
echo "Vault:          $VAULT_ROOT"
echo "State:          $STATE_ROOT"
echo "Profile:        $PROFILE"
echo "Agent integrations:"
if [ "${#AGENT_STATUSES[@]}" -eq 0 ]; then
  echo "  - none detected"
else
  printf '  - %s\n' "${AGENT_STATUSES[@]}"
fi
echo "Maintenance:    $SCHEDULER_BACKEND (nightly 03:00 + weekly Sun 04:00)"
echo ""
echo "Next steps:"
echo "  1. Restart your terminal (to pick up env vars)"
echo "  2. Open a project in your agent"
echo "  3. Review the integration states above; automatic capture runs only for active automatic entries"
echo ""
echo "Useful commands:"
echo "  uv run python scripts/search_memory.py 'your query'  # search vault"
echo "  uv run python scripts/build_advisory.py              # proactive advisory"
echo "  uv run python scripts/build_guardrails.py             # learned rules"
echo "  uv run python benchmark/run_benchmark.py              # run benchmark"
echo ""
echo "MCP baseline: 12 local task-shaped tools (installed)"
echo "Optional enhancements:"
echo "  uv sync --locked --no-default-groups --inexact --extra hybrid"
echo "  uv sync --locked --no-default-groups --inexact --extra code-graph"
echo "  uv sync --locked --no-default-groups --inexact --extra reranker"
echo ""
