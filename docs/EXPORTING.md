# Exporting the vault

How to package this repository for distribution or external audit — **without** leaking local state or build artifacts. An export carries the committed files and nothing else, so it is not a way to move your memory to another machine; see [Moving to a new machine](#moving-to-a-new-machine) below.

## TL;DR — use the export script

```bash
python scripts/export_vault.py
```

Wraps `git archive` with mandatory fail-closed verification. The script builds a sibling staging file, verifies every bounded member, and only then atomically publishes the final archive. It rejects forbidden, ambiguous, duplicate, and non-regular entries and scans member content and metadata with the built-in secret detector plus the authenticated `LLM_WIKI_DLP_POLICY` literals and exact fingerprints. Default output is `../llm-wiki-export-<shortsha>.zip`.

ZIP, TAR, and TAR.GZ use the same limits: 512 MiB compressed archive size, 10,000 members, 16 MiB per member, and 256 MiB total uncompressed content. Verification failure removes the staging file and does not replace an existing final archive. The deprecated `--no-verify` flag is retained for CLI compatibility but cannot disable security checks.

## Under the hood — `git archive`

```bash
git archive HEAD -o ../llm-wiki-export.zip
```

`git archive` packages only the files Git knows about (tracked + committed). It respects `.gitignore` by construction — untracked and ignored files are never included. Running it directly does not perform LLM Wiki's content scan or staged publication, so use `scripts/export_vault.py` for distributable archives.

## What to NEVER include in an export

These live in the working copy but must not ship in any distributable bundle:

| Path | Why it's excluded | Notes |
|---|---|---|
| `.venv/` | ~300 MB of machine-specific Python packages, no portability value. | Gitignored. |
| `.git/` | Internal git metadata. Bloats the archive and leaks branch/reflog history. | A fresh `git archive` omits it automatically. |
| `.claude/settings.local.json` | **Machine-local Claude Code permissions and overrides.** Contains your personal `allow/deny` lists, may reference absolute paths outside the vault. | Gitignored. |
| `gitleaks-report.json`, `gitleaks-report.sarif` | Local security-scan output. Often filled with noise from `.venv/` deps. | Gitignored. |
| `cache/`, `logs/`, `run/` | **Runtime state: hashes, dedupe markers, compile logs, FTS5/vector/graph indexes, hook-error log.** Live inside the vault but are gitignored — `git archive` omits them automatically, but a naive `zip -r` of the vault would include them. | Gitignored. The post-build `_verify_archive` step blocks them. |

## Wrong way: raw `zip -r`

```
zip -r llm-wiki.zip $LLM_WIKI_ROOT/
```

This **silently includes** `.venv/`, `.git/`, any `*.local*` files, and every `__pycache__/`. `zip` does not consult `.gitignore`. A colleague auditor specifically flagged this as the cause of `settings.local.json` appearing in an earlier export.

## Right way (details)

### Standard export
```bash
cd $LLM_WIKI_ROOT
uv run python scripts/export_vault.py --output ../llm-wiki-export.zip
```
Produces a clean zip of exactly the committed state. Good for audits and external reviewers.

### Export with a specific commit / tag
```bash
uv run python scripts/export_vault.py --ref <commit-or-tag> --output ../llm-wiki-<ref>.zip
```

### Tarball (Unix reviewers)
```bash
uv run python scripts/export_vault.py --format tar.gz --output ../llm-wiki-export.tar.gz
```

### Double-check before sharing
```bash
unzip -l ../llm-wiki-export.zip | grep -E '\.venv|settings\.local|gitleaks|__pycache__'
```
Should print nothing. If it does, the archive was not built with `git archive`.

## Moving to a new machine

An export is not a migration. The archive packages committed files, so it carries the code and the public examples and leaves out everything the running system wrote: notes you never committed, `knowledge/daily/`, and `cache/`, `logs/`, `run/`.

Move the harness by cloning it:

```bash
git clone git@github.com:Ekgardt/llm-wiki.git $LLM_WIKI_ROOT
```

Then run `install.ps1` / `install.sh` (or follow [[docs/USER-GUIDE|User guide]]) to set up the machine-local pieces (`$LLM_WIKI_ROOT` env var, hooks, agent wiring).

Move the memory itself with the encrypted backup, not with an export. `scripts/private_vault_backup.py backup` writes one verified Restic snapshot and returns a `snapshot_id` plus `manifest_sha256` receipt; `restore` requires both and unpacks validated `vault/` and `state/` directories into a pre-existing empty directory.

`publish` then puts that validated image into the installed vault on the new machine:

```bash
python scripts/private_vault_backup.py publish --image <restored-dir> --manifest-sha256 <digest>
```

It validates the image again first, so an image edited between restore and publish is refused, and it overwrites nothing: every destination must be absent or byte-identical, and the first conflicting path stops the whole publication. Publishing into a vault that already holds different content is therefore refused rather than merged — replacing a populated vault stays a deliberate act you take yourself. The full procedure and its refusals are in [[docs/USER-GUIDE|User guide]].

## Sharing a subset for discussion

If you want to share only a subset (e.g. only `knowledge/notes/`), use sparse checkout or extract a selected path:

```bash
# Extract only knowledge/notes from HEAD into a separate tarball
git archive HEAD --format=tar knowledge/notes | tar -xf - -C /tmp/export
```

## Related
- [[docs/USER-GUIDE|User guide]] and root installers for rebuilding the harness on a new machine.
- `.gitignore` in the repo root — authoritative list of excluded patterns.
