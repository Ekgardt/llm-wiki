---
title: Product Requirements in the AI Era
type: concept
confidence: medium
source_authority: web
status: preliminary
---

# Product Requirements in the AI Era

One-sentence summary: A 2026 synthesis of how product/business requirements shift toward short, prompt-native specs (PRO), iterative simulation-before-code, and AI-augmented discovery — and where AI still loses to deterministic classical methods.

## The shift (thesis)
- **Spec = prompt.** Heavy 20-page PRDs are obsolete for AI features; the **PRO one-pager** (Prototyping Requirements One-Pager, Product School — *⚠️ verify*) is ≤500 words, 8 sections, written as a prompt from the start.
- **Requirements are the ROI lever.** MIT NANDA's ~95% zero-ROI-from-GenAI stat (*⚠️ verify*) points to requirement quality, not model choice, as the deciding factor.
- **Velocity = moat.** Short iterative specs, cycles, named owners, simulate-before-code (Linear Method, Shape Up).

## Method → format → tool mapping
- Discovery → JTBD/Continuous Discovery (Opportunity Solution Tree) → research-synthesis tools.
- Spec → **PRO** (AI features) / Lean PRD / PR-FAQ / Pitch / RFC → LLM-assisted authoring.
- Behavior → BDD/Gherkin → eval suites (LangSmith/Braintrust/Promptfoo).
- Contract → OpenAPI / GitHub Spec Kit (backend authoritative; frontend types generated).
- Sync → drift detection (PRD ↔ design ↔ tickets).

## Where AI loses to classical (critical)
- Deterministic **rules** beat LLMs where behavior is fully specifiable — cheaper, exact, auditable.
- **Finance/billing/legal math**: RAG + code compute; LLM is renderer only.
- **Access control / PII**: code with permission checks, never an agent.
- **Discovery without human contact is empty** — AI synthesizes interviews but doesn't replace them.
- **No evals = release blind** — mandatory for any AI feature.

## Copy-paste artifacts
The full, copy-pasteable reference — method cards, document templates (PRO, Lean PRD, PR-FAQ, Pitch, RFC, ADR), the prompt library **C1–C8**, five workflows, a worked Discovery→PRO example, checklists, and a risk/OWASP-LLM table — lives in a separate research artifact (not shipped in public source).

## Open questions
- Which 2026 source claims (PRO dating, exact quotes, the 95% ROI stat) survive primary-source verification — capture into `raw/` when confirmed.
- How much of the prompt library (C1–C8) to productize into reusable skills vs. keep as paste-in templates.
- Whether PRO generalizes to non-AI features or stays AI-specific.

## Source
- Prior verified research session (July 2026) — specific 2026 facts flagged **⚠️ verify** in the originating research session §0 and §12.
- Canonical methodologies: Ulwick (ODI/JTBD), Teresa Torres (OST), Amazon (Working Backwards/PR-FAQ), Basecamp (Shape Up), Jeff Patton (Story Mapping), Evans (DDD), North/BDD.
- OWASP LLM Top-10 (*⚠️ verify current edition*).

## Related
- [[Karpathy LLM Wiki Workflow]] — same "source material → curated, linked knowledge" philosophy, applied here to a research body.
- [[Preliminary Flagging]] — why the 2026 source-specific claims are marked preliminary until verified against primary sources.
- [[LLM Knowledge Base]]
- [[Ingestion Workflow]]
