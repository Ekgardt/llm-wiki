# A refused compile of a day since compiled is history

Date: 2026-09-11. Trigger: the doctor has reported `transactions: error —
1 refused attempt(s) whose work never happened` on every run since
2026-08-25 (`logs/doctor-report.json`), and the session-start block repeats
it to the owner.

## What the attempt is

Transaction `cb387b96…` (operation `compile:75bccb…`, 2026-08-25 12:40Z,
`dlp_content_blocked`) meant to create eight compile receipts
(`knowledge/daily/receipts/v3-…`) plus the index and log. Its `after`
artifacts under `run/transactions/cb387b96…/after/` show every receipt names
`knowledge/daily/2026-08-25.md`, at eight successive snapshots of that day
(sha `bee1eded…` 14 188 bytes … `b6b86f9b…` 16 293 bytes). None of those
receipts was ever written — those snapshots no longer exist; the day kept
growing and was rewritten since. Checked on 2026-09-11 with the compile's
own part rule (`evidence_resolver._daily_part_bounds`): every part of the
day as it is now carries a committed v3 receipt. The work the refusal
stopped — compiling that day — happened; only the refused snapshots' own
receipts are missing, and they never can appear.

The doctor's three proofs (`_unresolved_quarantine`): a committed retry in
the same chain, a commit of the same base operation identity, or every
intended create written by a commit. A compile of a day that has changed
since matches none of them, so the finding can never clear.

## Sources

1. `knowledge/notes/self-resolving-health-findings-decision.md` and the
   doctor docstring: quarantine is retained evidence; a finding describes a
   live condition, and "a health check that is always red stops being read".
2. Compile semantics in this repository: a day is compiled when every part of
   its current bytes has a committed receipt (`compile_memory.daily_is_compiled`,
   `compile_source_identity` = sha256 of `[logical_path, part_sha256]`);
   older snapshots are never compiled again by design.

## Decision

A fourth proof, narrow on purpose: a refused attempt whose intended creates
are **only** compile receipts is history when (a) every receipt artifact it
staged parses as a compile receipt naming a day under `knowledge/daily/`,
and (b) every part of each named day's current bytes has a committed receipt
create in the transaction database. Anything unreadable, a staged receipt
outside the daily directory, a day that is missing, or one uncompiled part
keeps the finding. The proof reads only bounded runtime artifacts and the
day files; it writes nothing.

`_transaction_check` gains the vault root it needs to read the day files.

Files: `scripts/doctor.py`, `tests/test_doctor.py`, `CHANGELOG.md`.

## Exact historical v3 source outcomes (2026-10-05)

The earlier current-day proof remains separate. A selected v3 source digest
must never be substituted for a whole daily digest or upgraded to a v4 physical
source/span selector. A later genuine compilation may choose different pages or
explicitly find no durable content. It then has a different operation identity
and different receipt bytes, even when it handled exactly the same historical
source and complete batch.

The candidate recognizes that narrow historical outcome only when every staged
receipt is canonical v3 and bound to the retained compile request, the entire
source descriptor and all batch-manifest members equal a current canonical v3
receipt, and a committed transaction created those exact receipt bytes. The
existing receipt reader validates schema, complete terminal dispositions and
operation identity; the existing operation-integrity check verifies every
claimed output's path, kind and after hash. The receipt is read again before a
positive result. Missing, altered, uncommitted or unknown evidence retains the
finding. A replacement-only receipt is not a witness: the existing history-prune
contract protects committed CREATE witnesses of quarantined attempts.

This is transient doctor classification. Actual quarantined SQL records remain
quarantined; their before/after artifacts and history remain retained. Doctor's
`transaction_quarantined` deletion code still uses the full quarantined state
count, independently of unresolved findings. No rollback, abort, manual SQL
settlement, whole-current-day selection or archive authority is introduced.

Alternatives rejected: replaying a quarantined plan against changed CAS inputs,
using a source-digest prefix match, promoting historical v3 descriptors to v4,
and trusting a receipt filename without canonical committed operation evidence.
The cost is bounded reads of existing staged/current receipt images and SQL
proofs; this change does not promise full-health convergence during live writes.

Primary research rechecked on 2026-10-05:

- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html), rollback-journal
  semantics: SQL commit is not proof of independent filesystem publication.
- [Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects): exact
  content identity does not justify prefix aliases or producer authority.
