---
type: decision
title: "Integration Config Backups Are Bounded Sibling Preimages"
description: "Claude and Codex configuration merges create verified byte-exact sibling preimages and retain only bounded LLM-Wiki-owned backups without claiming filesystem metadata preservation."
date: 2026-08-14
confidence: high
source_authority: user
status: active
---
# Integration Config Backups Are Bounded Sibling Preimages

One-sentence summary: Claude and Codex configuration merges create verified byte-exact sibling preimages and retain only bounded LLM-Wiki-owned backups without claiming filesystem metadata preservation.

## Decision

Date: 2026-08-14.

Before replacing an existing Claude `settings.json` or Codex `hooks.json`, the
integration merger creates a byte-exact sibling preimage named
`<config-name>.bak-llm-wiki-<timestamp>`. A no-op merge creates no backup.

The merger verifies the new preimage before publishing changed configuration.
After the changed destination is successfully published and verified, it prunes only
files with that integration's exact LLM-Wiki backup prefix. Retention applies all
three limits:

- no more than 10 backups;
- no backup older than 90 days when a newer restore point exists;
- no more than 100 MiB in aggregate when older restore points can be removed.

The newest verified preimage is always retained, even when it alone exceeds an age or
size limit. An integration's sole restore point is never deleted. Unrelated user
backups are never considered for pruning.

## Paths And Scope

- Claude: sibling files such as
  `settings.json.bak-llm-wiki-20260814-120000-000000`.
- Codex: sibling files such as
  `hooks.json.bak-llm-wiki-20260814-120000-000000`.
- These are user-configuration preimages outside the vault's CODE, KNOWLEDGE, and
  RUNTIME zones. They do not change `$LLM_WIKI_ROOT`, `$LLM_WIKI_STATE_ROOT`, or the
  installed runtime layout.

## Limits

These files preserve configuration bytes, not complete filesystem metadata.
`shutil.copy2()` cannot preserve owner, group, ACLs, macOS resource forks, Windows
alternate data streams, or every platform-specific attribute. The restore contract
therefore covers byte content only. This mechanism is not the encrypted private-vault
backup required by `OPEN-001`.

The implementation remains compatible with Python 3.10 APIs. Python 3.10 is in
security-only support and reaches end of life in October 2026, so no newer-only file
API is part of this contract.

## Consequences

- A changed Codex hook merge gains a new sibling backup path.
- Repeated no-op merges stop growing backup inventory.
- Retention can delete only old files created under the exact LLM-Wiki prefix.
- The newest/sole byte-exact rollback point survives every retention boundary.
- Full owner/ACL/ADS recovery and private-vault disaster recovery remain separate
  unsolved requirements.

## Source / Evidence

- Explicit user approval in the 2026-08-14 OpenCode session.
- `docs/DEVELOPER-AUDIT-HANDOFF-2026-08-12.md`, `OPEN-031` in the audit source worktree.
- Python `shutil.copy2()` limitations: https://docs.python.org/3/library/shutil.html#shutil.copy2
- Python `os.replace()` and `os.fsync()` documentation: https://docs.python.org/3/library/os.html#os.replace
- Python branch status: https://devguide.python.org/versions/
- NIST SP 800-53 Rev. 5 and Release 5.2.0: https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final

## Related

- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/reliable-memory-stage-2]]
- [[knowledge/notes/agent-native-mcp-foundation]]
- [[knowledge/notes/install-ownership-control-plane-decision]]
- [[knowledge/notes/managed-ide-hooks-install-update-decision]]
- [[knowledge/notes/retire-cursor-and-antigravity-decision]]
