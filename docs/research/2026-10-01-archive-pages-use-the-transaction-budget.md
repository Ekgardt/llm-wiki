# Archived pages use the transaction target budget

Date: 2026-10-01. Compatible correction, no new setting, path, runtime, or numerical value.

The stale-page archive and restore readers had an independent 16 MiB ceiling whose basis was explicitly unknown. A real 17 MiB page reproduces refusal in both paths. Their Markdown transaction boundary already admits targets up to MAX_KNOWLEDGE_TARGET_BYTES (64 MiB). The earlier comment saying product writes never exceeded the ordinary 8 MiB page reader was incorrect: the transaction target contract is separate and larger.

Both reads now use the actual transaction target budget. The redundant declaration is removed. This is not a claim that the existing 64 MiB budget is a measured optimum, nor that ordinary retrieval admits pages that large. It removes one unjustified second policy from a mutation that already has a common safety boundary. CAS, source hashes, absent-destination checks, recoverable transactions, and refusal above the target budget remain. Archiving adds frontmatter, so an input fitting exactly may still produce an oversized output; the transaction must refuse that output and retain the source.

Primary sources checked today: [Python 3.10 binary streams](https://docs.python.org/3.10/library/io.html), [OWASP resource consumption](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/), and [MITRE CWE-400](https://cwe.mitre.org/data/definitions/400.html). They support explicit resource protection and bounded reads; none prescribes 16 or 64 MiB. The chosen numeric basis here is the existing destination contract, not those publications.

Alternatives rejected: invent a larger standalone value, add another setting without a measured reason, lower the archive to the ordinary retrieval reader's ceiling, or remove resource protection entirely. Reusing the destination contract permits supported mutations while retaining their shared protection. Larger admitted pages require more memory; streaming transactional transforms would be a separate design change rather than a hidden rewrite here.

Regression evidence: actual large archive/restore round trip, restore of an existing large archive, and a sparse source one byte past the target budget. Original implementation fails both admissible large cases and passes the oversize refusal. Qualification does not imply full audit completion or universal historical recovery. Private evidence: logs/audit-2026-10-01-archive-bound-*.
