# Expand and sparsify: the fly's trick, and what we would use it for

Dated 2026-09-06. Design note for the one genuinely new candidate that came out
of re-examining the insect literature after the owner refused my dismissal.

## The mechanism, precisely

The fly's olfactory circuit does three things, in order:

1. **Divisive normalisation** — the input is centred so that overall intensity
   stops carrying information and only the pattern does.
2. **Sparse random expansion** — about 50 projection-neuron inputs fan out to
   roughly 2000 Kenyon cells through a *sparse binary random* matrix. The
   dimension goes up, not down.
3. **k-winner-take-all** — feedback inhibition silences all but the highest-firing
   ~5%.

Published as an algorithm in *Science* 2017 (Dasgupta, Stevens, Navlakha) and
known as FlyHash. It is locality-sensitive hashing run backwards: classical LSH
produces a short dense code, this produces a **long sparse** one. Measured by
mean average precision on three datasets it beats classical LSH, and the margin
is largest at short hash lengths.

The follow-on work is alive rather than historical: DenseFly (2018), bio-inspired
hashing that learns the projection instead of drawing it at random (ICML 2020),
soft winner-take-all variants (2026), and design-choice studies of
similarity-preserving sparse randomised embeddings (2025).

## Two uses, one implementation

**Novelty.** The same circuit was published separately as a neural data structure
for novelty detection (Dasgupta et al., 2018): with a sparse high-dimensional
trace, *have I seen something like this before* is a lookup rather than a scan.
Our compile classifier returned FLUSH_OK on **50 of 50** sessions and the vault's
own diagnostics say it "may be too strict (losing signal)". A novelty score is
the second opinion it has never had.

**Retrieval.** Our dense leg is one 384-dimensional embedding per chunk compared
by cosine. Expand-and-sparsify is a different trade — more dimensions, almost all
zero, cheap to compare — and it is better where the budget is small.

Both need the same primitive, so it is written once.

## The parameters we will use, and why

- **Input**: the 384-dimensional `intfloat/multilingual-e5-small` embedding we
  already compute. No new model.
- **Expansion ×20 → 7680.** The fly's ratio is about 40; published follow-ups use
  10 and upward. Twenty keeps the code inside a few kilobytes while leaving the
  expansion clearly super-linear.
- **Sparsity 5%**, giving 384 active positions — the fly's own figure, and the
  one every follow-up keeps.
- **Fixed seed.** The projection is drawn once and pinned, because a code is only
  comparable to codes drawn from the same matrix. Changing the seed invalidates
  every stored code, so the seed is part of the format.

## What we will not claim

That this improves retrieval. Our own measurement says retrieval already returns
the right session for 87% of questions, so the bottleneck is elsewhere and this
competes for a problem we may not have. It is written because it is cheap,
needs no training and no model call, and because the *novelty* use has a
measured defect waiting for it — not because the retrieval gain is expected.

## Sources

- https://cseweb.ucsd.edu/~dasgupta/papers/fly-lsh.pdf — the algorithm, the
  50→2000 expansion, the 5% winner-take-all, and the comparison with LSH
- https://www.researchgate.net/publication/329394511_A_neural_data_structure_for_novelty_detection
- http://proceedings.mlr.press/v119/ryali20a/ryali20a.pdf — bio-inspired hashing
  with a learned projection
- https://arxiv.org/pdf/2501.14741 — design choices in similarity-preserving
  sparse randomised embeddings
- https://arxiv.org/pdf/1907.11959 — modelling winner-take-all competition in
  sparse binary projections

---

## Verdict on the retrieval use, 2026-09-06: not worth building

Checked before building it, and the premise does not hold.

**FlyHash is measured against classical LSH, and we do not use LSH.** LanceDB is
not installed on this machine (`have_lancedb()` is False), so the dense leg falls
through to `_numpy_dense_hits`, which is one matrix multiply against every vector
and an exact top-k. Measured: **0.29 ms per query over 10 531 vectors of 384
dimensions.**

So the comparison is not "sparse code versus classical LSH" — where the fly wins
— but "sparse code versus the exact answer", where an approximation can only
lose accuracy. Its gain is speed at a scale we are three orders of magnitude
away from.

Building it would replace a correct, fast computation with an approximate one,
and the honest thing is to say that rather than ship it because it was on a list.
The primitive stays: it cost little, its properties are pinned by tests, and the
day this vault holds millions of chunks the trade reverses.

**What the check also produced** is the more useful finding, recorded in
`scripts/sparse_code.py`: the embedding space is narrow — unrelated notes at
cosine 0.767 to 0.923, median 0.842 — so every similarity threshold in this
system has to be relative rather than absolute. That applies to the novelty use
as well, which is why it is not being built to a fixed cut either.
