---
type: skill
name: knowledge-lookup
argument-hint: "[question or topic]"
description: Answer a question using the compiled wiki, with a three-tier retrieval strategy that scales with vault size (direct read → hybrid search → vector-primary).
allowed-tools: Read Glob Grep LS Bash(uv run python scripts/lookup_mode.py *) Bash(uv run python scripts/search_memory.py *)
title: "Knowledge Lookup"
timestamp: 2026-07-03T05:41:37
---
Answer `$ARGUMENTS` using a retrieval strategy chosen by vault size. Karpathy's direct-read pattern is strong at small scale; hybrid BM25+Vector search earns its keep at larger scale. Don't pick one globally — let the page count decide.

## Step 0 — Pick the tier

Run once per session (or trust the last result):

```
uv run python scripts/lookup_mode.py
```

This prints the recommended tier based on the curated wiki page count:

| Tier | Range | Strategy |
|---|---|---|
| **DIRECT** | < 50 pages | Read `knowledge/index.md` + target pages. Skip search — the LLM's own navigation is faster and cheaper. |
| **HYBRID** | 50–300 pages | Wiki-first, fall back to `search_memory.py` only when the direct read is unconvincing or index navigation is ambiguous. |
| **QMD** | > 300 pages | `search_memory.py --semantic` primary. Read top-k results. Index becomes navigation, not retrieval surface. |

The helper also warns if the search index is stale.

## Tier: DIRECT  (current default for this vault)

1. Read `knowledge/index.md`.
2. Pick 1–4 relevant pages based on section headings and wikilinks.
3. Read them.
4. Synthesize. Cite paths.
5. If the answer isn't there, fall through to `knowledge/raw/` — the wiki is small enough that gaps are real gaps, not retrieval failures.

Do **not** invoke search in this tier. It adds latency without improving recall at <50 pages.

## Tier: HYBRID

1. Read `knowledge/index.md`. Form a hypothesis about which 2–3 sections are relevant.
2. Read the top candidate pages.
3. **If the read is convincing** — answer and stop.
4. **If ambiguous** — run `uv run python scripts/search_memory.py "<keywords>"` for BM25 matches, then `uv run python scripts/search_memory.py "<natural-language question>" --semantic` for hybrid lex+vec ranking. Read the top 3 hits that the index did not already surface.
5. **If the index is stale** (last-updated > 24h per `lookup_mode.py`), run `uv run python scripts/search_memory.py --rebuild` before step 4.
6. Synthesize across all consulted pages. Cite paths.

## Tier: QMD (>300 pages)

1. `uv run python scripts/search_memory.py "<natural-language question>" --semantic` — trust the hybrid ranker's top-8.
2. Read those pages.
3. Consult `knowledge/index.md` only for cross-section navigation (e.g. "are there related syntheses I missed?"), not for primary retrieval.
4. Synthesize. Cite paths.

At this tier, always verify the index is fresh (`uv run python scripts/lookup_mode.py` → check index age). Stale indexes silently degrade recall.

## Response rules (all tiers)

- Prefer synthesis over quotation.
- Explicitly state uncertainty — tag inferred claims as such (see [[Preliminary Flagging]]).
- Mention which wiki pages or source files drove the answer.
- If you discover durable missing knowledge, recommend updating the wiki or do so when asked.
- If the answer is non-obvious and likely to be asked again, offer to file it back via `/knowledge-qa-file-back`.

## When to bump tier

Re-run `uv run python scripts/lookup_mode.py` when the wiki has grown substantially (new sections added, a large ingestion batch finished). The current vault is ~30 curated pages (excluding editorial metadata and per-project `state.md` scaffolding) — DIRECT is correct until it crosses 50. The `knowledge/projects/<slug>/state.md` pages do not count toward the tier threshold since they're auto-updated metadata, not curated knowledge.
