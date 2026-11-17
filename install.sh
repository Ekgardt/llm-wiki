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
#   1. Checks prerequisites (Python 3.14, uv, git)
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
AGENTS_STOPPED=0
REPLACE_CODEX_MCP=0
CODEX_MCP_STATE=unverified
SCHEDULER_MODE=native
PENDING_OPTION=""
INSTALLER_SOURCE="${BASH_SOURCE[0]:-}"
COMPILE_BACKUP_IMAGE=""
# `--model <name>`: the memory model, checked with one short call; without it the
# installer asks on a terminal (docs/research/2026-09-29-the-installer-asks-which-model.md).
MODEL_ARGS=()
# `--adopt <resource-id>`: the operator takes over a file changed outside the installer as it
# is now; see docs/research/2026-09-28-a-rollback-undoes-only-what-it-did.md.
ADOPT_ARGS=()

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# This installer runs on bash 3.2, the shell macOS ships: no `mapfile` (bash 4.0), and a
# possibly empty array is expanded in the `+` form (see IDE_HOOK_ARGS) because a bare
# expansion under `set -u` is an error before bash 4.4.
# See docs/research/2026-09-17-the-installer-runs-on-the-bash-macos-ships.md.

take_option_value() {
  case "$1" in
    --scheduler) SCHEDULER_MODE="$2" ;;
    --adopt) ADOPT_ARGS+=(--adopt "$2") ;;
    --model) MODEL_ARGS=(--model "$2") ;;
    --compile-backup-image) COMPILE_BACKUP_IMAGE="$2" ;;
  esac
}

set_installer_flag() {
  case "$1" in
    --protect-push) PROTECT_PUSH=1 ;;
    --confirm-all-agents-stopped) AGENTS_STOPPED=1 ;;
    --replace-codex-mcp) REPLACE_CODEX_MCP=1 ;;
    *) return 1 ;;
  esac
}

read_installer_argument() {
  local argument="$1"
  if [[ -n "$PENDING_OPTION" ]]; then
    take_option_value "$PENDING_OPTION" "$argument"
    PENDING_OPTION=""
    return
  fi
  set_installer_flag "$argument" && return
  case "$argument" in
    --scheduler|--adopt|--model|--compile-backup-image) PENDING_OPTION="$argument" ;;
    --scheduler=*|--adopt=*|--model=*|--compile-backup-image=*)
      take_option_value "${argument%%=*}" "${argument#*=}" ;;
    *) fail "Unknown installer argument: $argument" ;;
  esac
}

parse_installer_arguments() {
  local argument
  for argument in "$@"; do
    read_installer_argument "$argument"
  done
}

missing_value_message() {
  case "$1" in
    --scheduler) echo "--scheduler requires native or cron" ;;
    --adopt) echo "--adopt requires a resource id" ;;
    --model) echo "--model requires a model name" ;;
    *) echo "--compile-backup-image requires an external image directory" ;;
  esac
}

validate_installer_arguments() {
  [[ -z "$PENDING_OPTION" ]] || fail "$(missing_value_message "$PENDING_OPTION")"
  [[ -z "$COMPILE_BACKUP_IMAGE" || "$AGENTS_STOPPED" -eq 1 ]] || \
    fail "--compile-backup-image requires --confirm-all-agents-stopped"
  case "$SCHEDULER_MODE" in
    native|cron) ;;
    *) fail "--scheduler requires native or cron" ;;
  esac
}


# One remote's push URLs cleared; `git config --get-all` exits 1 when there are none.
clear_push_urls() {
  local remote="$1" status=0
  git -C "$VAULT_ROOT" config --get-all "remote.$remote.pushurl" >/dev/null 2>&1 || status=$?
  [[ "$status" -ne 1 ]] || return 0
  [[ "$status" -eq 0 ]] || fail "Could not inspect push URLs for remote $remote"
  git -C "$VAULT_ROOT" config --unset-all "remote.$remote.pushurl" || \
    fail "Could not clear push URLs for remote $remote"
}

protect_remote_push_url() {
  local remote="$1" urls
  clear_push_urls "$remote"
  git -C "$VAULT_ROOT" config --add "remote.$remote.pushurl" no-push || \
    fail "Could not protect push URLs for remote $remote"
  urls="$(git -C "$VAULT_ROOT" remote get-url --all --push "$remote")" || \
    fail "Could not verify push URLs for remote $remote"
  [[ "$urls" == "no-push" ]] || fail "Could not protect push URLs for remote $remote"
}

protect_push_urls() {
  local remote remotes
  remotes="$(git -C "$VAULT_ROOT" remote)" || fail "Could not enumerate Git remotes"
  while IFS= read -r remote; do
    [[ -n "$remote" ]] || continue
    protect_remote_push_url "$remote"
  done <<< "$remotes"
}

protect_push_urls_if_authorized() {
  if [[ "$INSTALLER_CREATED_CLONE" == "1" || "$PROTECT_PUSH" == "1" ]]; then
    protect_push_urls
  fi
}

codex_inline_hooks_state() {
  # Asked before the ownership transaction, and it writes nothing: the
  # transaction may own hooks.json only when the inline configuration neither
  # disables the feature, already carries our handlers, nor contradicts them.
  local vault_root="$1"
  local codex_dir="$2"
  uv run --locked --no-sync --directory "$vault_root" python "$vault_root/scripts/codex_memory.py" \
    hooks-state \
    --source "$vault_root/integrations/codex/hooks.json" \
    --config "$codex_dir/config.toml" 2>/dev/null || echo "unknown"
}

