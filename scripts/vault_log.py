"""Where the vault's editorial log lives: a private file beside the template the repository ships.

`knowledge/log.md` is tracked and stays the four-line template; every install's
runtime writes and reads `knowledge/log.local.md`, which `.gitignore` denies. See
`docs/research/2026-09-14-the-vault-log-is-private.md`.
"""
from __future__ import annotations

LOG_RELATIVE = "knowledge/log.local.md"
LOG_NAME = "log.local.md"
