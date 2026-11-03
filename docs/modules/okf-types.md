# OKF page taxonomy

`okf_types` owns the existing durable/inbox type sets, aliases, never-archive set
and page-age defaults. It is standard-library-only and has no runtime state or
IO. Constants retain their current values and mutability: this qualification does
not turn mutable mappings into read-only proxies or rewrite accepted page types.
Consumers normalize and validate pages; this owner does not parse frontmatter,
move files or decide an archive transaction.

The user taxonomy and retention values are stated in AGENTS.md. Preserve the
separation of claim-candidate inbox staging from durable notes. Tests must cover
the complete published sets/mappings, disjoint staging, aliases and consumer
validation. Existing mixed schema tests remain integration obligations.
Production source stays byte-identical at scripts/okf_types.py; no compatibility
alias is introduced. Qualification evidence is recorded in the existing-leaf
research. Run `pytest tests/modules/okf_types tests/test_claim_schemas.py -q`.

Shared canonical contract: [Maintenance qualification](context-tooling.md).

## Structural reference retained from the overview

### Existing leaf owners

The existing `okf_types`, `host_transcripts`, `ephemeral_paths`,
`embedding_model` and `inference_threads` files are qualified in place,
with distinct complete packets and retained consumer tests. Their production
paths, bytes, host configuration, model pins and process-local state ownership
stay unchanged. See
[research](../research/2026-11-03-existing-leaf-owner-qualification.md).

Full contracts: [okf types](okf-types.md), [host transcripts](host-transcripts.md), [ephemeral paths](ephemeral-paths.md), [embedding model](embedding-model.md), [inference threads](inference-threads.md).