write_codex_mcp_block() {
  local block="$1" config="$2"
  if [ ! -f "$config" ]; then
    printf '%s\n' "$block" > "$config"
    return
  fi
  cp -p "$config" "$config.bak" || return 1
  if [ -s "$config" ]; then
    printf '\n%s\n' "$block" >> "$config"
    return
  fi
  printf '%s\n' "$block" >> "$config"
}

add_codex_mcp_block() {
  local vault_root="$1" config="$2"
  local block
  mkdir -p "$(dirname "$config")" || return 1
  block="$(uv run --locked --no-sync --directory "$vault_root" python scripts/codex_memory.py \
    config-block --vault-root "$vault_root")" || return 1
  write_codex_mcp_block "$block" "$config"
}

codex_mcp_state_status() {
  case "$1" in
    equivalent|replaced) return 0 ;;
    *) return 2 ;;
  esac
}

# This product's own earlier entry (`stale`) is rewritten to this vault's; an entry the
# operator wrote is replaced only with --replace-codex-mcp. Either way the previous file
# is kept as a verified preimage beside config.toml.
# See docs/research/2026-09-28-a-check-names-its-cause.md.
replace_codex_mcp() {
  local vault_root="$1" config="$2" foreign=()
  [ "${REPLACE_CODEX_MCP:-0}" -eq 1 ] && foreign=(--foreign)
  uv run --locked --no-sync --directory "$vault_root" python "$vault_root/scripts/codex_memory.py" \
    config-replace --config "$config" --vault-root "$vault_root" ${foreign[@]+"${foreign[@]}"}
}

configure_codex_mcp() {
  local vault_root="$1"
  local config="$2"
  local state
  state="$(uv run --locked --no-sync --directory "$vault_root" python "$vault_root/scripts/codex_memory.py" \
    config-state --config "$config" --vault-root "$vault_root")" || return 1
  if [ "$state" = absent ]; then
    add_codex_mcp_block "$vault_root" "$config"
    return
  fi
  case "$state" in
    stale|conflict) state="$(replace_codex_mcp "$vault_root" "$config")" || return 1 ;;
  esac
  CODEX_MCP_STATE="$state"
  codex_mcp_state_status "$state"
}

# The status line used to say "active automatic" whatever happened to the MCP
# entry, and an entry pointing at another vault passed a plain grep. The file is
# only read: it is Claude Code's live state and is never rewritten in place.
# install.ps1 asks the same helper, so both installers read it alike.
claude_mcp_state() {
  local config="$1" vault_root="$2"
  uv run --locked --no-sync --directory "$vault_root" python "$vault_root/scripts/installer_config.py" \
    claude-mcp-state --config "$config" --vault-root "$vault_root" 2>/dev/null || echo unreadable
}

claude_status_line() {
  case "$1" in
    current) echo "Claude Code: active automatic" ;;
    elsewhere) echo "Claude Code: hooks active; MCP entry points at another vault" ;;
    *) echo "Claude Code: hooks active; MCP server not registered" ;;
  esac
}

# What the nightly update does with this checkout, in the words of the code that
# does it: it follows only the default branch, and skips a detached head. A remote
# bootstrap is no longer detached (see below).
code_update_note() {
  uv run --locked --no-sync --directory "$1" python "$1/scripts/self_update.py" --note "$1" 2>/dev/null \
    || echo "unknown - the nightly update target could not be read"
}

# A failed fetch used to leave the directory `git init` had made, and the next
# attempt stopped at "already exists" with no way forward. The directory is ours
# alone here — the caller checked that it did not exist — so a failure takes it back.
#
# The pin decides what runs first, not what runs forever: the verified commit becomes the
# local default branch with the remote one as its upstream, so the nightly fast-forward
# reaches this vault as it reaches a cloned one. `git checkout --detach` freezes it again.
# See docs/research/2026-09-17-a-verified-first-install-then-follows-main.md.
pinned_fetch() {
  local target="$1" url="$2" commit="$3"
  git init "$target" \
    && git -C "$target" remote add origin "$url" \
    && git -C "$target" fetch --depth 1 origin "$commit"
}
pinned_branch() {
  local target="$1" commit="$2" branch="$3"
  git -C "$target" checkout -B "$branch" "$commit" \
    && git -C "$target" config "branch.$branch.remote" origin \
    && git -C "$target" config "branch.$branch.merge" "refs/heads/$branch"
}
fetch_pinned_checkout() {
  local target="$1" url="$2" commit="$3" branch="${4:-main}"
  if pinned_fetch "$target" "$url" "$commit" && pinned_branch "$target" "$commit" "$branch"; then
    return 0
  fi
  rm -rf -- "$target"
  return 1
}

existing_target_advice() {
  local target="$1"
  if [ -f "$target/install.sh" ] && [ -f "$target/pyproject.toml" ]; then
    echo "Remote install target already exists: $target. It holds a checkout; continue from it: bash \"$target/install.sh\""
    return 0
  fi
  echo "Remote install target already exists: $target. It is not an LLM-Wiki checkout; move it away and run the same command again"
}

# ─── 1. Resolve vault root ──────────────────────────────────────────

installer_script_dir() {
  [[ -n "$INSTALLER_SOURCE" && -f "$INSTALLER_SOURCE" ]] || return 0
  cd "$(dirname "$INSTALLER_SOURCE")" && pwd -P
}

use_local_checkout() {
  VAULT_ROOT="${LLM_WIKI_ROOT:-$SCRIPT_DIR}"
  REQUESTED_ROOT="$(cd "$VAULT_ROOT" 2>/dev/null && pwd -P)" || \
    fail "LLM_WIKI_ROOT does not identify an accessible checkout."
  [[ "$REQUESTED_ROOT" == "$SCRIPT_DIR" ]] || \
    fail "LLM_WIKI_ROOT points to a different checkout than this installer."
  VAULT_ROOT="$REQUESTED_ROOT"
}

