# Cognee Setup Guide (Phase 4 — Optional Semantic Layer)

> **Platform:** This guide covers Windows setup. For macOS/Linux, use
> `ollama serve` and adjust paths.

This guide walks you through enabling the **optional** Cognee semantic graph layer over your LLM-wiki vault. Cognee adds entity extraction and relationship graph queries on top of your markdown — useful when the vault grows past ~300 pages and `[[wikilinks]]` alone no longer surface all relevant connections.

**Skip this if**: your vault is under 300 pages (you're in DIRECT retrieval tier and don't need it yet). The vault is fully functional without Cognee.

## Prerequisites

| Requirement | Why | Where |
|---|---|---|
| 16GB+ RAM recommended | Cognee graph build peaks at 3-5GB; +Ollama ~1.5GB + Windows ~5GB | system |
| ~3GB free disk space | Ollama models (~1.5GB) + Cognee data + venv deps | (any drive) |
| Python 3.10+ | Cognee runtime | already installed |
| Ollama | Local embeddings + LLM (preserves LLM-agnostic axiom) | install below |

## Step 1: Set OLLAMA_MODELS to a non-C: drive (CRITICAL — do this BEFORE installing Ollama)

By default Ollama stores models in `C:\Users\<user>\.ollama\models`. On a small C: drive this fills up fast. **Set this env var first**:

```powershell
# User-level env var (persists across reboots)
setx OLLAMA_MODELS "<ollama-models-path>/models"

# Create the target directory
New-Item -ItemType Directory -Path "<ollama-models-path>/models" -Force
```

**Close and reopen your terminal** after `setx` so the new env var takes effect.

## Step 2: Install Ollama

Download from <https://ollama.com/download/windows> and run the installer. Verify:

```powershell
ollama --version
ollama list   # should show: NAME, ID, SIZE, MODIFIED (empty list is fine)
echo $env:OLLAMA_MODELS   # should print <ollama-models-path>/models
```

## Step 3: Pull the local models

```powershell
# Embeddings — used for vector search over wiki content
ollama pull mxbai-embed-large

# LLM — used by Cognee for entity extraction (small + fast)
ollama pull qwen3:0.6b
# OR (slightly better quality, ~2x size):
# ollama pull llama3.2:1b
```

Models are ~600MB (mxbai) + ~1GB (qwen3) = ~1.6GB total. They land in `<ollama-models-path>/models\`.

## Step 4: Install Cognee in the vault's venv

```powershell
cd $LLM_WIKI_ROOT
uv sync --extra cognee
```

This adds ~100 packages to `.venv` (FastAPI, SQLAlchemy, aiohttp, etc.). Cognee version pinned: `cognee >= 0.1, < 2` (matches `pyproject.toml`).

If the install reports conflicts with `anyio`, run:

```powershell
uv sync --extra cognee --upgrade-package anyio
```

## Step 5: Verify the setup

```powershell
cd $LLM_WIKI_ROOT
uv run python scripts/cognee_sync.py --status
```

Expected output:
```
=== Cognee setup status ===
  cognee installed:     YES
  cognee data dir:      $LLM_WIKI_STATE_ROOT/cache/cognee
  OLLAMA_MODELS env:    <ollama-models-path>/models
  Ollama reachable:     YES
  Pages eligible:       <N>
```

## Step 6: Start Ollama service

Ollama serves on `127.0.0.1:11434`. On Windows it auto-starts on boot once installed. To start manually:

```powershell
ollama serve
```

Leave this running in a terminal window (or use Task Scheduler / `nssm` to run as a service).

## Step 7: First sync

```powershell
cd $LLM_WIKI_ROOT
uv run python scripts/cognee_sync.py
```

This will:
1. Read all current pages from `knowledge/notes/` (~30 markdown pages, depending on what's been compiled).
2. Add them to Cognee's data store at `$LLM_WIKI_STATE_ROOT/cache/cognee/`.
3. Run `cognify()` — entity extraction + relationship graph build.

**Expect 5-15 minutes** for the first build (depending on page count and LLM speed). Watch CPU/RAM — peak ~5GB. Don't run other heavy tasks during the first build.

## Step 8: Query the graph

After sync completes, you can query Cognee from Python:

```python
import asyncio
import cognee

asyncio.run(cognee.search("What decisions were made about hook scripts?"))
```

Or via MCP once you install the Cognee Claude Code plugin:

```powershell
claude plugin marketplace add topoteretes/cognee-integrations
claude plugin install cognee-memory@cognee
```

## Ongoing maintenance

### After each `compile_memory.py` run

The compile pipeline does NOT auto-sync to Cognee. To sync after a compile:

```powershell
uv run python scripts/cognee_sync.py
```

To sync just the touched files (faster):

```powershell
uv run python scripts/cognee_sync.py --file knowledge/notes/new-pattern.md
```

### Skipping the graph build (faster)

If you just want to refresh the data without rebuilding the graph:

```powershell
uv run python scripts/cognee_sync.py --skip-cognify
```

Run a full sync (without `--skip-cognify`) weekly or after major knowledge additions.

## Troubleshooting

### "cognee not installed"
Run `uv sync --extra cognee` from `$LLM_WIKI_ROOT`.

### "Ollama not reachable"
Start the service: `ollama serve`. Check it's listening on 11434.

### RAM exhaustion during cognify
Close browser + Obsidian + Claude Code during the build. If still OOM, switch to a smaller LLM (`qwen3:0.6b` is the smallest reasonable choice).

### Models in C: instead of a non-C: drive
You forgot to set `OLLAMA_MODELS` before pulling. Move `C:\Users\<user>\.ollama\models\*` to `<ollama-models-path>/models\`, set the env var, restart Ollama.

### Backend selection
Start `ollama serve` (default backend). For cloud fallback, set `MEMORY_LLM_PROVIDER` and `LLM_API_KEY`. **Not recommended** for this vault — violates the LLM-agnostic axiom.

## When NOT to use Cognee

- **Under 300 pages**: DIRECT retrieval tier works fine; Cognee adds overhead without benefit.
- **Single-user, single-project**: wikilinks suffice.
- **Privacy-critical content**: even local Ollama sends text through a model. If pages contain secrets they shouldn't be in the vault anyway, but Cognee expands the surface.

## Uninstall

```powershell
cd $LLM_WIKI_ROOT
uv pip uninstall cognee
# Optionally remove data:
Remove-Item -Recurse -Force $LLM_WIKI_STATE_ROOT/cache/cognee
```

The vault continues to work without Cognee — all retrieval falls back to QMD + index.md.
