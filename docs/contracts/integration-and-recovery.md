# Integration and recovery contracts

This part of the structure reference moved here from `docs/STRUCTURE.md` so every module
packet does not carry it. It is as binding as that file.

## External integration configuration preimages

Claude and Codex configuration merges may create byte-exact sibling preimages
outside the vault zones. Claude uses
`settings.json.bak-llm-wiki-<YYYYMMDD-HHMMSS-ffffff>` beside `settings.json`; Codex
uses `hooks.json.bak-llm-wiki-<YYYYMMDD-HHMMSS-ffffff>` beside `hooks.json`. The same
prefix applies to a retired Cursor or Antigravity fragment while `uninstall` takes it
back. A no-op merge creates no backup.

Only files with the destination's exact `.bak-llm-wiki-` prefix are owned by this
retention contract. After changed configuration is published and verified, each
integration retains no backup older than the configured retention age (90 days by default) when a newer
restore point exists, and at most 100 MiB in aggregate when older files can be
removed. The newest verified or sole preimage is never deleted. These files preserve
bytes only, not owner, ACL, alternate streams, or complete filesystem metadata. They
are not runtime state and do not replace private-vault backup/restore. See
`knowledge/notes/integration-config-backup-retention-decision.md`. The redundant ten-copy cap was removed on 2026-09-30; see `docs/research/2026-09-30-backup-retention-uses-age-and-bytes.md`.

## Approved audit-closure boundary

The user approved the security, recovery, install, scheduling, coordination, and
evidence contract on 2026-08-15. The only new runtime directory is `run/install/`.
It owns resumable install state, exact-release and external-path manifests, verified
preimages, and non-secret scheduler definitions. It does not contain backup passwords
or provider credentials. Restic receives credentials through its standard external
password command or protected password file.

Cognee is retired from the supported product. The optional package extra, sync script,
and setup path were removed. Existing `cache/cognee/` content is
a disposable legacy cache: no supported reader depends on it, and no installer,
repair, or migration deletes it automatically.

First-party model calls share one fail-closed DLP boundary. Optional custom literals
and fingerprint allowlists are loaded only from the absolute external path named by
`LLM_WIKI_DLP_POLICY`; the policy is not a fourth root zone. Verified local-only mode
accepts only literal-loopback Ollama and requires verifiable Ollama cloud disablement.
The implemented strict path uses a saved `ollama/<model>` selection (with no selection,
the existing `MEMORY_LLM_PROVIDER=ollama` override; a saved model of another provider
is refused as `local_only_conflict` and named by doctor),
the official `OLLAMA_NO_CLOUD=1` server setting, and an explicit `127.0.0.1` or `::1`
endpoint. It disables provider fallback and rejects remote model metadata, but reports
`external_runtime_unverified` because LLM Wiki does not own or inspect the running
Ollama process and therefore cannot prove that it restarted with cloud disabled.

Private-vault backup is a coherent application snapshot, not a direct copy of live
SQLite files. The existing maintenance admission fence blocks cooperating writers;
SQLite online backup, source membership/hash recapture, and a manifest-bound staged
projection create the Restic input. Unknown or ambiguous owners, source races,
integrity failures, and schema mismatches block backup. Restore validates into an
empty staging target before publication and never guesses historical ownership.
The implemented CLI in `scripts/private_vault_backup.py` requires exact Restic
`0.19.1`, an external repository-file containing only the repository location, and
pre-existing empty staging/restore directories. Credentials remain in Restic's
external environment, password file, or password command. Backup returns an exact
snapshot ID plus manifest digest; restore requires both, runs `restic check`, and
keeps a validated `vault/` + `state/` image only on success. It does not publish over
an installed vault. Restic repositories must be outside the vault and staging tree.