verify_bootstrapped_checkout() {
  local required
  [[ "$(git -C "$VAULT_ROOT" rev-parse HEAD)" == "$LLM_WIKI_COMMIT_NORMALIZED" ]] || \
    fail "Checked-out commit does not match LLM_WIKI_COMMIT"
  [[ "$(git -C "$VAULT_ROOT" remote get-url origin)" == "$REPOSITORY_URL" ]] || \
    fail "Installed checkout repository identity does not match LLM-Wiki"
  for required in pyproject.toml uv.lock install.sh install.ps1 scripts/installer_config.py scripts/install_control.py; do
    [[ -f "$VAULT_ROOT/$required" ]] || fail "Installed checkout is missing $required"
  done
}

bootstrap_remote_checkout() {
  [[ "${LLM_WIKI_COMMIT:-}" =~ ^[0-9a-fA-F]{40}$ ]] || \
    fail "Remote bootstrap requires LLM_WIKI_COMMIT as a full 40-hex commit OID"
  LLM_WIKI_COMMIT_NORMALIZED="$(printf '%s' "$LLM_WIKI_COMMIT" | tr 'ABCDEF' 'abcdef')"
  INSTALL_DIR="$HOME/LLM-wiki"
  [[ ! -e "$INSTALL_DIR" ]] || fail "$(existing_target_advice "$INSTALL_DIR")"
  fetch_pinned_checkout "$INSTALL_DIR" "$REPOSITORY_URL" "$LLM_WIKI_COMMIT_NORMALIZED" || \
    fail "Could not fetch $LLM_WIKI_COMMIT_NORMALIZED; nothing was left behind, so the same command can be run again"
  VAULT_ROOT="$(cd "$INSTALL_DIR" && pwd -P)"
  INSTALLER_CREATED_CLONE=1
  verify_bootstrapped_checkout
  export LLM_WIKI_ROOT="$VAULT_ROOT"
  export LLM_WIKI_INSTALLER_CREATED_CLONE=1
  exec bash "$VAULT_ROOT/install.sh" "$@"
}

resolve_vault_root() {
  SCRIPT_DIR="$(installer_script_dir)"
  if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    use_local_checkout
    return
  fi
  bootstrap_remote_checkout "$@"
}

resolve_state_root() {
  INHERITED_STATE_ROOT="${LLM_WIKI_STATE_ROOT:-}"
  STATE_ROOT_INPUT="${LLM_WIKI_STATE_ROOT:-$VAULT_ROOT}"
  mkdir -p "$STATE_ROOT_INPUT"
  STATE_ROOT="$(cd "$STATE_ROOT_INPUT" && pwd -P)"
  export LLM_WIKI_ROOT="$VAULT_ROOT"
  export LLM_WIKI_STATE_ROOT="$STATE_ROOT"
}

