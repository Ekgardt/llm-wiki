# A day uses the existing daily evidence contract

Date: 2026-10-02. Status: isolated candidate; installation recorded separately.

The actual 2026-10-01 daily log is 5 383 252 bytes. The nightly failed before
parts could be offered to the compiler: a separate 4 MiB daily read bound was
smaller than the existing 16 MiB daily archive/evidence contract and the existing
32 MiB total compile-source budget. The pending and receipt-mirror readers used
the same smaller bound. The original nightly failure is retained.

Three independent primary sources checked today:

- [Python 3.10 binary IO](https://docs.python.org/3.10/library/io.html): bytes preserve source spans; bounded reads remain possible.
- [MITRE CWE-400](https://cwe.mitre.org/data/definitions/400.html): bound resource use rather than remove safeguards.
- [OWASP API4](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/): resource budgets must consider the cost of processing.

They do not establish any of the existing numeric values as optimal.
Choose a shared daily reader under the existing archive/evidence contract, also
bounded by the existing configured total compile-source budget. No new number,
setting, directory or model is added. Hashes, no-link stable reads, exact source
bytes, UTF-8 part boundaries and verified per-part receipt checks remain.
All daily readers use this boundary; generic context-page reads are unchanged.

Removing bounds would abandon resource safety. Raising the 4 MiB number alone
would leave divergent readers and no operator control. A streaming/archive
schema redesign is unnecessary for this actual file and requires separate
qualification; days above the existing 16 MiB contract still refuse. That older
contract's general numeric basis remains open in the audit: this repair does not
claim that every future day fits or that all limits are justified.

A real-byte regression bigger than the old 4 MiB bound reproduces snapshot,
pending/explicit, and whole-day digest failures before correction. It verifies
all part bytes and whole source digest, plus refusal below the configured budget
and above the unchanged archive boundary. A predicate in the digest unit test
models receipt presence; it is not proof that the actual live day's parts are
compiled. Live pending-part and full-cycle costs must be measured before a
manual compile is started. No background compile is started by this patch.

Qualification: 3 failed / 1 passed before correction, 120 related passes after
correction, 275 broader passes including archive, resolver and structural guards;
5 measured tests; Ruff, Gitleaks and actual Lizard CCN at most 4 passed. An initial
broader command named a nonexistent `test_archive_daily.py` and ran no tests;
the corrected run used `test_archive_daily_bagit.py`. That refusal is retained.

Actual live read-only measurement: 5 383 252 physical bytes, 345 parts, 342 still
pending by validated v3 receipts; 1009 compile sources / 8 605 036 source bytes,
1.397 s, 59 796 KiB process peak RSS. No model call was made. The real planner
admits all 342 pending parts under the existing context window and needs 342
separate draft batches under the established one-part-per-day source-identity
contract. Its conservative model-unspecified count estimates 7 280 475 mandatory
input tokens, up to 9 488 448 with optional context; output, critique, retries and
provider billing are not included. This is cost evidence, not a billing result
or proof of useful compilation. The one-part rule prevents receipts for content
dropped by path-keyed sources (`2026-09-25-one-part-of-a-day-per-batch.md`).
The high complete-cycle cost remains an efficiency problem; no manual full-day
compile, context deletion, receipt fabrication or corpus backfill was performed.
