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
#   curl ... https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.sh | bash
# The main branch URL is for development convenience only.
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs locked Python deps with the MCP server baseline
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
        "args = [\"run\", \"--directory\", $vault_json, \"python\", \"scripts/mcp_server.py\"]")"
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_ROOT="${LLM_WIKI_ROOT:-$SCRIPT_DIR}"

# If running from curl pipe, we need to clone first
if [[ ! -f "$VAULT_ROOT/pyproject.toml" ]]; then
  info "Cloning LLM-Wiki repository..."
  INSTALL_DIR="${HOME}/LLM-wiki"
  git clone --branch v4.0.0 --depth 1 https://github.com/Ekgardt/llm-wiki.git "$INSTALL_DIR"
  VAULT_ROOT="$INSTALL_DIR"
  cd "$VAULT_ROOT"
fi

cd "$VAULT_ROOT"
info "Vault root: $VAULT_ROOT"

# Prevent accidental pushes from the installed vault
git -C "$VAULT_ROOT" remote set-url --push origin no-push
ok "Push disabled (no-push) — installed vault cannot push to public remote"

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

info "Installing locked Python dependencies with MCP support..."
uv sync --locked --extra mcp-server --quiet
ok "Dependencies installed (MCP server baseline included)"

# ─── 4. Run tests ──────────────────────────────────────────────────

info "Running test suite..."
if uv run pytest -q 2>&1 | tail -1 | grep -qE "failed|error"; then
  warn "Some tests failed — core features will still work, but please report issues"
else
  ok "All tests passed"
fi

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
if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
  PROFILE="${HOME}/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
  PROFILE="${HOME}/.bashrc"
else
  PROFILE="${HOME}/.profile"
fi

# Set LLM_WIKI_ROOT (idempotent per-var, so a re-install updates STATE_ROOT
# even if LLM_WIKI_ROOT was already written by an older installer).
if ! grep -q "LLM_WIKI_ROOT=" "$PROFILE" 2>/dev/null; then
  echo "" >> "$PROFILE"
  echo "# LLM-Wiki memory system" >> "$PROFILE"
  echo "export LLM_WIKI_ROOT=\"$VAULT_ROOT\"" >> "$PROFILE"
  ok "Added LLM_WIKI_ROOT to $PROFILE"
else
  ok "LLM_WIKI_ROOT already in $PROFILE"
fi

# Runtime lives inside the vault as gitignored cache/logs/run dirs.
# LLM_WIKI_STATE_ROOT defaults to the vault itself; set it explicitly only
# if you want runtime on a different disk.
STATE_ROOT="$VAULT_ROOT"
if ! grep -q "LLM_WIKI_STATE_ROOT=" "$PROFILE" 2>/dev/null; then
  echo "export LLM_WIKI_STATE_ROOT=\"$STATE_ROOT\"" >> "$PROFILE"
  ok "Added LLM_WIKI_STATE_ROOT to $PROFILE"
else
  ok "LLM_WIKI_STATE_ROOT already in $PROFILE"
fi

# Create runtime dirs inside the vault (gitignored)
STATE_ROOT="${LLM_WIKI_STATE_ROOT:-$VAULT_ROOT}"
mkdir -p "$STATE_ROOT/run" "$STATE_ROOT/run/queue" "$STATE_ROOT/logs" "$STATE_ROOT/cache" "$STATE_ROOT/cache/cognee"
ok "Runtime dirs: $STATE_ROOT/{run,logs,cache} (gitignored)"

# ─── 6. Set up scheduled maintenance ────────────────────────────────

info "Setting up scheduled maintenance..."

CRON_NIGHTLY="0 3 * * * cd '$VAULT_ROOT' && $(which uv) run python scripts/scheduled_nightly.py >> '$STATE_ROOT/logs/cron-nightly.log' 2>&1"
CRON_WEEKLY="0 4 * * 0 cd '$VAULT_ROOT' && $(which uv) run python scripts/scheduled_weekly.py >> '$STATE_ROOT/logs/cron-weekly.log' 2>&1"

# Remove old LLM-Wiki cron block (between markers only)
if crontab -l 2>/dev/null | grep -q "LLM-Wiki-cron-start"; then
  info "Updating existing cron entries..."
  crontab -l 2>/dev/null | sed '/# LLM-Wiki-cron-start/,/# LLM-Wiki-cron-end/d' | crontab -
fi

# Add new entries with markers
( crontab -l 2>/dev/null; echo "# LLM-Wiki-cron-start"; echo "$CRON_NIGHTLY"; echo "$CRON_WEEKLY"; echo "# LLM-Wiki-cron-end" ) | crontab -
ok "Cron scheduled: nightly 03:00, weekly Sunday 04:00"