# Detect shell profile before any child process observes the installation roots.
detect_shell_profile() {
  if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
    PROFILE="${HOME}/.zshrc"
    return
  fi
  if [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
    PROFILE="${HOME}/.bashrc"
    return
  fi
  PROFILE="${HOME}/.profile"
}

announce_roots() {
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
}

# ─── 2. Check prerequisites ────────────────────────────────────────

check_uv() {
  if ! command -v uv &>/dev/null; then
    fail "uv is required at version ${UV_VERSION}. Install it from https://astral.sh/uv/${UV_VERSION}/install.sh and rerun the installer."
  fi
  installedUvVersion="$(uv --version | awk '{print $2}')"
  if [ "$installedUvVersion" != "$UV_VERSION" ]; then
    fail "uv is required at version ${UV_VERSION}, found ${installedUvVersion}. Upgrade it and rerun: standalone installs use 'uv self update ${UV_VERSION}'; pip or uv-tool installs use 'uv tool install \"uv==${UV_VERSION}\" --force' (or 'pip install uv==${UV_VERSION}')."
  fi
  ok "uv ${installedUvVersion}"
}

# One interpreter series for bootstrap and the project environment.
select_bootstrap_python() {
  if ! BOOTSTRAP_PYTHON="$(uv python find 3.14)"; then
    fail "Python 3.14 is required. Run: uv python install 3.14"
  fi
  ok "Python $("$BOOTSTRAP_PYTHON" --version)"
}

check_prerequisites() {
  info "Checking prerequisites..."
  if ! command -v git &>/dev/null; then
    fail "git is required but not installed."
  fi
  ok "git $(git --version)"
  check_uv
  select_bootstrap_python
}

# ─── 3. Install dependencies ───────────────────────────────────────

install_dependencies() {
  info "Installing locked production dependencies..."
  SYNC_PLAN="$("$BOOTSTRAP_PYTHON" "$VAULT_ROOT/scripts/installer_config.py" sync-args \
    --root "$VAULT_ROOT" --environment "${UV_PROJECT_ENVIRONMENT:-}")"
  PROJECT_ENVIRONMENT="$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.loads(sys.argv[1])["environment"])' "$SYNC_PLAN")"
  SYNC_ARGS=()
  while IFS= read -r sync_argument; do
    SYNC_ARGS+=("$sync_argument")
  done < <("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(*json.loads(sys.argv[1])["arguments"], sep="\n")' "$SYNC_PLAN")
  IGNORED_ENVIRONMENT="$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.loads(sys.argv[1])["ignored_environment"] or "")' "$SYNC_PLAN")"
  if [ -n "$IGNORED_ENVIRONMENT" ]; then
    # Timers, hooks and the MCP server run the vault from its own .venv (audit B-26).
    warn "UV_PROJECT_ENVIRONMENT=$IGNORED_ENVIRONMENT is not used: the vault runs from $PROJECT_ENVIRONMENT"
  fi
  export UV_PROJECT_ENVIRONMENT="$PROJECT_ENVIRONMENT"
  uv "${SYNC_ARGS[@]}"
  ok "Production dependencies installed (MCP included)"
}

# ─── 4. Run production smoke ───────────────────────────────────────

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
# A signal to a target that may already be gone: its absence is not a cleanup failure.
send_signal() {
  if kill -s "$1" "${@:2}" 2>/dev/null; then :; fi
}
test_group_is_own() {
  case "$testPgid" in
    ""|*[!0-9]*|0|1|"$$") return 1 ;;
  esac
  return 0
}
stop_test_group() {
  local attempt=0
  send_signal TERM -- "-$testPgid"
  send_signal CONT -- "-$testPgid"
  while [ "$attempt" -lt 5 ] && test_tree_alive; do
    sleep 0.1
    attempt=$((attempt + 1))
  done
  if test_tree_alive; then
    send_signal KILL -- "-$testPgid"
  fi
}
stop_test_process() {
  send_signal TERM "$testPid"
  send_signal CONT "$testPid"
}
stop_live_test_tree() {
  if ! test_tree_alive; then return; fi
  if test_group_is_own; then stop_test_group; else stop_test_process; fi
}
stop_test_child() {
  if [ -z "$testPid" ]; then
    return
  fi
  stop_live_test_tree
  if wait "$testPid" 2>/dev/null; then :; fi
  testPid=""
  testPgid=""
}
stop_test_timer() {
  if [ -z "$testTimerPid" ]; then
    return
  fi
  case "$testTimerPid" in
    *[!0-9]*|0|1|"$$") send_signal TERM "$testTimerPid" ;;
    *) send_signal TERM -- "-$testTimerPid"; send_signal CONT -- "-$testTimerPid" ;;
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
  uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds "$smokeDeadlineSeconds" \
    --report "$STATE_ROOT/logs/install-smoke.json" &
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
  # Stop the child before reaping the timer, never the other way round.
  # `stop_test_timer` ends in `wait` on the timer job, and this handler runs
  # inside a trap that interrupted the outer `wait` on the child. In that
  # state the shell has not yet reaped the timer even though the timer has
  # already exited: measured under load, `ps` and `kill -0` both reported the
  # timer gone while `jobs -l` still called it Running, so `wait` on it
  # blocked until some other child changed state — and the only other child
  # was the very process this handler exists to kill. The escalation below
  # then ran 45s past the deadline, after the smoke child had finished on its
  # own. Killing first makes the promise that an overrunning run leaves
  # nothing behind independent of when the shell gets around to the timer.
  stop_test_child
  stop_test_timer
}
handle_test_signal() {
  local status="$1"
  # USR1 is ignored rather than defaulted for the duration of the cleanup.
  # Defaulting it would terminate the installer if the still-running timer
  # fired while the child was being stopped, which is exactly the window the
  # reordering below opens; ignoring it closes that window instead.
  trap - HUP INT TERM
  trap '' USR1
  # Same order and same reason as handle_test_timeout: the child first,
  # because `stop_test_timer` ends in a `wait` that this handler cannot rely
  # on returning promptly, and a signalled installer must not leave a running
  # smoke behind while it waits for bookkeeping.
  stop_test_child
  stop_test_timer
  restore_test_monitor_mode
  exit "$status"
}
read_smoke_bound() {
  testTimeoutSeconds="${LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS:-180}"
  case "$testTimeoutSeconds" in
    ""|*[!0-9]*|0) fail "LLM_WIKI_INSTALL_SMOKE_TIMEOUT_SECONDS must be a positive integer" ;;
  esac
  # The smoke's own deadline is two thirds of the bound, rounded up: 120 s of the
  # default 180, leaving the rest for it to report and exit before it is stopped.
  # A fixed 120 meant a longer bound could never give the smoke longer.
  smokeDeadlineSeconds=$(( (testTimeoutSeconds * 2 + 2) / 3 ))
}
report_smoke_outcome() {
  if [ "$testTimedOut" -eq 1 ]; then
    fail "Production smoke timed out after ${testTimeoutSeconds}s; installation aborted"
  fi
  if [ "$testExit" -ne 0 ]; then
    fail "Production smoke failed; installation aborted"
  fi
  ok "Production smoke passed"
}
run_production_smoke() {
  info "Running production smoke..."
  read_smoke_bound
  testPid=""
  testPgid=""
  testTimerPid=""
  testTimedOut=0
  testMonitorMode=""
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
  report_smoke_outcome
}

# ─── 5. Set environment variables ──────────────────────────────────

# External configuration starts only after the mandatory test gate.
prepare_runtime_directories() {
  protect_push_urls_if_authorized
  if [[ "$INSTALLER_CREATED_CLONE" == "1" || "$PROTECT_PUSH" == "1" ]]; then
    ok "Push disabled (no-push) for every configured remote"
  fi
  # Create runtime dirs inside the vault (gitignored)
  mkdir -p "$STATE_ROOT/run" "$STATE_ROOT/logs" "$STATE_ROOT/cache"
  ok "Runtime dirs: $STATE_ROOT/{run,logs,cache} (gitignored)"
}

