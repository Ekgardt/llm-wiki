# What an answer should admit about itself

Date: 2026-08-24
Reason: MEM-05. The grounded answer already cites byte spans and already has an
abstention status, and then the envelope that carries it says
`coverage: 0.0` and `"Grounded answer coverage is unknown."` on every single
answer — a constant, regardless of what the vault knows about the pages it just
cited. Before replacing that number I checked what the practice measures and what
it refuses to claim.

## What is already true here

`scripts/mcp_server.py::_grounded_answer_quality` returns `coverage 0.0`,
`confidence = min(0.8, verified_claim_ratio)` and one fixed warning. Meanwhile
every cited page carries `type`, `source_authority`, `confidence`, `status` in
its frontmatter, the trust table already weighs the first two
(`scripts/provenance.py`), and `scripts/archive_stale.py::TYPE_AGE_DAYS` already
states how long a page of each type stays current. None of it reaches the reader.

## What the practice says

**Citations are the trust contract made visible.** The 2026 practice reports that
roughly 80% of the perceived-quality gain from faithful RAG comes from citation
UX — hover-to-verify, click-through, source panels — not from the generation
step, and that production stacks log which chunks contributed to each answer so
failures can be debugged. This vault already does the hard half (verified spans);
what it withholds is the cheap half.

**A confidence number must come from something.** The signals current work uses
are the model's own certainty, answer/evidence similarity, and the separation of
reranker scores. Verbal self-reported confidence is the weakest of them: recent
work on noise-aware calibration exists precisely because a model's stated
confidence is unreliable under retrieval noise. A constant, as here, is worse
than all of them — it is not a signal at all.

**Source reliability is a separate axis from citation correctness.** A system
that cites flawlessly still depends on whether the cited source deserves trust;
the curated-corpus trade-off is that coverage is narrower but provenance is
knowable. This vault is the curated case, so the provenance it knows should be
part of the answer rather than an internal ranking detail.

## What follows for this repository

The envelope stops inventing and starts reporting what is on disk:

* **coverage** — the share of claims whose citations all came back verified. That
  is a real measurement of the answer, and it is what the existing helper already
  computes and then throws away.
* **confidence** — the claim coverage, multiplied by the weakest cited page's own
  stated `confidence` (`high` 0.9, `medium` 0.7, `low` 0.4, absent 0.6), then
  capped by the same 0.8 ceiling the code already applies. The weakest page
  decides because a chain of claims is only as good as its worst evidence.
* **warnings** — concrete sentences naming the page: low stated confidence,
  inferred authority, or a page past the archive window its own type declares
  (`TYPE_AGE_DAYS`, e.g. `debugging` 60 days, `pattern` 180). No blanket
  "coverage is unknown" — a warning that appears on every answer is one nobody
  reads, which is the lesson already recorded for the session-start health block.

What is deliberately not claimed: nothing here judges whether the cited span
*entails* the claim. The citation-relevance gate checks shared content, not
entailment, and that limit is stated in `citation-relevance-gate-decision`; this
change reports provenance and coverage, not truth.

The type-age windows move from `scripts/archive_stale.py` to `scripts/okf_types.py`,
whose docstring already claims to be the single source of truth for type
taxonomy, so the answer path and the archiver read the same numbers.

## Sources

- [Evaluating RAG Reliability under Clean, Misleading, and Mixed Retrieval, arXiv 2606.07783](https://arxiv.org/html/2606.07783) — retrieved content can be plausible and wrong; citation correctness alone does not establish reliability.
- [NOVA: Noise-aware Verbal Confidence Calibration for RAG, arXiv 2601.11004](https://arxiv.org/pdf/2601.11004) — self-reported confidence is unreliable under retrieval noise.
- [Model Internals-based Answer Attribution for Trustworthy RAG, arXiv 2406.13663](https://arxiv.org/pdf/2406.13663) — attribution as the mechanism that makes an answer checkable.
- [RAG System Metrics: Recall, Precision, Faithfulness 2026, Digital Applied](https://www.digitalapplied.com/blog/rag-system-metrics-recall-precision-faithfulness-2026) — chunk attribution in production; the share of perceived quality that comes from citation UX; confidence signals in use.
- [Curated retrieval versus open web search: a coverage-trust trade-off, arXiv 2607.05217](https://arxiv.org/pdf/2607.05217) — a curated corpus buys knowable provenance at the price of coverage.
