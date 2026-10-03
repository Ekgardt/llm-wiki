# Corpus defaults share the discovery budget

Date: 2026-10-01. Compatible existing-setting correction: no new setting, path, schema, runtime, model, or provider.

The installed corpus grew past the default 10,000-file cutoff. A frozen source-only private copy contains 10,255 accepted physical sources, 23,565 chunks, and 14,222,653 accepted bytes. Collection succeeds in 22.28 seconds, process peak RSS 124,376 KiB. A metadata inventory of the broader knowledge tree is larger and is not treated as accepted corpus identity. Concurrent live collection with a diagnostic larger limit still refused a changed ancestor, and containment was preserved. These measurements do not qualify complete generation building or model memory.

Both default corpus.max_files and extraction.max_sources were 10,000, independently below the walk's existing 50,000-entry budget. Real 10,001-file collection and extraction tests reproduce both refusals. Both registry defaults now reuse the single existing MAX_CORPUS_INSPECTED_ENTRIES value. It is defined once in settings and imported under its existing name by the collector. Explicit smaller file/environment/caller limits still refuse. Byte, entry, directory, depth, heading, chunk, record, deadline, cancellation, DLP and ownership protections remain. No private configuration file is changed.

The numeric basis of the original discovery entry budget remains under the wider audit. This correction removes two extra default policies; it does not declare 50,000 optimal or measured safe for every corpus. More accepted input can cost more memory and time; existing caller deadlines and byte/record controls continue to refuse excessive work. Bounds may need explicit operator selection for a different deployment.

## Research and alternatives

Primary sources verified today: [Python directory iteration](https://docs.python.org/3.10/library/os.html#os.scandir), [OWASP resource controls](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/), and [MITRE uncontrolled resource consumption](https://cwe.mitre.org/data/definitions/400.html). They support tracking resource work and retaining bounded traversal, not a universal 10,000 or 50,000 value.

Rejected alternatives: invent a larger independent count, select a private operator override without establishing its basis, drop all count/byte safeguards, hide the refusal, or remove retained captured sources. The chosen default follows the walk's already enforced budget while operator-selected smaller quotas remain enforceable. Both pipeline stages must change together or the same corpus merely fails at the next stage.

## Qualification

Old implementation: two actual default failures and one explicit-limit control passing. The broader first candidate run exposed a fixture error: setting the environment after memory_state had loaded did not change its already-resolved root. The corrected fixture also sets that root to its private test vault; strict refusal assertions are retained. Evidence remains under private logs/audit-2026-10-01-corpus-default-*.

The source snapshot and all new fixtures remain private. Current model and historical retrieval findings are separate: a multilingual question can place a retained English passage below the dense candidate window. Increasing corpus capacity does not establish query accuracy, historical recovery, or all native lifecycle delivery. The original audit remains partial until those are qualified.