# Detected here rather than in step 7 so the plugin is written by the ownership
# transaction: an uninstall has to be able to take back exactly what it wrote.
detect_opencode_plugin() {
  OPENCODE_PLUGIN=0
  if [ -d "$HOME/.config/opencode" ] || command -v opencode &>/dev/null; then
    OPENCODE_PLUGIN=1
  fi
}
# Same reason: Claude's user settings were merged by a separate script in step 7,
# so an uninstall left our hooks in settings.json pointing at a vault that was
# gone. The transaction owns our hook blocks and the two env keys now.
detect_claude_settings() {
  CLAUDE_SETTINGS=0
  if command -v claude &>/dev/null || [ -d "$HOME/.claude" ] || [ -f "$HOME/.claude.json" ]; then
    CLAUDE_SETTINGS=1
  fi
}
# Same reason again: Codex hooks were merged in step 7 by a separate command, so
# an uninstall never took them back. The transaction owns hooks.json when the
# inline configuration leaves that free.
detect_codex_hooks() {
  CODEX_HOOKS_STATE="none"
  CODEX_HOOKS_OWNED=0
  if command -v codex &>/dev/null || [ -d "$HOME/.codex" ]; then
    mkdir -p "$HOME/.codex"
    CODEX_HOOKS_STATE="$(codex_inline_hooks_state "$VAULT_ROOT" "$HOME/.codex")"
    if [ "$CODEX_HOOKS_STATE" = "absent" ]; then
      CODEX_HOOKS_OWNED=1
    fi
  fi
}
add_ide_hook_argument() {
  if [ "$1" -eq 1 ]; then
    IDE_HOOK_ARGS+=("$2")
  fi
}
detect_owned_integrations() {
  detect_opencode_plugin
  detect_claude_settings
  detect_codex_hooks
  IDE_HOOK_ARGS=()
  add_ide_hook_argument "$OPENCODE_PLUGIN" --opencode-plugin
  add_ide_hook_argument "$CLAUDE_SETTINGS" --claude-settings
  add_ide_hook_argument "$CODEX_HOOKS_OWNED" --codex-hooks
}

# ─── 5a. Choose the memory model ───────────────────────────────────

# The chooser saves one vault setting; hooks and schedulers read it at runtime.
choose_memory_model() {
  info "Choosing the model the memory pipeline calls..."
  MODEL_CHOICE="$(uv run --locked --no-sync --directory "$VAULT_ROOT" python \
    "$VAULT_ROOT/scripts/choose_model.py" --home "$HOME" \
    ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"})" || fail "Model choice failed"
  MODEL_VALUE="$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.loads(sys.argv[1])["model"])' "$MODEL_CHOICE")"
  if [ -n "$MODEL_VALUE" ]; then
    ok "Memory model saved in llm-wiki.toml: $MODEL_VALUE"
  fi
  MODEL_REASON="$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.loads(sys.argv[1]).get("reason", ""))' "$MODEL_CHOICE")"
  if [ -n "$MODEL_REASON" ]; then
    warn "The memory model was not changed: $MODEL_REASON"
  fi
}

# ─── 6. Set up scheduled maintenance ────────────────────────────────

set_up_maintenance() {
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
    ${IDE_HOOK_ARGS[@]+"${IDE_HOOK_ARGS[@]}"} \
    ${ADOPT_ARGS[@]+"${ADOPT_ARGS[@]}"})" || fail "Install ownership transaction failed"
  SCHEDULER_BACKEND="$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.loads(sys.argv[1])["scheduler_backend"])' "$INSTALL_CONTROL_RESULT")"
  case "$SCHEDULER_BACKEND" in
    launchd|systemd_user|cron) ;;
    *) fail "Install control returned an invalid scheduler backend" ;;
  esac
  ok "Environment roots and $SCHEDULER_BACKEND maintenance are verified"
}

# ─── 7. Detect and wire up agents ──────────────────────────────────

opencode_is_active() {
  AGENT_STATUSES+=("OpenCode: active automatic")
  ok "OpenCode configuration is active"
  uv run --locked --no-sync --directory "$VAULT_ROOT" python \
    "$VAULT_ROOT/scripts/session_start_context.py" \
    --output-file "$STATE_ROOT/cache/session-context.md" 2>/dev/null || true
}

# OpenCode configuration is merged structurally and verified after all precedence layers.
wire_up_opencode() {
  OPENCODE_RESULT="$(uv run --locked --no-sync --directory "$VAULT_ROOT" python \
    "$VAULT_ROOT/scripts/installer_config.py" opencode \
    --root "$VAULT_ROOT" --state-root "$STATE_ROOT" --cwd "$CALLER_CWD")"
  OPENCODE_STATUS="$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.loads(sys.argv[1])["status"])' "$OPENCODE_RESULT")"
  case "$OPENCODE_STATUS" in
    active) opencode_is_active ;;
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
}

# Codex CLI
configure_codex_mcp_or_advise() {
  if configure_codex_mcp "$VAULT_ROOT" "$CODEX_CONFIG"; then
    CODEX_MCP_READY=1
    ok "Codex MCP config verified: $CODEX_CONFIG"
  else
    mcp_exit=$?
    # The helper's own line for the state the entry was left in; none asks for a manual merge.
    [ "$mcp_exit" -eq 2 ] || CODEX_MCP_STATE=unverified
    warn "$(uv run --locked --no-sync --directory "$VAULT_ROOT" python "$VAULT_ROOT/scripts/codex_memory.py" \
      config-advice --state "$CODEX_MCP_STATE")"
  fi
}

