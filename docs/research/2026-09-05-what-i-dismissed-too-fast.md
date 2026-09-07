# What I dismissed too fast: insects and plants

Dated 2026-09-05. Written because the owner refused two of my rejections, and
they were right about both.

## What I said, and why it was wrong

On 2026-09-02 I wrote that insects "gave no mechanism we lack" and that plant
epigenetic memory was not solid enough to build on. Re-reading my own note: the
first was a failure to look, and the second rejected a whole field on the
weakness of one corner of it.

**Insects.** I generalised from "good memory does the work early" and stopped.
The insect literature contains at least three mechanisms with published
algorithms attached, one of which is a direct competitor to the retrieval we run.

**Plants.** I rejected plant memory because associative-learning results
replicate badly. That is true and it is beside the point: the replicated,
quantitatively modelled mechanism in plants is *defence priming*, and it has
nothing to do with associative learning.

## Insects, mechanism 1: sparse expansive coding — and it is already an algorithm

The fly's olfactory circuit does three things. It **normalises** the input
divisively, centring the mean. It **projects into a much higher dimension**
through a sparse binary random matrix — 50 inputs to 2000 Kenyon cells. Then
**k-winner-take-all inhibition silences all but the highest-firing ~5%**.

That is FlyHash (Dasgupta, Stevens and Navlakha, *Science* 2017). It is a
locality-sensitive hash that runs the opposite way to the classical one: instead
of a short dense code it produces a **long sparse** one. Reported across three
datasets by mean average precision, it beats classical LSH, and the gap is
largest at short hash lengths — where budget is tightest, which is our case.

Follow-on work is active rather than historical: DenseFly (2018), bio-inspired
hashing that learns rather than using fixed random projections (ICML 2020),
soft winner-take-all variants (2026), and design-choice studies of
similarity-preserving sparse embeddings (2025).

**Why it is ours.** Our dense leg is one 384-dimensional embedding per chunk
compared by cosine. Expand-and-sparsify is a different trade: more dimensions,
almost all zero, cheap to compare, and better at small budgets. It needs no model
call and no training — the projection is random.

## Insects, mechanism 2: the same circuit as a novelty detector

The same expansion supports "a neural data structure for novelty detection"
(Dasgupta et al., 2018): a sparse high-dimensional trace makes *have I seen
something like this before* a cheap lookup rather than a scan.

**Why it is ours.** That is exactly the question our compile step asks of every
session and answers badly — the classifier returned FLUSH_OK for 50 of 50
sessions, which the vault's own self-report calls "may be too strict (losing
signal)". A novelty score is the missing input to that decision.

## Insects, mechanism 3: forgetting is regulated, and what is forgotten is not gone

Active forgetting in *Drosophila* runs through named machinery — the small G
protein Rac1, the dopamine receptor DAMB, the scaffold Scribble — and a dedicated
dopamine circuit; a 2020 *Nature* paper describes transient forgetting driven by
interfering stimuli just before retrieval. A 2026 *Nature Neuroscience* paper
goes further: forgotten memories persist as **silent traces** and a reminder cue
recovers them.

**Why it is ours.** Our archive policy deletes or hides by age. The biology says
the useful shape is *stop retrieving, keep recoverable, restore on cue* — which
is what our archives already are physically and are not yet in retrieval.

## Plants: priming is the mechanism, and it is well measured

Defence priming keeps a set of genes in a **poised state**: not expressed, but
prepared, so a later challenge is answered faster and harder. It is established
by chromatin reconfiguration and DNA-methylation changes, is maintained across
generations in documented cases, and has a **genome-scale quantitative model** of
its dynamics whose analysis found priming comes from parallel modulation of many
pathways rather than a sequential cascade. The same literature names its limits:
robustness comes from redundancy, and that redundancy also caps how much
resistance priming can add.

**Honest complication.** This is the same shape as trained immunity, which is
already item 6 on the worklist — memory as a changed disposition rather than
stored content. Plants do not add a fifth mechanism; they add a second,
independently evolved and quantitatively modelled instance of one we had already
chosen, and a warning about its ceiling. That raises confidence in item 6 rather
than lengthening the list.

## What I would do with this

1. **Try expand-and-sparsify on our dense leg.** It is a random projection and a
   top-k, no training, no model call, and it is measurable on the stand we
   already have. This is the one genuinely new candidate.
2. **Use novelty as the compile signal.** The classifier keeps everything; a
   sparse-trace novelty score is a cheap second opinion, and the vault's own
   diagnostics say the classifier is losing signal.
3. **Read the forgetting result into the archive policy** rather than the
   retrieval one: nothing should be deleted that a cue could bring back.

None of this changes the ranking of what is already open: retrieval finds the
right session 87% of the time, so a better hash competes for a bottleneck we do
not currently have. It is worth measuring, not worth assuming.

## Sources

- https://cseweb.ucsd.edu/~dasgupta/papers/fly-lsh.pdf — A neural algorithm for a
  fundamental computing problem (*Science*, 2017): normalisation, sparse random
  expansion 50→2000, 5% winner-take-all, and the comparison with LSH
- https://www.researchgate.net/publication/329394511_A_neural_data_structure_for_novelty_detection
  — the same circuit as a novelty detector
- http://proceedings.mlr.press/v119/ryali20a/ryali20a.pdf — bio-inspired hashing
  that learns, rather than fixed random projections
- https://arxiv.org/pdf/2501.14741 — design choices in similarity-preserving
  sparse randomized embeddings
- https://www.nature.com/articles/s41586-020-03154-y — a dopamine-based mechanism
  for transient forgetting
- https://www.nature.com/articles/s41593-026-02381-2 — forgotten information
  persists as silent traces and can be recovered
- https://www.annualreviews.org/doi/full/10.1146/annurev-arplant-042916-041132 —
  Defense priming: an adaptive part of induced resistance
- https://pmc.ncbi.nlm.nih.gov/articles/PMC4980392/ — epigenetic control of
  defence signalling and priming
- https://pubmed.ncbi.nlm.nih.gov/35822618/ — immune priming in plants, onset to
  transgenerational maintenance
