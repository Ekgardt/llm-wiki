# The queue is adopted before anything writes to it

Dated 2026-09-17. Found by running `install.sh` end to end for the first time (the gap the
third audit calls I-C1). Not in the audit: this is a new high finding, and it is issue #17 a
third time. The research before the fix.

## What was found

- A complete install was run in a throwaway environment: a `git clone` of this branch as the
  vault, a temporary `HOME`, a temporary state root, `MEMORY_LLM_PROVIDER=fake`,
  `--scheduler cron`, and a stub `crontab` on `PATH` so the machine's own crontab stayed
  untouched. The installer finished with exit 0 and printed:
  `Reliability V3 state is 'conflict'; session capture is disabled until adoption runs`.
- The check's JSON says why: `legacy_operational_database_pair_incomplete`, with
  `coordinator_legacy: file` and no legacy queue. `run/markdown-transactions.sqlite3` had
  been created during the install itself.
- Isolated on a second empty state root: `scripts/sync_memory.py --apply` alone creates
  `run/markdown-transactions.sqlite3` (`MarkdownCoordinator.__init__` opens it). Step 8 of
  the installer runs that sync; step 8b then asks about adoption, and by then the vault is
  no longer `fresh` — it holds half of a legacy pair, which `_inspect_legacy_pair` reports
  as `conflict`. `conflict` is not an adoptable state, so the installer can only print the
  warning.
- So the first round's fix for I-A1 (reading the state whatever the check exits with) is
  necessary but not sufficient: on a real fresh install the state it reads correctly is
  `conflict`, and session capture stays disabled with `legacy_protocol_unquiesced` exactly
  as issue #17 described. No test saw it, because no test ran the installer.
- The Windows installer has the same order (step 8 sync, step 8b adoption).

## Practice on this date

- Verified in the same throwaway environment, on a third empty state root: adopt first
  (`repair_installed_memory.py --apply --adopt-ownership-v3 --confirm-all-agents-stopped`,
  exit 0), then `sync_memory.py --apply`, then check — `adoption_state: adopted`, and the
  legacy paths exist as the tombstones adoption writes. The order is the whole difference.
- The contract in `CLAUDE.md` is that explicit offline adoption "publishes versioned v3
  databases and replaces legacy active paths with JSON tombstones". A step that writes
  through the pre-adoption coordinator before that cutover has happened creates the very
  legacy artefact the cutover is meant to migrate.

## The decision

Both installers adopt Reliability V3 immediately after the ownership transaction and the
agent wiring, and before the bounded runtime sync and the model fetch. Nothing in the
adoption step depends on the sync: it reads the vault and writes `run/`. The two later
steps keep their text. A test asks the installers for the order, and a second test shows the
mechanism on the real modules: a coordinator opened before adoption leaves a vault that
cannot be adopted, and one opened after it leaves an adopted vault.

Files: `install.sh`, `install.ps1`,
`tests/test_the_queue_is_adopted_before_anything_writes_to_it.py`,
`docs/research/2026-09-17-the-queue-is-adopted-before-anything-writes-to-it.md`.