report_codex_hooks() {
  CODEX_HOOKS="$HOME/.codex/hooks.json"
  case "$CODEX_HOOKS_STATE" in
    absent)
      CODEX_HOOKS_READY=1
      ok "Codex official hooks owned by the install transaction: $CODEX_HOOKS"
      info "Open /hooks in Codex to review and trust the LLM-Wiki commands."
      ;;
    equivalent)
      CODEX_HOOKS_READY=1
      ok "Equivalent LLM-Wiki hooks are already configured inline; hooks.json was not changed."
      info "Open /hooks in Codex to review and trust the inline LLM-Wiki commands."
      ;;
    conflict)
      warn "Active inline Codex hooks require manual merge and /hooks trust review; hooks.json was not changed."
      ;;
    disabled)
      warn "Codex lifecycle hooks are disabled. Set [features] hooks = true in config.toml and rerun the installer; hooks.json was not changed."
      ;;
    *)
      warn "Codex hooks were not changed: the existing $CODEX_HOOKS state is '${CODEX_HOOKS_STATE}'. Compare it with integrations/codex/hooks.json and merge the LLM-Wiki entries by hand, then open /hooks in Codex to trust them."
      ;;
  esac
}

record_codex_status() {
  if [ "$CODEX_MCP_READY" -eq 1 ] && [ "$CODEX_HOOKS_READY" -eq 1 ]; then
    AGENT_STATUSES+=("Codex: manual /hooks trust review required")
  else
    AGENT_STATUSES+=("Codex: conflict or unverified")
  fi
}

wire_up_codex() {
  command -v codex &>/dev/null || return 0
  CODEX_MCP_READY=0
  CODEX_HOOKS_READY=0
  CODEX_CONFIG="$HOME/.codex/config.toml"
  mkdir -p "$HOME/.codex"
  configure_codex_mcp_or_advise
  report_codex_hooks
  record_codex_status
}

# Claude Code — hooks and env are owned by the install transaction (step 6)
write_claude_mcp_file() {
  info "Adding MCP server config for Claude Code..."
  printf '%s\n' '{"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--locked","--no-sync","--directory",'"$VAULT_JSON"',"python","scripts/mcp_server.py"]}}}' > "$CLAUDE_MCP"
  CLAUDE_MCP_STATE="current"
  ok "Claude MCP config: ~/.claude.json"
}

# `~/.claude.json` is Claude Code's live state file and it writes to it
# while running, so this must not read-modify-write it. Its own CLI adds
# the entry safely; without the CLI the only honest option is to say what
# to add.
add_claude_mcp_with_cli() {
  if command -v claude &>/dev/null && claude mcp add --scope user llm-wiki \
      -- uv run --locked --no-sync --directory "$VAULT_ROOT" python scripts/mcp_server.py \
      >/dev/null 2>&1; then
    CLAUDE_MCP_STATE="current"
    ok "Claude MCP server registered: llm-wiki"
  else
    warn "Existing ~/.claude.json found without llm-wiki; add it with:"
    warn "  claude mcp add --scope user llm-wiki -- uv run --locked --no-sync --directory $VAULT_ROOT python scripts/mcp_server.py"
  fi
}

register_claude_mcp() {
  case "$CLAUDE_MCP_STATE" in
    missing) write_claude_mcp_file ;;
    absent) add_claude_mcp_with_cli ;;
    elsewhere)
      warn "The llm-wiki MCP entry in ~/.claude.json points at another vault; replace it with:"
      warn "  claude mcp remove --scope user llm-wiki"
      warn "  claude mcp add --scope user llm-wiki -- uv run --locked --no-sync --directory $VAULT_ROOT python scripts/mcp_server.py"
      ;;
    unreadable)
      warn "The file ~/.claude.json could not be read as JSON, so llm-wiki was not registered; once it reads, add it with:"
      warn "  claude mcp add --scope user llm-wiki -- uv run --locked --no-sync --directory $VAULT_ROOT python scripts/mcp_server.py"
      ;;
  esac
}

wire_up_claude() {
  [ "$CLAUDE_SETTINGS" -eq 1 ] || return 0
  ok "Claude settings owned by the install transaction → ~/.claude/settings.json"
  # v4.0: MCP server config for Claude Code
  CLAUDE_MCP="$HOME/.claude.json"
  CLAUDE_MCP_STATE="$(claude_mcp_state "$CLAUDE_MCP" "$VAULT_ROOT")"
  register_claude_mcp
  AGENT_STATUSES+=("$(claude_status_line "$CLAUDE_MCP_STATE")")
}

report_agent_statuses() {
  if [ "${#AGENT_STATUSES[@]}" -eq 0 ]; then
    warn "No supported agents detected. Install Claude Code, OpenCode, or Codex CLI."
  else
    ok "Agent integrations:"
    printf '  - %s\n' "${AGENT_STATUSES[@]}"
  fi
}

