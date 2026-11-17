# Checked filesystem synchronization

`llm_wiki.runtime_sync` owns file/directory flushing, platform branches and
metadata-durability error conversion. It depends only on runtime error values,
stdlib and lazy windows_workspace operations. It never publishes or grants
admission. File flush mode retains Windows write-access requirements. Directory
flush opens the proper handle/descriptor, propagates inability to prove durability
as MetadataDurabilityUnavailable and closes its acquired resource.

Native errno/winerror and exception chaining are preserved. Sync success is only
the requested filesystem boundary, not a multi-file transaction. The old public
names and pickle paths remain available through identical reliable_memory aliases.
Run `uv run pytest tests/modules/runtime_sync -q`. Linux checks do not qualify
Windows flush behavior; original native tests remain platform-specific.

## Canonical ownership reference

Shared canonical contract: [runtime-contracts](runtime-contracts.md).
The complete guide is included in this owner's maintenance packet.
