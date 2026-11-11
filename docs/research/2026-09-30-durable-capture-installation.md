# Durable capture installation

Date: 2026-09-30. This records installed behavior and bounded qualification,
not completion of every audit item or the superset product.

The isolated candidate suite passed **10,676 tests**, with **206 skipped** and
9 warnings in 1,710.27 seconds. Platform skips remain unverified. Evidence:
`logs/audit-2026-09-30-continuation-final-full.{txt,xml}`. The prior failed run
is retained; it was not counted as a passing suite. The separate installed fixes
to graph/extraction limits and citation short terms passed 140 and 106 related
tests, including actual complexity checks; they postdate the candidate suite.

On the frozen comparison corpus, unchanged product retrieval at the existing
20-second setting found 9/10 historical pages and 3/3 new boundary facts; a
14-second repeat found 9/10 and 2/3. The unchanged 14-second default was not
raised globally. One historical page remains a retrieval miss. The model weights
and integrity checks were not replaced, and no experiment-only reranking patch
was used for these measurements.

Three paired read-only grounded-answer tasks then tested new boundary facts
through retrieval, generation and citation verification. Baseline: 0/3 answered,
9 model calls, 129.86 seconds and 65,872 prompt/system bytes. Candidate default:
3/3 answered, 3 calls, 86.89 seconds and 124,639 bytes. A separate 16,384-byte
caller-input experiment retained 3/3 verified claims in 3 calls, 82.96 seconds
and 37,214 bytes, about 70% below the candidate's default. That experimental
size accommodates the single-fact evidence spans and prompt in these cases;
it was not installed as a universal limit. Quality came before prompt reduction.

The same configured automatic provider selected Codex with an implicit model in
all arms. The model version was not pinned by this evaluation. The client reports
estimated input counts, not billed usage; output/reasoning/cache token charges
are unknown. All generator calls and their durations are retained. The successful
claims themselves, not merely citation payloads, contain each expected fact.
Runs overlapped other local work, so seconds are observations, not controlled
latency bounds. These three constructed cases are not a broad accuracy estimate.
Evidence: `logs/audit-2026-09-30-continuation-answer-cycle-*.{json,txt}`. The
initial baseline setup error is retained separately and excluded from comparison.

Installation copied 92 reviewed differing public files under the existing
`runtime-deletion-check` quiescence fence, with verified preimages and hashes.
No active operational owner existed before acquisition; admission stayed fenced
through replacement and release. The original approved development-law wording
was preserved in byte-identical AGENTS/CLAUDE files. The obsolete
`scripts/capture_operation.py` was removed after its replacements qualified;
installed adapter configurations and candidate code have no consumer of it.
Existing hook entrypoints remain because installed hosts still name them. V1
full-session readers remain because those supported events and retained tasks
still consume them; remove them only after that contract is replaced and all
retained bindings qualify for migration or purge.

The installed native Codex `post_tool_use` event from the active session passed
the complete production terminal proof: result digest, indexed receipt,
immutable linked source, input identity and journal transaction. This establishes
real tool delivery, not that every native event type was fired during this run.
Evidence: `logs/audit-2026-09-30-continuation-installed-native-delivery.json`.
No historical loss counters, quarantines, retained failure evidence or runtime
databases were deleted. Idle transactions were healthy, source failures zero,
and the scheduler current. The first concurrent doctor scan was unreadable;
the later direct scan and idle report passed. Historical loss and LSP warnings
remain visible. The wider law 9 inventory and final publication remain open.

Post-installation qualification in the matching isolated checkout passed
**391 tests in 64.97 seconds**, including the combined installed extraction and
citation repairs, release/structure/privacy guards, native hook payloads,
namespace ownership and complete terminal proof. A run in the active vault had
207 passing tests and a final isolation-guard error caused by three verified
native Codex events written by the host during that run; those records were not
test writes. The guard was not changed or bypassed. Evidence:
`logs/audit-2026-09-30-continuation-installed-isolated-qualification.txt` and the
retained failed `installed-qualification.txt`.

The installed generation was rebuilt successfully with current extraction
identity and complete vectors. Its report found no critical error, and its
idle queue had no live worker, unresolved dead task or source failure. A genuine
systemd nightly service run after cutover completed at 17:34:12 UTC with
**failures=0**. Existing historical warnings remain. Evidence:
`logs/audit-2026-09-30-continuation-installed-generation-rebuild.json` and
`logs/nightly-2026-09-30.md`.