wire_up_agents() {
  info "Detecting installed agents..."
  AGENT_STATUSES=()
  VAULT_JSON=$("$BOOTSTRAP_PYTHON" -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$VAULT_ROOT")
  wire_up_opencode
  wire_up_codex
  wire_up_claude
  report_agent_statuses
}

# ─── 8. Reliability V3 adoption ────────────────────────────────────
# Session capture writes through the V3 queue, and a vault that has not
# adopted V3 refuses every capture with `legacy_protocol_unquiesced` (issue
# #17). A fresh vault, or one whose legacy pair the check finds quiescent,
# is adopted here; any other state is named with the command to run.
#
# This comes before the runtime sync: that sync opens the pre-adoption
# coordinator, which leaves half a legacy pair and a vault the cutover can no
# longer adopt. A real end-to-end install finished with capture disabled that
# way. See docs/research/2026-09-17-the-queue-is-adopted-before-anything-writes-to-it.md.
# The check exits 1 for a fresh or upgrade-required vault, because both are
# reported as degraded. Under `pipefail` a fallback written after the pipe
# fired even though the parser had already answered, and the state became two
# lines that matched no branch: no fresh install ever adopted. What the check
# printed and how it exited are read apart here; only unparsable output is
# `unknown`. See docs/research/2026-09-17-a-fresh-install-adopts-the-queue.md.
adoption_state_of() {
  local report
  report="$(uv run --locked --no-sync python "$VAULT_ROOT/scripts/repair_installed_memory.py" --check --json 2>>"$ADOPTION_ERR" || true)"
  printf '%s' "$report" | "$BOOTSTRAP_PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("details", {}).get("adoption_state", "unknown"))' 2>>"$ADOPTION_ERR" || echo unknown
}
adoption_tail() {
  if [ -s "$ADOPTION_ERR" ]; then
    warn "  last lines of $ADOPTION_ERR:"
    tail -n 5 "$ADOPTION_ERR" | sed 's/^/    /' >&2
  fi
}
# `--confirm-all-agents-stopped` is the operator's statement, and the installer makes it
# only where it can see that it is true: a `fresh` vault holds no legacy database an agent
# could be writing. A vault that holds them (`upgrade-required`, or a `partial` cutover,
# which the repair resumes) is adopted only when the operator said so to the installer.
# See docs/research/2026-09-17-the-installer-does-not-vouch-for-agents-it-cannot-see.md.
adoption_plan() {
  local state="$1" confirmed="$2"
  case "$state" in
    adopted) echo adopted ;;
    fresh) echo adopt ;;
    upgrade-required|partial)
      if [ "$confirmed" -eq 1 ]; then echo adopt; else echo ask; fi
      ;;
    *) echo unknown ;;
  esac
}
adoption_ask() {
  SYNC_WARNING=1
  warn "Reliability V3 state is '${ADOPTION_STATE}': this vault holds the earlier queue, and the installer cannot see whether an agent is using it."
  warn "Close every agent session, then either rerun the installer with --confirm-all-agents-stopped or run:"
  warn "  $ADOPT_COMMAND"
  warn "Session capture stays disabled until then."
}
# The report on standard output names the reason of a failure, so it is kept too.
adoption_apply() {
  if uv run --locked --no-sync python "$VAULT_ROOT/scripts/repair_installed_memory.py" --apply --adopt-ownership-v3 --confirm-all-agents-stopped >>"$ADOPTION_ERR" 2>&1; then
    ok "Reliability V3 adopted (was ${ADOPTION_STATE}); session capture is enabled"
  else
    SYNC_WARNING=1
    warn "Reliability V3 adoption did not complete; session capture stays disabled until it does:"
    warn "  $ADOPT_COMMAND"
    adoption_tail
  fi
}
# The check's own line says what the state means; the installer claims no more.
# See docs/research/2026-09-28-a-check-names-its-cause.md.
adoption_unknown() {
  SYNC_WARNING=1
  warn "$(uv run --locked --no-sync python "$VAULT_ROOT/scripts/repair_installed_memory.py" --check --summary 2>>"$ADOPTION_ERR" || true)"
  warn "  details: uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json"
  adoption_tail
}
adopt_reliability_v3() {
  SYNC_WARNING=0
  info "Checking Reliability V3 adoption..."
  # Whatever the check or the adoption says on stderr lands in one log and its
  # tail is quoted on failure, so the user never has to rerun a command blind
  # (audit OPS-20, docs/research/2026-09-11-a-skipped-night-and-a-failed-adoption-say-why.md).
  ADOPTION_ERR="$STATE_ROOT/logs/install-adoption.err.log"
  mkdir -p "$STATE_ROOT/logs"
  : > "$ADOPTION_ERR"
  ADOPTION_STATE="$(adoption_state_of)"
  ADOPT_COMMAND="uv run --locked --no-sync python scripts/repair_installed_memory.py --apply --adopt-ownership-v3 --confirm-all-agents-stopped"
  case "$(adoption_plan "$ADOPTION_STATE" "$AGENTS_STOPPED")" in
    adopted) ok "Reliability V3 adopted" ;;
    ask) adoption_ask ;;
    adopt) adoption_apply ;;
    *) adoption_unknown ;;
  esac
}

compile_progress_state_of() {
  local report
  report="$(uv run --locked --no-sync python "$VAULT_ROOT/scripts/repair_installed_memory.py" --check --json 2>>"$ADOPTION_ERR" || true)"
  printf '%s' "$report" | "$BOOTSTRAP_PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("details", {}).get("compile_progress", "unknown"))' 2>>"$ADOPTION_ERR" || echo unknown
}
apply_compile_progress_upgrade() {
  if uv run --locked --no-sync python "$VAULT_ROOT/scripts/repair_installed_memory.py" --apply --upgrade-compile-progress --backup-image "$COMPILE_BACKUP_IMAGE" --confirm-all-agents-stopped >>"$ADOPTION_ERR" 2>&1; then
    ok "Compile journal upgraded; retain its backup image"
    return
  fi
  SYNC_WARNING=1
  warn "Compile migration did not complete; retain the image and resume with the same arguments."
  adoption_tail
}
upgrade_compile_progress() {
  if [[ "$(compile_progress_state_of)" == upgraded ]]; then
    ok "Compile journal is ready"
    return
  fi
  if [[ -z "$COMPILE_BACKUP_IMAGE" || "$AGENTS_STOPPED" -ne 1 ]]; then
    SYNC_WARNING=1
    warn "Compilation needs an offline journal upgrade. Stop agents, then rerun with --confirm-all-agents-stopped --compile-backup-image <absolute-external-directory>."
    warn "Use an empty directory for a new migration, or its exact retained image to resume."
    return
  fi
  apply_compile_progress_upgrade
}

