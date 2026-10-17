# Decode native events once per validation

Research date: 2026-10-07. Runtime qualification uses CPython 3.10.20.

The historical-capture profile showed repeated JSON decoding inside native-event validation. Regression tests reproduced two decodes for one ordinary event, one event inside a breadcrumb part, and each of the two source-authority consumers. These tests measure the original redundant work, rather than elapsed-time thresholds.

The shared decoder classifies the outer JSON record and passes that same record to canonical validation. Every invocation still parses its input anew. Canonical encoding, schema, event identity, complete physical evidence, current source checks and pending-source rejection remain required. Malformed native fragments retain their original JSON decoding error. Generic JSON remains outside the native-event domain.

Caching an accepted proof across calls was rejected: external source and authority checks must remain fresh. Removing validation or repairing malformed input was also rejected. The replaced private decoder has no remaining product consumers and is removed. The public boolean classifier remains available to its existing callers.

This eliminates duplicate parsing inside these calls. It does not establish that the complete health check meets its deadline or that audit point 7 is complete. No model calls are needed for this component's regression checks.

Primary sources:

- [Python 3.10 JSON documentation](https://docs.python.org/3.10/library/json.html): decoding and JSONDecodeError behavior.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259): JSON text and parser requirements; parsing is not provenance validation.
- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/): provenance connects an entity to its derivation and source; mechanical parsing does not replace source evidence.

Local evidence: the original failing regression reports, exact changed-callable complexity report and related regression reports are retained privately under `logs/`. No runtime or knowledge evidence is published with this document.