- [Python 3.10.22 hashlib](https://docs.python.org/3.10/library/hashlib.html): hashes
  bind exact bytes; canonical schema and committed transaction proofs remain
  separate requirements.

Qualification uses real offline adopted coordinators and actual prepare/apply
transitions, with a refused old interpretation and a different committed source
outcome. No model call is necessary. Negative controls cover descriptor fields,
logical paths, all batch members, terminal dispositions, receipt version and
canonical bytes, request binding, declared output operations, missing receipts,
nonterminal transactions, deadlines and changes during the authority read.
Runtime observations and final source hashes belong to the private diagnostic
report; this public explanation contains no private source text. A successful
historical proof does not make the remaining unresolved attempts, nightly or the
whole audit complete.

The receipt's own complete source descriptor must also occur in its manifest.
The CREATE requirement and every declared output hash are checked against the
same committed transaction row; separate lookups could otherwise combine a
retained CREATE witness with an unretained replacement's output evidence. A
real adopted regression exposed that intermediate candidate defect before
installation, and the common committed-receipt helper now offers this internal
optional restriction without changing its existing callers' default behavior.

The final focused qualification passed 27 cases, including the original refused
source outcome and the separate-transaction witness negative. The original RED,
an initial deadline-fixture error, and an interrupted intermediate related run
are retained as failed/partial evidence. A targeted read-only run against the
retained installed records kept every SQL state and selected row fingerprint
unchanged. This demonstrates historical recognition only; it is not a matched
performance measurement or a complete doctor/nightly qualification. No model
inference, SQL settlement, artifact pruning or installation was performed by
this candidate work.

## Exact current v4 source-work evidence (2026-10-06 candidate)

A historical v3 selected source is not a whole-day identity and has no physical
offset authority. A current v4 execution can nevertheless prove new work on
exactly those old bytes. The candidate keeps those two facts separate. Every
old staged receipt must remain canonical, match its retained request and cover
its complete old batch. Each old descriptor must match one unique, complete
current source unit at the same logical path, with identical bytes, length,
digest and occurrence metadata. Native user containers and raw tool physical
lines include all required parts; native permanent heads are verified again.
No old claim, staged interpretation or historical physical offset is promoted.

Every current part needs a canonical committed v4 receipt. A current witness
must declare exactly the complete old batch expressed as current physical
descriptors, including the current whole-source digest and bounds. Extra,
missing, partial, ambiguous or changed members refuse recognition. Preserved
companions may retain their earlier packing receipt; that does not replace the
full current batch witness. Its CREATE receipt and all declared output hashes
must belong to one committed transaction. Declared outputs are checked against
current files, and receipts, source bytes and native physical proofs are checked
again before acceptance. This is read-only classification, not SQL settlement,
v3 receipt publication, v4 historical authority, semantic replay or permission
to delete a quarantined record.

The existing part selector deliberately reconstructs raw parts without native
projection frames. An adopted native publication control confirms identical
physical v4 descriptors and receipt recognition with and without those frames.
Its pending-source path separately restores already committed companions when a
native unit is incomplete. That selector was not changed: it does not itself
claim that an arbitrary historical packet is a complete atomic unit.

The history-pruning path keeps all `compile:` authority rows through its existing
`KEPT_OPERATION_FAMILIES` contract. A v4 witness therefore does not need a new
legacy receipt-path alias or lineage entry. The candidate uses those kept SQL
operations and live receipt/output bytes, not the committed transaction's undo
images. A genuine adopted test prunes eligible undo images and runs the normal
history prune, then requires the same source-work outcome and unchanged old
quarantine. Missing or damaged retained evidence still refuses recognition.

Primary research rechecked on 2026-10-06: [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html),
[Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects), and
[Python 3.10.22 hashlib](https://docs.python.org/3.10/library/hashlib.html).
These explain why content identity, filesystem containment and committed
publication are separate proofs. Alternatives rejected include digest/prefix
aliases, reconstructing unknown historical offsets, rewriting quarantined SQL,
and replaying the old model plan. No format, environment, setting, path or
runtime database changes are introduced.

The cost includes fresh reads and native proof reconstruction of the relevant
current daily sources. There is no external-authority cache or increased
deadline. Qualification of temporary canonical transactions does not prove
model quality, actual execution of any remaining real source unit, full health
convergence, matched production latency or completion of the audit. A changed
source, unavailable receipt, nonterminal disposition or live output drift keeps
the unresolved finding; actual quarantined state and deletion protection remain.

The multi-source control includes two old units at the same logical path.
Current v4 manifest validation requires their physical version to agree, and
the reader compares that whole manifest exactly. A controlled append or edit
between source captures cannot combine two versions into a positive witness.
These controls pass without a new source-clock cache or relaxed parser rule.
