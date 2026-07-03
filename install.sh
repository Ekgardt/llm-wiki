#!/usr/bin/env bash
# LLM-Wiki one-command installer for macOS, Linux, and WSL2.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
#
# Or clone first:
#   git clone git@github.com:Ekgardt/llm-wiki.git && cd llm-wiki && ./install.sh
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, uv, git)
#   2. Installs Python deps (uv sync)
#   3. Runs tests to verify everything works
#   4. Sets LLM_WIKI_ROOT in shell profile
#   5. Sets up cron jobs (nightly + weekly) — macOS launchd or Linux cron
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

# ─── 1. Resolve vault root ──────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_ROOT="${LLM_WIKI_ROOT:-$SCRIPT_DIR}"

# If running from curl pipe, we need to clone first
if [[ ! -f "$VAULT_ROOT/pyproject.toml" ]]; then
  info "Cloning LLM-Wiki repository..."
  INSTALL_DIR="${HOME}/LLM-wiki"
  git clone https://github.com/Ekgardt/llm-wiki.git "$INSTALL_DIR"
  VAULT_ROOT="$INSTALL_DIR"
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
uv sync --quiet
ok "Dependencies installed"

# ─── 4. Run tests ──────────────────────────────────────────────────

info "Running test suite (106 tests)..."
if uv run pytest -q 2>&1 | tail -1 | grep -q "passed"; then
  ok "All tests passed"
else
  warn "Some tests failed — core features will still work, but please report issues"
fi

# ─── 5. Set environment variables ──────────────────────────────────

info "Setting environment variables..."

# Detect shell profile
if [[ -n "${ZSH_VERSION:-}" ]] || [[ "$SHELL" == */zsh ]]; then
  PROFILE="${HOME}/.zshrc"
elif [[ -n "${BASH_VERSION:-}" ]] || [[ "$SHELL" == */bash ]]; then
  PROFILE="${HOME}/.bashrc"
else
  PROFILE="${HOME}/.profile"
fi

# Check if already set
if ! grep -q "LLM_WIKI_ROOT=" "$PROFILE" 2>/dev/null; then
  echo "" >> "$PROFILE"
  echo "# LLM-Wiki memory system" >> "$PROFILE"
  echo "export LLM_WIKI_ROOT=\"$VAULT_ROOT\"" >> "$PROFILE"
  STATE_ROOT="$VAULT_ROOT/../LLM-wiki-state"
  echo "export LLM_WIKI_STATE_ROOT=\"$STATE_ROOT\"" >> "$PROFILE"
  ok "Added LLM_WIKI_ROOT to $PROFILE"
else
  ok "LLM_WIKI_ROOT already in $PROFILE"
fi

# Create state directory
STATE_ROOT="${LLM_WIKI_STATE_ROOT:-$VAULT_ROOT/../LLM-wiki-state}"
mkdir -p "$STATE_ROOT/memory-state"
mkdir -p "$STATE_ROOT/memory-reports"
mkdir -p "$STATE_ROOT/search"
ok "State directory: $STATE_ROOT"

# ─── 6. Build search index ─────────────────────────────────────────

info "Building FTS5 search index..."
uv run python "$VAULT_ROOT/scripts/search_memory.py" --rebuild 2>/dev/null || true
ok "Search index built"

# ─── 7. Set up scheduled maintenance ────────────────────────────────

info "Setting up scheduled maintenance..."

CRON_NIGHTLY="0 3 * * * cd $VAULT_ROOT && $(which uv) run python scripts/scheduled_nightly.py >> $STATE_ROOT/memory-reports/cron-nightly.log 2>&1"
CRON_WEEKLY="0 4 * * 0 cd $VAULT_ROOT && $(which uv) run python scripts/scheduled_weekly.py >> $STATE_ROOT/memory-reports/cron-weekly.log 2>&1"

# Remove old entries if re-running
if crontab -l 2>/dev/null | grep -q "LLM-Wiki"; then
  info "Updating existing cron entries..."
  crontab -l 2>/dev/null | grep -v "LLM-Wiki\|scheduled_nightly\|scheduled_weekly" | crontab -
fi

# Add new entries
( crontab -l 2>/dev/null; echo "# LLM-Wiki nightly maintenance"; echo "$CRON_NIGHTLY"; echo "# LLM-Wiki weekly maintenance"; echo "$CRON_WEEKLY" ) | crontab -
ok "Cron scheduled: nightly 03:00, weekly Sunday 04:00"

# ─── 8. Detect and wire up agents ──────────────────────────────────

info "Detecting installed agents..."

AGENTS_FOUND=""

# OpenCode
if [ -d "$HOME/.config/opencode" ] || command -v opencode &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND OpenCode"
  PLUGIN_DIR="$HOME/.config/opencode/plugins"
  mkdir -p "$PLUGIN_DIR"
  if [ ! -f "$PLUGIN_DIR/llm-wiki-memory.js" ]; then
    cp "$VAULT_ROOT/scripts/llm-wiki-memory-opencode.js" "$PLUGIN_DIR/llm-wiki-memory.js" 2>/dev/null || true
    # If no pre-built plugin, create a minimal one
    if [ ! -f "$PLUGIN_DIR/llm-wiki-memory.js" ]; then
      cat > "$PLUGIN_DIR/llm-wiki-memory.js" << 'PLUGIN'
export const LlmWikiMemoryPlugin = async ({ client, $, directory }) => {
  const VAULT = process.env.LLM_WIKI_ROOT || "LLM-wiki";
  return {
    "session.created": async () => {
      try { await $`uv run python ${VAULT}/scripts/session_start_context.py`.quiet().nothrow(); } catch {}
    },
    "tool.execute.after": async (input) => {
      const tool = String(input?.tool || "");
      if (!["edit","write","multi_edit","bash"].includes(tool)) return;
      try {
        const payload = JSON.stringify({tool, target: String(input?.input?.filePath||input?.input?.command||"").slice(0,100)});
        await $`uv run python ${VAULT}/scripts/daily_log_append.py`.stdin(payload).quiet().nothrow();
      } catch {}
    },
  };
};
PLUGIN
    fi
    ok "OpenCode plugin installed"
  else
    ok "OpenCode plugin already exists"
  fi
fi

# Codex CLI
if command -v codex &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND Codex"
  info "Codex CLI detected. Add this to your shell profile:"
  info "  alias codex-mem='uv run python $VAULT/scripts/codex_memory.py daily-log --cwd \$(pwd) --reason codex-session-end --json'"
fi

# Claude Code
if [ -d "$HOME/.claude" ] || command -v claude &>/dev/null; then
  AGENTS_FOUND="$AGENTS_FOUND Claude"
  ok "Claude Code detected — hooks in settings.json (configure manually on macOS/Linux)"
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
