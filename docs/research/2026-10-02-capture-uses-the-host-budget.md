# Durable capture uses the host's configured budget

Research date: 2026-10-02. Installed Claude Code 2.1.283 and Codex CLI 0.159.0
were checked locally. Python compatibility remains 3.10+. No command, matcher,
root, environment contract, queue protocol, schema or runtime directory changes.

The product templates imposed five seconds on Claude UserPromptSubmit and on
Codex UserPromptSubmit/PostToolUse durable capture. Native observations repeatedly
showed cancellation at that cutoff. Existing SQLite admission and busy waits can
legitimately exceed it. Removing unnecessary transaction entries improved the
publication path but did not eliminate native cancellations.

These three handlers now omit the product's unsupported timeout override. Claude
uses its documented 30-second UserPromptSubmit default; Codex uses its documented
600-second command-hook default for these events. Operators can configure the
host timeout in their hook definitions. Other lifecycle events and read-only
hints keep their existing settings; this decision does not justify those other
numeric budgets or certify the full audit. Internal ownership, deadlines, DLP,
file identity, durable evidence and queue validation remain unchanged.

A real isolated adopted vault, complete copied scripts, the locked uv command and
an actual second-thread SQLite exclusive lock reproduced all three old failures.
The lock was held one second beyond the old five-second cutoff, within existing
admission timing. All three baseline cases timed out; without the override all
three commands finished and their retained bundles passed digest validation.
The first runs took 19.47 seconds for three failures and 20.67 for three passes.
No fake provider is selected by the command fixture. These are real command and
storage tests, not native host-event, durable Markdown-terminal, model-token or
Windows execution qualification. The wrapper kills only its own fixture process
group when reproducing host cancellation.

A larger arbitrary product constant would replace one unexplained policy with
another. Asynchronous execution was considered but not selected: Codex cancels
unfinished background hooks at session end, so durability before cancellation
would need separate qualification. The chosen synchronous path can delay a turn
longer than five seconds under contention; it gives the existing publication
protocol time to complete rather than having the host interrupt it prematurely.
It cannot promise completion under every possible load or repair a damaged queue.

Installation must update the owned external hook fragments through install
control, preserving foreign settings and rollback evidence. Source-template
checks alone do not establish installed configuration or native activation.
Codex requires review of changed non-managed hook hashes; no trust bypass or
hand-written trust state is part of this change. Native observation after the
configuration is loaded remains necessary.

Sources checked on the research date:

- [OpenAI hook contracts](https://learn.chatgpt.com/docs/hooks): defaults, trust
  review and background cancellation. SessionEnd/Interrupt have different budgets
  and are outside this change.
- [Claude Code hook contracts](https://code.claude.com/docs/en/hooks): the
  UserPromptSubmit 30-second default and cancellation/discard behavior.
- [SQLite rollback locking](https://www.sqlite.org/lockingv3.html): legitimate
  competing locks and transaction coordination.
- [Python subprocess](https://docs.python.org/3.10/library/subprocess.html):
  explicit command execution and timeout handling in the qualification fixture.

The separate live queue corruption detected during this investigation is retained
as an open recovery problem. No error counter or damaged queue record is erased
by this timeout correction.