# ─── 8a. Pinned model weights ──────────────────────────────────────
# The read path loads weights local-only. Every pinned model whose runtime is
# installed is fetched now, verified; with none installed, nothing is expected,
# and the script's own lines say which it was.
# This comes before the runtime sync: the sync builds the first generation, whose
# vectors need these weights, and ends with the doctor check that decides whether
# the install ends with warnings. Fetched after it, the weights left the first
# generation without vectors and every fresh install warning about weights it was
# about to fetch. See docs/research/2026-09-27-the-install-checks-itself-last.md.
fetch_model_weights() {
  MODELS_EXIT=0
  uv run --locked --no-sync python "$VAULT_ROOT/scripts/install_models.py" || MODELS_EXIT=$?
  case "$MODELS_EXIT" in
    0) ok "Model weights step done" ;;
    2) info "huggingface_hub is not installed; model weights are fetched once it is" ;;
    *) warn "Model weights incomplete; run: uv run --locked --no-sync python scripts/install_models.py" ;;
  esac
}

# A managed Pyright the operator installed earlier is validated, and repaired when
# its receipt is from before the tree digest (`install_pyright.py` retires such an
# install and reinstalls the pinned release). Nothing is installed where the operator
# never installed it: that stays an explicit action.
# See docs/research/2026-09-28-a-check-names-its-cause.md.
verify_managed_pyright() {
  [ -d "$STATE_ROOT/cache/code-tools/pyright" ] || return 0
  if PYRIGHT_REPORT="$(uv run --locked --no-sync python "$VAULT_ROOT/scripts/install_pyright.py" --state-root "$STATE_ROOT" 2>&1 >/dev/null)"; then
    ok "Managed Pyright verified"
  else
    warn "Managed Pyright could not be verified or repaired: ${PYRIGHT_REPORT}"
  fi
}

# ─── 8b. Bounded runtime sync ──────────────────────────────────────

synchronize_runtime() {
  info "Synchronizing runtime state and derived indexes..."
  SYNC_EXIT=0
  # The first generation is built here (the generation is the only index since
  # 2026-09-23): 48.9 s for 189 pages with vectors on the reference vault, so the
  # sync gets fifteen minutes instead of its 30-second default.
  uv run --locked --no-sync python "$VAULT_ROOT/scripts/sync_memory.py" --apply --time-limit-seconds 900 || SYNC_EXIT=$?
  case "$SYNC_EXIT" in
    0) ok "Runtime state synchronized" ;;
    1) SYNC_WARNING=1; warn "Runtime synchronization completed with warnings" ;;
    *) fail "Runtime synchronization failed" ;;
  esac
}

# ─── 9. Print summary ─────────────────────────────────────────────

print_install_outcome() {
  if [ "$SYNC_WARNING" -eq 1 ]; then
    echo -e "${YELLOW}  LLM-Wiki installed with warnings${NC}"
    echo "  The install completed; this is not a failure (a failure stops with [FAIL] and exit 1)."
    echo "  The [WARN] lines above and the doctor line name what needs attention now."
    echo "  For the state now: uv run --locked --no-sync python scripts/doctor.py"
  else
    echo -e "${GREEN}  LLM-Wiki installed successfully!${NC}"
  fi
}

print_agent_statuses() {
  if [ "${#AGENT_STATUSES[@]}" -eq 0 ]; then
    echo "  - none detected"
  else
    printf '  - %s\n' "${AGENT_STATUSES[@]}"
  fi
}

print_summary() {
  echo ""
  echo "=============================================="
  print_install_outcome
  echo "=============================================="
  echo ""
  echo "Vault:          $VAULT_ROOT"
  echo "State:          $STATE_ROOT"
  echo "Profile:        $PROFILE"
  echo "Agent integrations:"
  print_agent_statuses
  echo "Maintenance:    $SCHEDULER_BACKEND (nightly 03:00 + weekly Sun 04:00)"
  echo "Code updates:   $(code_update_note "$VAULT_ROOT")"
  echo ""
  echo "Next steps:"
  echo "  1. Restart your terminal (to pick up env vars)"
  echo "  2. Open a project in your agent"
  echo "  3. Review the integration states above; automatic capture runs only for active automatic entries"
  echo ""
  echo "Useful commands:"
  echo "  uv run --locked --no-sync python scripts/search_memory.py 'your query'  # search vault"
  echo "  uv run --locked --no-sync python scripts/build_advisory.py              # proactive advisory"
  echo "  uv run --locked --no-sync python scripts/build_guardrails.py             # learned rules"
  echo "  uv run --locked --no-sync python benchmark/run_benchmark.py              # run benchmark"
  echo ""
  echo "MCP: 12 local task-shaped tools; every component is installed, with Pyright for Python"
  echo "Code navigation in another language (one command each, when you need it):"
  echo "  uv run --locked --no-sync python scripts/install_language_server.py --profile <typescript|gopls|rust-analyzer>"
  echo ""
}

main() {
  parse_installer_arguments "$@"
  validate_installer_arguments
  resolve_vault_root "$@"
  resolve_state_root
  detect_shell_profile
  announce_roots
  check_prerequisites
  install_dependencies
  run_production_smoke
  prepare_runtime_directories
  detect_owned_integrations
  choose_memory_model
  set_up_maintenance
  wire_up_agents
  adopt_reliability_v3
  upgrade_compile_progress
  fetch_model_weights
  verify_managed_pyright
  synchronize_runtime
  print_summary
}

main "$@"
