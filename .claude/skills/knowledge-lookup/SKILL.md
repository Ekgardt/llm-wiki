---
type: skill
name: knowledge-lookup
argument-hint: "[question or topic]"
description: Answer a question using the compiled wiki, with a three-tier retrieval strategy that scales with vault size (direct read → hybrid → QMD-primary).
allowed-tools: Read Glob Grep LS Bash(qmd *) Bash(python scripts/lookup_mode.py*)
title: "SKILL"
timestamp: 2026-07-03T05:41:37
---
Answer `$ARGUMENTS` using a retrieval strategy chosen by vault size. Karpathy's direct-read pattern is strong at small scale; QMD's hybrid lex+vec search earns its keep at larger scale. Don't pick one globally — let the page count decide.

## Step 0 — Pick the tier

Run once per session (or trust the last result):

```
python scripts/lookup_mode.py
```

This prints the recommended tier based on the curated wiki page count:

| Tier | Range | Strategy |
|---|---|---|
| **DIRECT** | < 50 pages | Read `wiki/index.md` + target pages. Skip QMD entirely — the LLM's own navigation is faster and cheaper. |
| **HYBRID** | 50–300 pages | Wiki-first, fall back to QMD only when the direct read is unconvincing or index navigation is ambiguous. |
| **QMD** | > 300 pages | `qmd query` primary. Read top-k results. Index becomes navigation, not retrieval surface. |

The helper also warns if the QMD index is stale.

## Tier: DIRECT  (current default for this vault)

1. Read `wiki/index.md`.
2. Pick 1–4 relevant pages based on section headings and wikilinks.
3. Read them.
4. Synthesize. Cite paths.
5. If the answer isn't there, fall through to `raw/` — the wiki is small enough that gaps are real gaps, not retrieval failures.

Do **not** invoke QMD in this tier. It adds latency without improving recall at <50 pages.

## Tier: HYBRID

1. Read `wiki/index.md`. Form a hypothesis about which 2–3 sections are relevant.
2. Read the top candidate pages.
3. **If the read is convincing** — answer and stop.
4. **If ambiguous** — run `qmd search --md --full "<exact keywords>"` for BM25 matches, then `qmd query "<natural-language question>"` for hybrid lex+vec ranking. Read the top 3 hits that the index did not already surface.
5. **If the index is stale** (last-updated > 24h per `lookup_mode.py` / `qmd status`), run `qmd update` before step 4. Re-embed with `qmd embed` only if new pages were added since the last embed pass.
6. Synthesize across all consulted pages. Cite paths.

## Tier: QMD

1. `qmd query "<natural-language question>"` — trust the hybrid ranker's top-8.
2. Read those pages (use `qmd get qmd://wiki/<path>` for batch fetch via `qmd multi-get`).
3. Consult `wiki/index.md` only for cross-section navigation (e.g. "are there related syntheses I missed?"), not for primary retrieval.
4. If top-8 is noisy, narrow with a typed query: `qmd query 'lex:<keywords>\nvec:<paraphrase>'` (see `qmd query --help`).
5. Synthesize. Cite paths.

At this tier, always verify the index is fresh (`qmd status` → "updated <1h ago"). Stale indexes silently degrade recall.

## Response rules (all tiers)

- Prefer synthesis over quotation.
- Explicitly state uncertainty — tag inferred claims as such (see [[Preliminary Flagging]]).
- Mention which wiki pages or source files drove the answer.
- If you discover durable missing knowledge, recommend updating the wiki or do so when asked.
- If the answer is non-obvious and likely to be asked again, offer to file it back via `/knowledge-qa-file-back`.

## When to bump tier

Re-run `python scripts/lookup_mode.py` when the wiki has grown substantially (new sections added, a large ingestion batch finished). The current vault is ~15 curated pages (excluding editorial metadata and per-project `state.md` scaffolding) — DIRECT is correct until it crosses 50. The `wiki/projects/<slug>/state.md` pages do not count toward the tier threshold since they're auto-updated metadata, not curated knowledge.
