# Verify the same bytes once

Date: 2026-08-24
Reason: measured, not suspected. A search on this vault takes 11.4 s, and 9.75 s
of it is `_valid_generation_fts` — called **five times in one query**, 1.95 s
each. The MCP operation budget is 10 s, so the agent's own read path cannot
finish: it times out, falls back to lexical, and the fallback has no budget left
either. This is what `tests/test_mcp_server.py::test_search_vault_returns_list`
started failing on.

## What the profile says

One `search(limit=8, semantic=False, graph=False, rerank=False)` on the live
vault (1222 sources, 6240 chunks), warm process:

    total                                    11.37 s
      _valid_generation_fts (5 calls)         9.75 s   (1.95 s each)
      everything else, including retrieval    1.62 s

With the semantic leg the whole query is ~35 s, and with the cross-encoder ~40 s.
The reranker is an optional stage that a deadline abandons, so it is not the
blocker; the mandatory validation is.

## Why it is repeated

`GenerationCatalog._validate` runs the full semantic validation of a generation
on every read — `get_active_for_repository` → `_get_active` →
`_registered_generation` → `_validate` — and each retrieval touches the catalog
several times (fallback selection, the seal, the connection). Each validation
walks all 6240 chunk rows of `search.sqlite3` and rebuilds the rows it expects
from the generation's own sources.

The verdict cannot differ between those five calls. The check is a pure function
of the generation's bytes: `_generation_authoritative_sources` reads the
generation's own `source-manifest.json` and `evidence.sqlite3`, never the live
vault, and `_ArtifactScan` hashes every artifact and compares it against the
manifest **before** the semantic check runs. Same bytes, same answer.

## What the practice says

Content-addressed identifiers are the primary artifact reference precisely
because "a content-addressed artifact is immutable by definition: changing any
byte changes the address". The corresponding cache is standard: a verification
gate "caches acceptance decisions keyed by artifact hash", and once verified,
later invocations skip the expensive check with the hash serving as the key;
invalidation happens when the inputs to the decision change (policy version, key
rotation). Derived artifacts are keyed by content digest plus the version of
whatever produced them.

## What follows for this repository

`_validate_generation` remembers, per process, that a given set of bytes
validated, keyed by:

* the generation id,
* the SHA-256 of its `manifest.json`,
* the verified digest of every artifact, as `_ArtifactScan` just computed them.

Every call still hashes every artifact — the key is earned, not assumed — so a
changed byte changes the key and the semantic check runs again. The cache is
bounded to a handful of generations and holds only booleans; nothing else about
the generation is memoised. What is skipped on a hit is exactly the work whose
inputs are proven identical.

What this does not change: publication, activation, `doctor`, and any caller of
`validate_generation_fts_artifact` still validate directly; the seal that a
reader holds during a query is unaffected; and the first query in a process still
pays the full check.

## Sources

- [Immutable Artifacts, MinimumCD Practice Guide](https://beyond.minimumcd.org/docs/migrate-to-cd/pipeline/immutable-artifacts/) — content-addressable digests as the primary reference; immutable by definition.
- [Certified Purity for Cognitive Workflow Executors, arXiv 2605.01037](https://arxiv.org/pdf/2605.01037) — a verification gate that caches acceptance decisions keyed by artifact hash, and what invalidates them.
- [CI/CD Lab 07: Artifact Signing and SLSA Provenance](https://pras-labs.medium.com/ci-cd-lab-07-artifact-signing-and-slsa-provenance-in-gitlab-ci-788af4ecb594) — signing by digest binds a decision to exactly one artifact.
- [Context Engineering for Commercial Agent Systems, Jeremy Daly](https://www.jeremydaly.com/context-engineering-for-commercial-agent-systems/) — derived artifacts keyed by object id, content digest, and producer version.
