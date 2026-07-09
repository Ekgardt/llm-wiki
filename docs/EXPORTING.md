# Exporting the vault

How to package this repository for distribution, external audit, or migration to a new machine — **without** leaking local state or build artifacts.

## TL;DR — use the export script

```bash
python scripts/export_vault.py
```

Wraps `git archive` with built-in post-export verification: lists the archive contents and fails loudly if any forbidden path slipped in (`.venv/`, `.git/`, `.obsidian/workspace.json`, `settings.local.json`, `gitleaks-report.json`, etc.). Default output is `../llm-wiki-export-<shortsha>.zip`.

## Under the hood — `git archive`

```bash
git archive HEAD -o ../llm-wiki-export.zip
```

`git archive` packages only the files Git knows about (tracked + committed). It respects `.gitignore` by construction — untracked and ignored files are never included.

## What to NEVER include in an export

These live in the working copy but must not ship in any distributable bundle:

| Path | Why it's excluded | Notes |
|---|---|---|
| `.venv/` | ~300 MB of machine-specific Python packages, no portability value. | Gitignored. |
| `.git/` | Internal git metadata. Bloats the archive and leaks branch/reflog history. | A fresh `git archive` omits it automatically. |
| `.claude/settings.local.json` | **Machine-local Claude Code permissions and overrides.** Contains your personal `allow/deny` lists, may reference absolute paths outside the vault. | Gitignored. |
| `gitleaks-report.json`, `gitleaks-report.sarif` | Local security-scan output. Often filled with noise from `.venv/` deps. | Gitignored. |
| `$LLM_WIKI_STATE_ROOT/` (`$LLM_WIKI_STATE_ROOT/` by default) | Runtime state: hashes, dedupe markers, compile logs, QMD index, hook-error log. Lives OUTSIDE the vault — `git archive` can't reach it, but a naive `zip -r` of the vault's parent would. | Lives outside the vault by design. |

## Wrong way: raw `zip -r`

```
zip -r llm-wiki.zip $LLM_WIKI_ROOT/
```

This **silently includes** `.venv/`, `.git/`, any `*.local*` files, and every `__pycache__/`. `zip` does not consult `.gitignore`. A colleague auditor specifically flagged this as the cause of `settings.local.json` appearing in an earlier export.

## Right way (details)

### Standard export
```bash
cd $LLM_WIKI_ROOT
git archive HEAD --format=zip -o ../llm-wiki-export.zip
```
Produces a clean zip of exactly the committed state. Good for audits and external reviewers.

### Export with a specific commit / tag
```bash
git archive <commit-or-tag> --format=zip -o ../llm-wiki-<ref>.zip
```

### Tarball (Unix reviewers)
```bash
git archive HEAD --format=tar.gz -o ../llm-wiki-export.tar.gz
```

### Double-check before sharing
```bash
unzip -l ../llm-wiki-export.zip | grep -E '\.venv|settings\.local|gitleaks|__pycache__'
```
Should print nothing. If it does, the archive was not built with `git archive`.

## Migrating to a new machine

Don't export-then-unzip; just clone:

```bash
git clone git@github.com:Ekgardt/llm-wiki.git $LLM_WIKI_ROOT
```

Then run `install.ps1` / `install.sh` (or follow [[docs/USER-GUIDE|User guide]]) to set up the machine-local pieces (`$LLM_WIKI_ROOT` env var, hooks, agent wiring).

## Sharing a subset for discussion

If you want to share only a subset (e.g. only `knowledge/notes/`), use sparse checkout or extract a selected path:

```bash
# Extract only knowledge/notes/concepts from HEAD into a separate tarball
git archive HEAD --format=tar knowledge/notes/concepts | tar -xf - -C /tmp/export
```

## Related
- [[docs/USER-GUIDE|User guide]] and root installers for rebuilding the harness on a new machine.
- `.gitignore` in the repo root — authoritative list of excluded patterns.
