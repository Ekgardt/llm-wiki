---
title: Wiki vs Memory Compiler vs Fusion
type: comparison
---

# Karpathy LLM Wiki vs claude-memory-compiler vs Fusion

One-sentence summary: Three approaches to giving an LLM durable, reusable knowledge outside the context window — differing in *what* they persist (curated source knowledge vs. session-derived behavior) and *who* drives compilation (explicit ingest vs. passive accumulation).

> [!note] Provenance caveat
> Only [[Karpathy LLM Wiki Workflow]] has a source-derived page in this vault. The characterizations of `claude-memory-compiler` and the fusion system below are inferred from operating conventions in this project (CLAUDE.md + the auto-memory instructions) rather than from an ingested raw document. Flagged as **preliminary** until a primary source lands in `inbox/`.

---

## 1. Karpathy LLM Wiki

**Description.** A human curates `raw/` inputs; an LLM incrementally compiles a markdown wiki (`wiki/`) with concept pages, entities, syntheses, and backlinks. Obsidian is the frontend. At ~100 articles the LLM answers by reading the wiki directly — no dedicated RAG. See [[Karpathy LLM Wiki Workflow]].

**Strengths**
- Human-readable artifact — the wiki is valuable on its own, independent of the LLM.
- Strong provenance: every claim traces back to `raw/`.
- Portable — plain markdown + wikilinks, no vendor lock-in.
- Scales naturally with deliberate curation; the graph structure surfaces gaps.

**Weaknesses**
- Requires active ingestion discipline; stale `inbox/` decays value fast.
- Direct-read breaks past some corpus size (open question in [[Karpathy LLM Wiki Workflow]]).
- Doesn't capture *how you work* — only *what you know*. Preferences, feedback, and session context are lost.

**Best when**
- Building a durable reference body (research notes, domain knowledge, project docs).
- You want the artifact to outlive any single LLM session or tool.

---

## 2. claude-memory-compiler

**Description.** An automated per-session memory layer (as specified in this project's operating instructions): the assistant writes typed memory files — `user`, `feedback`, `project`, `reference` — indexed by `MEMORY.md`, loaded at session start. Distilled passively from conversation, not from curated sources.

**Strengths**
- Zero-friction: accumulates without explicit ingestion steps.
- Captures *tacit* knowledge — preferences, prior corrections, working-relationship context — that source material cannot encode.
- Typed schema (user/feedback/project/reference) keeps retrieval targeted.
- Decay-aware: entries are short-lived and updated in place.

**Weaknesses**
- Low provenance — memories are distilled summaries, not citations.
- Session-scoped bias: what the LLM happens to observe, not what is objectively important.
- Can accumulate stale or wrong entries without review cadence.
- Not a shareable knowledge artifact — it encodes *you*, not the domain.

**Best when**
- Long-running collaboration where tone, preferences, and project context compound.
- Cross-session continuity matters more than citeable facts.

---

## 3. Fusion system (this vault)

**Description.** Combines both layers in one project: `wiki/` as the curated, provenance-bearing knowledge layer (Karpathy-style) *and* `memory/` as the session-distilled behavioral layer (compiler-style). Session memory index sits alongside the wiki index; CLAUDE.md enforces wiki-first retrieval while auto-memory runs in the background.

**Strengths**
- Clean separation of concerns: *facts with sources* vs. *working-relationship state*.
- Each layer reinforces the other — memory can point to wiki pages; wiki review can be triggered by patterns noticed in memory.
- Loses nothing: source knowledge stays citeable, tacit knowledge stays captured.
- Single vault, single frontend (Obsidian can view both).

**Weaknesses**
- Higher operational complexity: two sets of conventions, two indexes, two review cadences.
- Risk of overlap/drift — a fact may end up in both layers with divergent wording.
- Requires discipline about *which* layer gets a given update (source-derived → wiki; session-derived → memory).
- More surface area to keep healthy; neglect of either layer degrades the whole.

**Best when**
- You want both a durable public-shape knowledge base *and* a personalized assistant.
- The project is long-lived and you're willing to invest in two-layer hygiene.

---

## Decision table

| If your priority is… | Use |
|---|---|
| A citeable, shareable knowledge artifact | Karpathy wiki |
| Frictionless cross-session continuity and preference retention | memory-compiler |
| Both, and you accept the upkeep cost | Fusion |
| Quick one-off research project | Karpathy wiki (memory layer is overkill) |
| Long collaboration with no need for a reference artifact | memory-compiler alone |

---

## Recommendation

For this vault: **stay with the fusion approach** — the wiki is already established under Karpathy's conventions, and auto-memory adds behavioral continuity at negligible cost. The main risk is drift; mitigate by:

1. **Hard routing rule.** Source-derived claims → `wiki/`. Session-derived observations → `memory/`. Never duplicate.
2. **Cross-reference, don't copy.** Memory entries that reference domain facts should link to the wiki page, not restate it.
3. **Joint review cadence.** When running [[Review Workflow]], also audit `MEMORY.md` for stale entries.

If the memory layer stays empty in practice, collapse back to the pure Karpathy wiki — the overhead of a two-layer system isn't worth it without active use of both.

## Open questions
- At what corpus size does wiki direct-read fail, and does memory offload any of that pressure?
- Should memory entries be allowed to cite `raw/` directly, or only via `wiki/` pages?
- Is there value in a periodic "memory → wiki promotion" pass for observations that turn out to be durable facts?

## Source
- [[Karpathy LLM Wiki Workflow]] (wiki approach — source-derived)
- Project `CLAUDE.md` and auto-memory operating instructions (compiler + fusion — inferred, not yet source-derived)

## Related
- [[Karpathy LLM Wiki Workflow]]
- [[LLM Knowledge Base]]
- [[Ingestion Workflow]]
- [[Retrieval Workflow]]
- [[Review Workflow]]
- [[Preliminary Flagging]] — the convention this page's preliminary-caveat callout relies on.
- [[Memory Subsystem Action Plan]] — follow-up audit that acted on the fusion approach recommended here.
- [[Global Multi-Project Migration Plan]] — extends the fusion into a global multi-project model.
