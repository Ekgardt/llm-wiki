#!/usr/bin/env bash
# LLM-Wiki one-command installer for macOS, Linux, and WSL2.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
#
# Or clone first:
#   git clone git@github.com:Ekgardt/llm-wiki.git && cd llm-wiki && ./install.sh
#
# NOTE: For reproducible installs, pin to a version tag:
#   curl ... https://raw.githubusercontent.com/Ekgardt/llm-wiki/v3.4.0/install.sh | bash
# The main branch URL is for development convenience only.
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs Python deps (uv sync)
#   3. Runs tests to verify everything works
#   4. Sets LLM_WIKI_ROOT in shell profile
#   5. Sets up cron jobs (nightly + weekly) — cron only (no launchd)
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

info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

resolve_opencode_config_home() {
  local candidate="${XDG_CONFIG_HOME:-}"
  if [[ -n "$candidate" ]] && [[ "$candidate" == /* ]]; then
    printf '%s' "$candidate"
  else
    printf '%s' "$HOME/.config"
  fi
}

shell_quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\\\''/g"
  printf "'"
}

cron_quote() {
  shell_quote "$1" | sed 's/%/\\%/g'
}

is_llm_wiki_checkout() {
  local root="$1"
  [[ -f "$root/pyproject.toml" ]] || return 1
  awk '
    /^\[project\][[:space:]]*$/ { in_project = 1; next }
    /^\[/ { in_project = 0 }
    in_project && /^[[:space:]]*name[[:space:]]*=[[:space:]]*"llm-wiki"[[:space:]]*$/ {
      found = 1
    }
    END { exit found ? 0 : 1 }
  ' "$root/pyproject.toml"
}

update_shell_profile() {
  local original_profile="$1"
  local vault_root="$2"
  local state_root="$3"
  local provider="$4"
  local target
  local current_target
  local target_dir
  local target_name
  local base
  local temporary
  local had_base=0

  resolve_profile_target() {
    local candidate="$1"
    local link_target
    local link_count=0
    local candidate_dir
    local candidate_name

    while [[ -L "$candidate" ]]; do
      link_count=$((link_count + 1))
      [[ "$link_count" -le 40 ]] || return 1
      link_target=$(readlink "$candidate") || return 1
      if [[ "$link_target" == /* ]]; then
        candidate="$link_target"
      else
        candidate="$(dirname "$candidate")/$link_target"
      fi
    done
    candidate_dir=$(cd -P "$(dirname "$candidate")" && pwd) || return 1
    candidate_name=$(basename "$candidate")
    printf '%s/%s' "$candidate_dir" "$candidate_name"
  }

  target=$(resolve_profile_target "$original_profile") || return 1
  target_dir=$(dirname "$target")
  target_name=$(basename "$target")

  base=$(mktemp "$target_dir/.${target_name}.llm-wiki.base.XXXXXX") || return 1
  temporary=$(mktemp "$target_dir/.${target_name}.llm-wiki.tmp.XXXXXX") || {
    rm -f "$base"
    return 1
  }
  if [[ -e "$target" ]]; then
    if [[ ! -f "$target" ]] || [[ -L "$target" ]]; then
      rm -f "$base" "$temporary"
      return 1
    fi
    had_base=1
    if ! cp -p "$target" "$base" || ! cp -p "$base" "$temporary"; then
      rm -f "$base" "$temporary"
      return 1
    fi
    if ! awk \
        '!/^export (LLM_WIKI_ROOT|LLM_WIKI_STATE_ROOT|MEMORY_LLM_PROVIDER)=/' \
        "$base" > "$temporary"; then
      rm -f "$base" "$temporary"
      return 1
    fi
  else
    : > "$temporary"
  fi
  if ! {
    printf 'export LLM_WIKI_ROOT=%s\n' "$(shell_quote "$vault_root")"
    printf 'export LLM_WIKI_STATE_ROOT=%s\n' "$(shell_quote "$state_root")"
    printf 'export MEMORY_LLM_PROVIDER=%s\n' "$(shell_quote "$provider")"
  } >> "$temporary"; then
    rm -f "$base" "$temporary"
    return 1
  fi
  current_target=$(resolve_profile_target "$original_profile") || {
    rm -f "$base" "$temporary"
    return 1
  }
  if [[ "$current_target" != "$target" ]]; then
    rm -f "$base" "$temporary"
    return 1
  fi
  if [[ "$had_base" -eq 1 ]]; then
    if ! cmp -s "$base" "$target"; then
      rm -f "$base" "$temporary"
      return 1
    fi
  elif [[ -e "$target" ]] || [[ -L "$target" ]]; then
    rm -f "$base" "$temporary"
    return 1
  fi
  if ! mv -f "$temporary" "$target"; then
    rm -f "$base" "$temporary"
    return 1
  fi
  rm -f "$base"
}

# ─── 1. Resolve vault root ──────────────────────────────────────────

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [[ -n "$SCRIPT_PATH" ]] && [[ -f "$SCRIPT_PATH" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
fi
if [[ -n "$SCRIPT_DIR" ]] && is_llm_wiki_checkout "$SCRIPT_DIR"; then
  VAULT_ROOT="$SCRIPT_DIR"
elif [[ -n "${LLM_WIKI_ROOT:-}" ]] && is_llm_wiki_checkout "$LLM_WIKI_ROOT"; then
  VAULT_ROOT="$LLM_WIKI_ROOT"
else
  VAULT_ROOT=""
fi

# If running from curl pipe, we need to clone first
if ! is_llm_wiki_checkout "$VAULT_ROOT"; then
  info "Cloning LLM-Wiki repository..."
  INSTALL_DIR="${HOME}/LLM-wiki"
  git clone --branch v3.4.0 --depth 1 https://github.com/Ekgardt/llm-wiki.git "$INSTALL_DIR"
  VAULT_ROOT="$INSTALL_DIR"
  is_llm_wiki_checkout "$VAULT_ROOT" || fail "Cloned repository is not a valid LLM-Wiki checkout"
  cd "$VAULT_ROOT"
fi

cd "$VAULT_ROOT"
info "Vault root: $VAULT_ROOT"

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

# uv (install if missing)
if ! command -v uv &>/dev/null; then
  info "Installing uv (fast Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv &>/dev/null; then
    fail "uv installation failed. Install manually: https://docs.astral.sh/uv/"
  fi
fi
ok "uv $(uv --version 2>/dev/null || echo 'installed')"

# ─── 3. Install dependencies ───────────────────────────────────────

info "Installing Python dependencies..."
if ! uv sync --locked --quiet; then
  fail "Dependency installation failed"
fi
ok "Dependencies installed"

# ─── 4. Run tests ──────────────────────────────────────────────────

info "Running test suite..."
if ! uv run pytest -q; then
  fail "Test suite failed"
fi
ok "All tests passed"

# Prevent accidental pushes only after all validation gates pass.
git -C "$VAULT_ROOT" remote set-url --push origin no-push
ok "Push disabled (no-push) — installed vault cannot push to public remote"

# ─── 5. Set environment variables ──────────────────────────────────

info "Setting environment variables..."

# Warn if env vars already point somewhere else (avoid silent clobber)
if [ -n "${LLM_WIKI_ROOT:-}" ] && [ "$LLM_WIKI_ROOT" != "$VAULT_ROOT" ]; then
  warn "LLM_WIKI_ROOT was '$LLM_WIKI_ROOT', overwriting to '$VAULT_ROOT'"
fi
if [ -n "${LLM_WIKI_STATE_ROOT:-}" ] && [ "$LLM_WIKI_STATE_ROOT" != "$VAULT_ROOT" ]; then
  warn "LLM_WIKI_STATE_ROOT was '$LLM_WIKI_STATE_ROOT', overwriting to '$VAULT_ROOT'"
fi

# Detect shell profile
if [[ -n "${ZSH_VERSION:-}" ]] || [[ "${SHELL:-}" == */zsh ]]; then
  PROFILE="${HOME}/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "${SHELL:-}" == */bash ]]; then
  PROFILE="${HOME}/.bashrc"
else
  PROFILE="${HOME}/.profile"
fi

# Runtime lives inside the vault as gitignored cache/logs/run dirs.
# LLM_WIKI_STATE_ROOT defaults to the vault itself; set it explicitly only
# if you want runtime on a different disk.
STATE_ROOT="$VAULT_ROOT"
if ! update_shell_profile \
    "$PROFILE" "$VAULT_ROOT" "$STATE_ROOT" "opencode-sdk"; then
  fail "Could not update shell profile atomically: $PROFILE"
fi
ok "Updated LLM-Wiki environment in $PROFILE"

export LLM_WIKI_ROOT="$VAULT_ROOT"
export LLM_WIKI_STATE_ROOT="$STATE_ROOT"
export MEMORY_LLM_PROVIDER="opencode-sdk"

# Create runtime dirs inside the vault (gitignored)
mkdir -p "$STATE_ROOT/run" "$STATE_ROOT/run/queue" "$STATE_ROOT/logs" "$STATE_ROOT/cache" "$STATE_ROOT/cache/cognee"
ok "Runtime dirs: $STATE_ROOT/{run,logs,cache} (gitignored)"

# ─── 6. Build search index ─────────────────────────────────────────

info "Building FTS5 search index..."
uv run python "$VAULT_ROOT/scripts/search_memory.py" --rebuild 2>/dev/null || true
ok "Search index built"

# ─── 7. Set up scheduled maintenance ────────────────────────────────

info "Setting up scheduled maintenance..."

UV_BIN="$(command -v uv)"
VAULT_ROOT_Q=$(cron_quote "$VAULT_ROOT")
STATE_ROOT_Q=$(cron_quote "$STATE_ROOT")
UV_BIN_Q=$(cron_quote "$UV_BIN")
NIGHTLY_LOG_Q=$(cron_quote "$STATE_ROOT/logs/cron-nightly.log")
WEEKLY_LOG_Q=$(cron_quote "$STATE_ROOT/logs/cron-weekly.log")
CRON_ENV="LLM_WIKI_ROOT=$VAULT_ROOT_Q LLM_WIKI_STATE_ROOT=$STATE_ROOT_Q MEMORY_LLM_PROVIDER=opencode-sdk"
CRON_NIGHTLY="0 3 * * * cd $VAULT_ROOT_Q && $CRON_ENV $UV_BIN_Q run python scripts/scheduled_nightly.py >> $NIGHTLY_LOG_Q 2>&1"
CRON_WEEKLY="0 4 * * 0 cd $VAULT_ROOT_Q && $CRON_ENV $UV_BIN_Q run python scripts/scheduled_weekly.py >> $WEEKLY_LOG_Q 2>&1"

# Remove old LLM-Wiki cron block (between markers only)
if crontab -l 2>/dev/null | grep -q "LLM-Wiki-cron-start"; then
  info "Updating existing cron entries..."
  crontab -l 2>/dev/null | sed '/# LLM-Wiki-cron-start/,/# LLM-Wiki-cron-end/d' | crontab -
fi

# Add new entries with markers
( crontab -l 2>/dev/null; echo "# LLM-Wiki-cron-start"; echo "$CRON_NIGHTLY"; echo "$CRON_WEEKLY"; echo "# LLM-Wiki-cron-end" ) | crontab -
ok "Cron scheduled: nightly 03:00, weekly Sunday 04:00"

# ─── 8. Detect and wire up agents ──────────────────────────────────

info "Detecting installed agents..."

AGENTS_FOUND=""

# OpenCode
OPENCODE_CONFIG_HOME="$(resolve_opencode_config_home)/opencode"
if [ -d "$OPENCODE_CONFIG_HOME" ] || command -v opencode &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND OpenCode"
  PLUGIN_DIR="$OPENCODE_CONFIG_HOME/plugins"
  mkdir -p "$PLUGIN_DIR"
  if [ -f "$VAULT_ROOT/scripts/llm-wiki-memory-opencode.js" ]; then
    cp -f "$VAULT_ROOT/scripts/llm-wiki-memory-opencode.js" "$PLUGIN_DIR/llm-wiki-memory.js"
    ok "OpenCode plugin installed/updated"
    # Generate initial context file so the first session has context
    mkdir -p "$STATE_ROOT/cache"
    uv run python "$VAULT_ROOT/scripts/session_start_context.py" --output-file "$STATE_ROOT/cache/session-context.md" 2>/dev/null || true
  else
    warn "OpenCode detected but plugin source missing at $VAULT_ROOT/scripts/llm-wiki-memory-opencode.js"
  fi
fi

# Codex CLI
if command -v codex &>/dev/null || [ -d "$HOME/.codex" ]; then
  AGENTS_FOUND="$AGENTS_FOUND Codex"
  info "Merging native LLM-wiki hooks into Codex config (backup first)..."
  if uv run python "$VAULT_ROOT/scripts/merge_codex_hooks.py" --vault-root "$VAULT_ROOT"; then
    ok "Codex hooks merged → ~/.codex/hooks.json"
    warn "Review and trust the new hooks with /hooks in Codex."
  else
    warn "Codex hooks merge failed — run: uv run python scripts/merge_codex_hooks.py"
  fi
  info "Codex CLI detected. Add this to your shell profile:"
  info "  alias codex-mem='uv run python $VAULT_ROOT/scripts/codex_memory.py daily-log --cwd \$(pwd) --reason codex-session-end --json'"
fi

# Cursor
if [ -d "$HOME/.cursor" ] || command -v cursor &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND Cursor"
  info "Cursor detected. Copy rules file to each project:"
  info "  cp $VAULT_ROOT/integrations/cursor/rules/llm-wiki.mdc <project>/.cursor/rules/"
fi

# Antigravity
if [ -d "$HOME/.config/Antigravity" ] || command -v agy &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND Antigravity"
  info "Antigravity detected. Copy AGENTS.md to each project:"
  info "  cp $VAULT_ROOT/integrations/antigravity/AGENTS.md <project>/"
fi

if [ -z "$AGENTS_FOUND" ]; then
  warn "No supported agents detected. Install OpenCode, Codex CLI, Claude Code, Cursor, or Antigravity."
else
  ok "Agents detected:$AGENTS_FOUND"
fi

# Claude Code — merge hooks if CLI or config dir present (safe: backup + non-destructive)
if command -v claude &>/dev/null || [ -d "$HOME/.claude" ]; then
  AGENTS_FOUND="$AGENTS_FOUND Claude"
  info "Merging LLM-wiki hooks into Claude user settings (backup first)..."
  if uv run python "$VAULT_ROOT/scripts/merge_claude_settings.py" \
      --vault-root "$VAULT_ROOT" \
      --state-root "$STATE_ROOT" \
      --legacy-shell bash; then
    ok "Claude settings merged → ~/.claude/settings.json"
  else
    warn "Claude settings merge failed — run: uv run python scripts/merge_claude_settings.py"
  fi
fi

# ─── 9. Optional: sentence-transformers ─────────────────────────────

info "Optional: install sentence-transformers for semantic search?"
info "  uv pip install sentence-transformers"
info "  (adds ~500MB, enables hybrid BM25+Vector search with Recall@5=100%)"

# ─── 10. Print summary ─────────────────────────────────────────────

echo ""
echo "=============================================="
echo -e "${GREEN}  LLM-Wiki installed successfully!${NC}"
echo "=============================================="
echo ""
echo "Vault:          $VAULT_ROOT"
echo "State:          $STATE_ROOT"
echo "Profile:        $PROFILE"
echo "Agents:        ${AGENTS_FOUND:-none detected}"
echo "Maintenance:    cron (nightly 03:00 + weekly Sun 04:00)"
echo ""
echo "Next steps:"
echo "  1. Restart your terminal (to pick up env vars)"
echo "  2. Open a project in your agent"
echo "  3. The system captures automatically — just work normally"
echo ""
echo "Useful commands:"
echo "  uv run python scripts/search_memory.py 'your query'  # search vault"
echo "  uv run python scripts/build_advisory.py              # proactive advisory"
echo "  uv run python scripts/build_guardrails.py             # learned rules"
echo "  uv run python benchmark/run_benchmark.py              # run benchmark"
echo ""
