# A pending conversation is not dedupe cache

Research and qualification date: 2026-10-03; Python 3.10-compatible code.

The Codex turn-end bookkeeping kept only the newest 64 sessions. Its comment
assumed that one operator would not reach that number and claimed eviction
could cause duplicates but never loss. A pending tail was evicted by exactly
the same rule as a completed capture timestamp. After 80 newer captures the
older pending conversation could no longer be claimed by a subsequent hook.
This is a demonstrated loss of pending work, not a measured performance limit.

Removing that count alone would leave another defect: the state writer tried
to evict disposable dedupe/reducer entries but still published an oversized
file when only protected state remained. The existing writable target is
192 KiB; doctor already refuses reads above 256 KiB. Their shared contract and
the prior measured reader failure are documented in `memory_state.py`. This
change introduces neither a new number nor a new setting.

Before design, the installed built-in code index and navigation at commit
`0997289e` were consulted for turn-end claiming and state serialization. The
graph's incomplete status was retained. Full source inspection supplied the
actual mutation order: serialization occurs before keeping a preimage or
replacing the current state. Repository searches confirmed `_last_touch` and
the 64-entry constant had no other readers before their removal.

Three independent primary sources were read on the research date:

- [Python 3.10 datetime documentation](https://docs.python.org/3.10/library/datetime.html)
  defines aware timestamps and subtraction, including naive/aware limitations.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html) distinguishes JSON
  representation and implementation limits; an entry count does not establish
  its encoded byte size or a conversation's completion.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) explains why
  visibility and preservation of prior state must be treated separately from
  an in-memory mutation. The existing runtime continues to use its approved
  durability and atomic-file mechanisms; no SQLite mode changes are made.

A larger cardinality would postpone the same loss. An unbounded state file
would break an existing reader. Dropping pending entries to satisfy that
reader would lose work. The selected implementation retires only a valid
capture-only timestamp whose existing dedupe window has expired. It preserves
pending entries, unknown fields, invalid timestamps and future timestamps.
The existing capture cadence remains unchanged and is described as a product
cadence, not as an asserted Codex host session lifetime. Its origin is not
newly qualified by this change.

After normal disposable-cache trimming, serialization refuses protected
overflow with `OSError(EFBIG)` before modifying either durable state copy.
The existing Codex fallback handles that error by choosing a durable capture
rather than claiming an uncaptured tail was remembered. More duplicate work
can result under sustained pressure; an already retained tail is preserved.
No new path, environment contract, hook, queue kind or persistent actor is added.

Five regressions failed against the original code: pending eviction, young
capture eviction, expired bookkeeping retention, protected overflow publishing,
and a false remembered-tail decision under insufficient space. Seven new tests
cover these plus unknown entries and publication of the fallback's complete
transcript as a v1 durable intent. The latter uses the established hermetic
adopted-vault fixture: foreground delegate and background wakeup are mocked,
so it proves intent publication, not worker completion or model quality.
The combined state, Codex, rendered-hook and capture-failure suite passed 67
tests in 10.47 seconds. Ruff passed. Actual Lizard/AST measurements matched
changed callable starting lines and checked conditionals/nesting; maximum CCN
was five, including the paired-state driver.

The useful paired task copied one real current pending entry into isolated
state roots, then performed actual locks, preimage preservation, atomic writes
and reads for 80 controlled additional capture timestamps. The original lost
the pending entry and exposed no quiet tail; the candidate preserved it and
returned the tail. Full cycles took 0.602 and 0.686 seconds, with 5,531 and
7,329 serialized bytes respectively, both below the existing target. No model
was called. The added timestamps and future clock are controlled inputs, not
claimed native events. The real seed is retained privately for reproduction.
This is preservation evidence, not a speed improvement claim or a qualification
of every lifecycle marker, full conversation worker or grounded answer.

Private evidence under ignored logs is dated 2026-10-03:
`pending-tail-admission-red`, `pending-tail-admission-navigation`,
`pending-tail-admission-complexity`, `pending-tail-full-state-seed` and
`pending-tail-full-state-pair`. Installed guards and canonical preimages are
retained separately. The remaining audit capture and unsupported-limit findings
remain open; preserving this pending tail does not make historical losses vanish.