The image carries what Git does not (decided 2026-09-17): the knowledge, the runtime
half, untracked files, and tracked files modified since `HEAD`. Paths git holds
identically are skipped, as are `cache/`, `logs/`, `run/`, `.git/`, `.venv/` and
regenerable tool caches (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`,
`node_modules`); directories left holding nothing are not part of the image. Where git
cannot answer, nothing is skipped on git's word. This is what lets `publish` — which
overwrites nothing — land in a fresh clone. See
`docs/research/2026-09-17-a-backup-image-carries-what-git-does-not.md`.

Windows Task Scheduler remains the native Windows scheduler. macOS uses a per-user
LaunchAgent and Linux uses a per-user systemd timer; cron is explicit degraded
fallback only. Blackboard tables reuse `markdown-transactions-v3.sqlite3`, capture
reuses Queue v3 intents/terminal proof, and the active operational database count
remains two. No daemon, MCP tool or runtime root is added, and no automatic Git
operation beyond the nightly fast-forward of the default branch.
Blackboard adds only `blackboard_claim_epochs` and `blackboard_claims` to the exact
coordinator-v3 schema. They provide bounded all-or-none resource claims, renewable
logical leases, expiry/reclaim, and monotonic fencing; authoritative task, conflict,
and resolution events remain append-only Markdown. Installed databases require the
existing explicit offline re-adoption path before clients accept the changed schema
digest. See `knowledge/notes/audit-closure-security-recovery-control-plane-decision.md`
and `knowledge/notes/blackboard-fenced-resource-claims-decision.md`.

### Install ownership state

The first install-control slice is defined by
`knowledge/notes/install-ownership-control-plane-decision.md`. The approved managed
IDE-hook extension is defined by
`knowledge/notes/managed-ide-hooks-install-update-decision.md`. Version 1 records
remain readable; validated installs adopt canonical `install-manifest/v2` and
`install-transaction/v2` for resumable resource-set updates and one retained committed
update rollback. No `complete.json`, install database, daemon, MCP tool, or
force-adoption path exists.

The manifest owns recognized LLM-Wiki profile fragments, Windows user root variables,
native scheduler resources, an explicitly selected cron block, and bounded structural
fragments in the Claude Code and Codex user configuration files. A fragment written by
an install before 2026-08-26 in a retired host's file stays removable. It records exact
source identity and digests but does not claim that a dirty local checkout is an
immutable release. Other agent configuration, Git push protection, code upgrade, full
release inventory, and restored-vault publication remain separate follow-up scopes.

Transactions move through `prepared`, `mutating`, `publishing`, and `committed`.
Failure recovery uses `reverting` and `reverted`; malformed state, ambiguous ownership,
or external drift uses `quarantined`. A durable prepared transaction and all required
owned-fragment preimages precede external mutation. Update leaves the prior manifest
active until target verification and publishes generation +1. The original projection
is retained for uninstall; the prior installed projection supports only the latest
committed-update rollback. Restoration requires the exact expected installed value and
never overwrites a concurrent user edit.

Files the installer owns whole -- the systemd units, the launchd plists, and the OpenCode
plugin -- are replaced by every install or update even when they were changed outside
it (2026-09-28). The changed version becomes the transaction's rollback point, and
`install_takeover.py` first keeps a readable copy under
`run/install/displaced/<time>-<resource-id>/` and reports what the new version does not
carry. A line added to a systemd unit moves to `<unit>.d/50-local.conf`; a line that
replaced one of ours with the same key is reported instead, because in a drop-in it would
add to ours, and an existing `50-local.conf` is never planned or touched. The drop-in is
a released resource of that update's transaction (`systemd_drop_in`): written after the
units, each write reloading systemd, reverted with the transaction on failure, kept out
of the manifest, and removed only by the `rollback` of that same generation, first, before
the unit is restored. launchd has no drop-ins, so its copy and the report keep the edit.
Copies older than `retention.config_backup_days` go on the next install unless their
digest is referenced by the active manifest or transaction. Shared files (the profile block, the cron block, the Claude and
Codex hook files, Windows variables and tasks) keep the fragment rules: a change outside
the installer is refused with the resource, its location, and `--adopt <resource-id>`
where it can be adopted. A transaction matches resources by id, kind and location, so a
rerun that drops resources or moves one (a profile in another file) is one update: the
new set is written and verified first, what it no longer owns goes back to its origin
last, and a failure reverts both; `rollback` restores the previous set first and takes
the new one back after. A set change that involves a cron entry or a Windows task, which
cannot be read back, is still taken back before its replacement and restored if that
fails; the installer says so, and a refusal names the entry and the uninstall command.
See `docs/research/2026-09-28-an-update-replaces-what-it-owns.md` and
`docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md`.

`manifest.json`, `transaction.json`, preimages, and scheduler definitions are bounded,
digest-verified, and durably published on the supported local-filesystem boundary.
Whole profiles, whole crontabs, whole user hook files, unrelated task definitions,
provider credentials, and backup passwords are never copied into `run/install/`. An active manifest,
nonterminal transaction, quarantine, or unreadable install state blocks the offline
`run/` deletion snapshot.
