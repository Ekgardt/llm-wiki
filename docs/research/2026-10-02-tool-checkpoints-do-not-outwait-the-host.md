# Tool checkpoints do not outwait the host

Date: 2026-10-02. Scope: synchronous lifecycle adapters and the existing durable project checkpoint backlog. No database, runtime path, environment contract, or host timeout changes.

## Reproduced failure

The installed Codex app-server 0.160.0 emitted genuine `hook/completed` notifications for PostToolUse: the capture handler failed at 5002 and 5001 ms while the graph hint handler completed. Other capture runs completed in 2543–4791 ms. A genuine PreCompact capture completed in 4423 ms. Registration alone is not successful event qualification.

A regression holds the real adopted coordinator writer gate after the breadcrumb has been durably published. UserPromptSubmit already returns with its checkpoint pending. PostToolUse waited beyond the existing five-second host contract: the initial run failed one case and passed three. This proves a second, recoverable project handoff can outwait the host after the primary event is safe.

## Research and choice

Three independent primary sources were checked on this date:

- [Codex hooks](https://learn.chatgpt.com/docs/hooks): synchronous event commands have a host timeout. The measured host is 0.160.0; its actual notifications supply the version-specific timing evidence.
- [SQLite busy timeout](https://www.sqlite.org/pragma.html#pragma_busy_timeout): database contention waiting is a separate operation from the host command deadline. Increasing a host timeout does not correct a writer wait that exceeds it.
- [Python threading events](https://docs.python.org/3/library/threading.html#threading.Event): event synchronization allows a regression to prove the competing writer is live before capture continues, and release it explicitly afterward. These APIs are compatible with the supported Python 3.10 baseline; the documentation currently describes Python 3.14.

Raising the host timeout changes the user-facing wait and does not resolve competing ownership. Dropping the checkpoint loses project handoff evidence. Adding a worker or runtime store duplicates mechanisms already present. The chosen change uses the existing nonwaiting checkpoint writer attempt for every non-SessionStart synchronous lifecycle event. SessionStart keeps its existing bounded recovery behavior. The checkpoint is persisted before the writer attempt; an unavailable writer leaves it in the existing ordered backlog, which the existing recovery actor drains later.

No capture, adoption, schema, integrity, ownership, DLP, or transaction validation is removed. The regression additionally proves the pending checkpoint drains after the real competing writer is released, produces a project journal, and an uncontended tool event still commits its checkpoint immediately.

## Remaining qualification

The live factory profile independently measured 0.732 s opening the queue and 1.405 s opening the coordinator against retained operational history. Whole-database certification is still on that path. This change repairs the reproduced handoff wait; it does not establish that every native capture now fits five seconds, nor close all seven native event qualifications. Host failures are not automatically proof of lost content: publication and terminal evidence must be reconciled separately.

## Verification and installation

The corrected regression passes all four scenarios. Related checkpoint, breadcrumb, worker and recovery checks: 142 passed. Mandatory structure, branch-count, test-weight and slow-machine checks: 64 passed. Ruff and Gitleaks passed. Actual Lizard measurements: changed handler CCN 3; changed regression functions CCN 1–2. Installation used verified preimages and the existing maintenance fence and writer gate. The installed producer was exercised on four separate adopted temporary vaults with real contention and recovery; all four passed, with no model calls and no live-knowledge test writes. The old prompt-only branch was removed rather than retained beside the common path.

Private evidence: `logs/audit-2026-10-02-tool-checkpoint-{red,green,related,guards,lizard,installed-proof}.*`; live host notifications and the admission profile are recorded separately. The full shard-four result on the preceding a7d2ea54 checkpoint was 2414 passed and 73 skipped; it is not a full-suite assertion for this change.
