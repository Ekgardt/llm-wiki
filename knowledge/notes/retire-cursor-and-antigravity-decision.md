---
type: decision
title: "Cursor and Antigravity Are Retired as Supported Platforms"
description: "Cursor and Antigravity stop being supported hosts; Claude Code, OpenCode, and Codex remain, and uninstall still takes back a hook fragment an older install wrote."
date: 2026-08-26
confidence: high
source_authority: user
status: accepted
supersedes: [[knowledge/notes/managed-ide-hooks-install-update-decision]]
---
# Cursor and Antigravity Are Retired as Supported Platforms

One-sentence summary: Cursor and Antigravity stop being supported hosts of LLM Wiki;
Claude Code, OpenCode, and Codex remain, and `uninstall` still takes back a hook
fragment an older install wrote.

## Decision

The owner decided on 2026-08-26 to drop Cursor and Antigravity support entirely:
"я предлагаю выпилить вообще поддержку этих платформ… а те кто пользуется пускай
сами адаптируют мою систему под себя."

Claude Code is the host that must work perfectly. OpenCode and Codex are retained —
the owner uses them on a personal machine and may exercise them later. Cursor and
Antigravity he does not use at all, and no evidence for them was ever produced on
any machine available to this project.

This supersedes [[knowledge/notes/managed-ide-hooks-install-update-decision]]
(2026-08-16), which made Cursor and Antigravity user hooks first-class managed
fragments. That page is marked `superseded`; its body is unchanged, because a
decision records what was decided at the time.

## What was removed

- `integrations/cursor/` and `integrations/antigravity/`, including the hook
  templates, the Cursor rules file, and the Antigravity `AGENTS.md`.
- The install-time resource builders `cursor_hooks_resource`,
  `antigravity_hooks_resource`, and `managed_ide_hook_resources`, along with the
  template materialiser they were the only users of.
- The `--cursor-hooks` and `--antigravity-hooks` flags of
  `scripts/install_control.py install`, and the detection that set them in
  `install.sh` and `install.ps1`.
- Every host branch in `scripts/integration_adapter.py`: `IDE_SOURCES`, the Cursor
  and Antigravity payload projections, the bounded host-payload helpers they were
  the only users of, `normalize_host_event`, the two host-shaped success outputs,
  and the neutral-output table.
- The two agent-name patterns in `scripts/event_envelope.py`, the two integration
  hosts and their doctor checks in `scripts/doctor.py`, and the `cursor` /
  `antigravity` choices of `scripts/flush_memory.py --agent`.
- `tests/test_ide_hook_integrations.py`, which existed only for these hosts, and
  the platform-specific cases in five other test files.

Nothing was left switched off. There is no flag, constant, or dormant branch that
would bring either platform back.

## What is kept, and why

The projection readers and writers for both hook formats stay, as a
**removal-only** path: `retired_cursor_hooks_resource(home)` and
`retired_antigravity_hooks_resource(home)`.

The install control plane fails closed when the recorded manifest names a resource
id the running code no longer supplies — `_active_resource` raises
`install_resource_request_mismatch`. Deleting the writing code outright would
therefore make `uninstall` refuse on any machine whose `run/install/manifest.json`
records `cursor-user-hooks` or `antigravity-user-hooks`, leaving our handlers in
the user's `~/.cursor/hooks.json` or `~/.gemini/config/hooks.json`, pointing at a
vault nothing maintains, with no supported way to take them back. That is exactly
the defect this repository fixed for the OpenCode plugin on 2026-08-23.

Subtracting a fragment from a shared JSON file needs the file's format, so no
generic retirement resource is possible: the format knowledge is the removal path.
What the retirement resources drop is everything install-side — they carry no
template, no desired bytes, and no recogniser. `write_owned` refuses with
`install_resource_retired`. Only `read_projections` and `write_projection` work,
which is what `uninstall` and `rollback` use on the v2 control path, where the
snapshots come from the manifest rather than from the checkout.

## What an existing user of those platforms loses

Automatic capture and injected session context. Their hooks stop being installed,
updated, verified, or reported on by `doctor`.

What they do not lose: the vault, MCP reads and actions, and the ability to remove
what an earlier install wrote. MCP was never platform-specific — any agent that
speaks MCP can still configure the local server by hand and use the same 12 tools.
Adapting the rest is theirs to do, which is what the owner said.

## What this decision does not claim

- It does not claim either platform was broken. Both were implemented and tested;
  neither was ever exercised on a real installation by anyone in this project.
- It does not claim the retirement path was proven against a real Cursor or
  Antigravity installation. It is proven against a fragment installed through the
  historical resource shape, reconstructed in the test, and removed by the current
  code — file bytes restored exactly.
- It does not promise a migration. There is no export of captured Cursor or
  Antigravity sessions, because nothing was captured from them here.
- It does not close `EVID-001` or `EVID-015`. It narrows them: the Cursor and
  Antigravity half is out of scope rather than pending, and the OpenCode and Codex
  half still waits on the owner installing those hosts.

## Later evidence

Added 2026-08-26 after the dated research this decision shipped without. The
research supports the removal-only path and finds one thing missing.

Supporting: keeping the uninstall path is the part retirement usually gets
wrong, and the packaging world names the consequence — residual configuration
that "eventually breaks upgrades", which is what `apt purge` exists for. That
format knowledge is the removal path, and so must outlive the feature, is
correct.

Missing: the notice stage. Homebrew retires through deprecated → disabled →
removed, requires a stated reason, and removes only a year after disabling;
Kubernetes keeps a deprecated API working for at least a year "but usage will
result in a warning being displayed". This retirement went from supported to
removed in one step, and on this codebase nothing tells an affected machine:
`doctor` no longer checks these hosts, and `inspect_install_state` reports only
whether the manifest and transaction files exist, not which resource ids the
manifest names. A machine whose manifest still says `cursor-user-hooks` gets no
signal from any command. The removal path is also all-or-nothing — it is reached
only through `uninstall` and `rollback`, so taking back just the Cursor fragment
means removing the whole installation.

The defence is in this page already: no such installation is known to exist. A
deprecation clock protects users who exist. One line in `doctor` when the
manifest names a retired resource is what would turn silence into a fixable
state.

See `docs/research/2026-08-26-retiring-a-supported-host.md`.

## Source / Evidence

- Owner instruction, 2026-08-26 (verbatim above).
- `scripts/install_control.py` — `_active_resource`, `_require_resource_request`,
  `_v2_apply_resource`: the fail-closed rebuild that makes the removal path
  necessary.
- `scripts/integration_hook_config.py` — `retired_cursor_hooks_resource`,
  `retired_antigravity_hooks_resource`, `_refuse_retired_write`.
- `tests/test_integration_hook_config.py` —
  `test_uninstall_takes_back_a_cursor_fragment_written_before_cursor_was_retired`,
  `test_uninstall_takes_back_an_antigravity_fragment_written_before_it_was_retired`,
  and `test_without_the_removal_path_an_old_fragment_could_never_be_taken_back`,
  which records the hazard the removal path exists for.
- [[knowledge/notes/managed-ide-hooks-install-update-decision]] — superseded.
- [[knowledge/notes/install-ownership-control-plane-decision]] — the ownership
  contract this removal is bound by.

## Related

- [[knowledge/notes/managed-ide-hooks-install-update-decision]]
- [[knowledge/notes/install-ownership-control-plane-decision]]
- [[knowledge/notes/agent-native-mcp-foundation]]
- [[knowledge/notes/integration-config-backup-retention-decision]]