# ─── 7. Detect and wire up agents ──────────────────────────────────

info "Detecting installed agents..."

AGENTS_FOUND=""
VAULT_JSON=$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$VAULT_ROOT")

# OpenCode
if [ -d "$HOME/.config/opencode" ] || command -v opencode &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND OpenCode"
  PLUGIN_DIR="$HOME/.config/opencode/plugins"
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
if command -v codex &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND Codex"
  CODEX_CONFIG="$HOME/.codex/config.toml"
  mkdir -p "$HOME/.codex"
  if configure_codex_mcp "$VAULT_ROOT" "$CODEX_CONFIG"; then
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
    ok "Codex official hooks merged: $CODEX_HOOKS"
    info "Open /hooks in Codex to review and trust the LLM-Wiki commands."
  else
    hook_exit=$?
    if [ "$hook_exit" -eq 2 ]; then
      warn "Active inline Codex hooks require manual merge and /hooks trust review; hooks.json was not changed."
    elif [ "$hook_exit" -eq 3 ]; then
      ok "Equivalent LLM-Wiki hooks are already configured inline; hooks.json was not changed."
      info "Open /hooks in Codex to review and trust the inline LLM-Wiki commands."
    elif [ "$hook_exit" -eq 4 ]; then
      : # install_codex_hooks already printed the manual enable instruction.
    else
      warn "Codex hooks were not changed; review the existing hooks configuration manually."
    fi
  fi
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
if command -v claude &>/dev/null || [ -d "$HOME/.claude" ] || [ -f "$HOME/.claude.json" ]; then
  AGENTS_FOUND="$AGENTS_FOUND Claude"
  info "Merging LLM-wiki hooks into Claude user settings (backup first)..."
  if uv run python "$VAULT_ROOT/scripts/merge_claude_settings.py" \
      --vault-root "$VAULT_ROOT" \
      --state-root "$STATE_ROOT"; then
    ok "Claude settings merged → ~/.claude/settings.json"
  else
    warn "Claude settings merge failed — run: uv run python scripts/merge_claude_settings.py"
  fi
  # v4.0: MCP server config for Claude Code
  CLAUDE_MCP="$HOME/.claude.json"
  if [ ! -f "$CLAUDE_MCP" ]; then
    info "Adding MCP server config for Claude Code..."
    printf '%s\n' '{"mcpServers":{"llm-wiki":{"command":"uv","args":["run","--directory",'"$VAULT_JSON"',"python","scripts/mcp_server.py"]}}}' > "$CLAUDE_MCP"
    ok "Claude MCP config: ~/.claude.json"
  elif ! grep -q '"llm-wiki"' "$CLAUDE_MCP" 2>/dev/null; then
    warn "Existing ~/.claude.json found without llm-wiki; merge this under top-level mcpServers:"
    warn '  "llm-wiki":{"command":"uv","args":["run","--directory",'"$VAULT_JSON"',"python","scripts/mcp_server.py"]}'
  fi
fi

# v4.0: OpenCode MCP config
if [ -d "$HOME/.config/opencode" ] || command -v opencode &>/dev/null; then
  OPENCODE_CONFIG="$HOME/.config/opencode/opencode.json"
  if [ ! -f "$OPENCODE_CONFIG" ]; then
    info "Adding MCP server config for OpenCode..."
    mkdir -p "$HOME/.config/opencode"
    printf '%s\n' '{"mcp":{"llm-wiki":{"type":"local","command":["uv","run","--directory",'"$VAULT_JSON"',"python","scripts/mcp_server.py"],"enabled":true}}}' > "$OPENCODE_CONFIG"
    ok "OpenCode MCP config"
  elif ! grep -q '"llm-wiki"' "$OPENCODE_CONFIG" 2>/dev/null; then
    warn "Existing opencode.json found without llm-wiki; merge this under top-level mcp:"
    warn '  "llm-wiki":{"type":"local","command":["uv","run","--directory",'"$VAULT_JSON"',"python","scripts/mcp_server.py"],"enabled":true}'
  fi
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
info "  uv sync --extra hybrid      # LanceDB HNSW + sentence-transformers"
info "  uv sync --extra reranker     # cross-encoder reranker (ONNX)"

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
echo "MCP baseline: 12 local task-shaped tools (installed)"
echo "Optional enhancements:"
echo "  uv sync --extra hybrid        # LanceDB HNSW + semantic search"
echo "  uv sync --extra code-graph    # tree-sitter code graph"
echo "  uv sync --extra reranker      # cross-encoder reranker"
echo "  uv sync --extra full          # all of the above"
echo ""
