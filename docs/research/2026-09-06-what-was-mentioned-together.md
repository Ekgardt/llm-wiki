# What was mentioned together

Dated 2026-09-06. Written before adding a signal to retrieval, because it adds a
signal to retrieval.

## The problem it is for

Multi-session questions are the ones whose answer is assembled from more than one
conversation. On our stand they are the largest category and among the weakest.
The failure is not that the right session is missing — retrieval returns the
labelled answer session for 87% of questions — it is that the *second* session,
the one that completes the answer, never comes back with the first.

## The biological shape

The Venus flytrap has no nervous system and still counts. A touched hair emits an
electrical signal; the signal is *accumulated*; the accumulation *decays* over
minutes; and when it crosses a threshold the trap closes. Memory as a decaying
accumulator with a threshold, built out of nothing but chemistry.

Applied here: two things named in one entry raise each other's weight, and the
raise fades if it is not renewed.

## What the field does with the same idea

The idea is standard and current, under the name spreading activation.

- **TIGRAG** (2026) builds a token co-occurrence graph from sliding-window
  statistics and expands a query through bridging entities found in what was
  already retrieved — precisely the second-session problem.
- **Query-aware spreading activation** (2026) spreads from seed entities
  extracted from the question, gating each step by the cosine similarity between
  the candidate's description and the question, so activation does not leak into
  everything reachable.
- **EHRAG** (2026) combines structural co-occurrence with latent semantic
  similarity through hypergraph diffusion.

The common lesson across all three is the one the flytrap already states: an
activation that spreads without a decay or a gate reaches the whole graph and
means nothing. Every one of these papers spends its design effort on the brake,
not on the spread.

## What we will build

**Co-occurrence from what the vault already writes.** Wikilinks are explicit:
`[[a]]` and `[[b]]` in one entry is an author's own statement that these belong
together. No extraction model, no token windows, no new text processing.

**A decayed weight.** Each co-occurrence contributes, and its contribution
halves with age. A pairing seen once a year ago is nearly nothing; one seen
three times this week is a real link.

**A gate.** Only the neighbours of candidates that already rank well are raised,
and only by a bounded factor, exactly as the citation disposition is bounded.
Spread from everything to everything is the failure this literature warns about.

**Derived and disposable.** The table lives under `cache/`, is rebuilt from the
vault, and nothing depends on it existing. It is not authority; Markdown remains
the authority.

## What is not claimed

That it will help. Retrieval already finds the right first session most of the
time, and this aims at the second one, which we have never measured separately.
The number to watch is the multi-session category, and it will be watched rather
than assumed.

## Sources

- https://arxiv.org/abs/2606.30093 — TIGRAG: token co-occurrence graphs, and
  query expansion through bridging entities
- https://arxiv.org/abs/2606.30133 — query-aware spreading activation with a
  per-step semantic gate
- https://arxiv.org/html/2604.17458 — EHRAG: structural co-occurrence with
  hypergraph diffusion
- https://arxiv.org/html/2512.15922v1 — spreading activation for document
  retrieval in knowledge-graph RAG
- `docs/research/2026-09-02-four-mechanisms-worth-stealing.md` — the flytrap as
  a decaying accumulator with a threshold
