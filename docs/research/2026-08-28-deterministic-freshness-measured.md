# Deterministic freshness, measured against the paper's protocol (`MEM-14`)

Date: 2026-08-28. Roadmap item `MEM-14`, section 12 of
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`: "воспроизвести протокол
arXiv 2606.01435 нашей supersession; заявить «без LLM в разрешении
конфликтов» с числом". Market context:
`docs/research/2026-08-27-number-one-memory-market-research.md`.

Stand: `benchmark/run_conflict_resolution.py` +
`benchmark/factconsolidation_data.py` +
`benchmark/build_conflict_fixture.py`, fixture
`benchmark/conflict-resolution-v1.json`. Tests:
`tests/test_conflict_resolution_stand.py` (17, green).

---

## 0. The paper is not the paper the roadmap names

Both the roadmap and the market note call this work **"Don't Ask the LLM to
Track Freshness: A Deterministic Recipe for Memory Conflict Resolution"**.
arXiv **2606.01435** — the identifier both documents give — is:

> **Reliable Post-Retrieval Assembly for Agent Memory: Separating Evidence
> Extraction from Policy Execution** — Vikas Reddy, Sumanth Reddy Challaram.

Same subject, different title. The content is the one `MEM-14` wants: it
evaluates on MemoryAgentBench FactConsolidation and compares an LLM-judgment
resolver against a deterministic one. I treated 2606.01435 as authoritative and
the roadmap's title as a paraphrase. **Someone should fix the two documents that
carry the wrong title** — I am not permitted to edit them in this task.

Its protocol, verbatim from the paper:

- Facts are numbered; "the original fact and its counterfactual variant are
  concatenated in order so the counterfactual appears with a higher serial
  number." The serial is a total order over observations.
- The resolver is *code*: "Freshness picking: ĉ = argmax_{c∈C} c.s, then return
  ĉ.e as the answer." The LLM's only job is candidate extraction.
- The baseline is the same pipeline with the LLM deciding, told newer facts
  carry larger serials.
- Abstention exists: "If C = ∅, return 'no answer'", and "we count 'no answer'
  as wrong under SubEM but in a production setting it is a calibrated
  abstention."
- Headline gap: "Δ (Headline − LLM-judgment) **+10.8 pp**", called "statistically
  reliable (non-overlapping 95% CIs at the marginal level; paired McNemar test
  χ² = 14.6, p < 0.001)".
- Stated limits: "The approach generalizes to any total order but cannot handle
  partial orders or causal dependencies between updates"; the +10.8 pp "is
  therefore a *pipeline-level* effect… cleanly isolating the resolver's own
  contribution is left to future work"; and on LongMemEval knowledge-update "the
  deterministic pipeline does not beat the LLM baseline; the 6.6 pp difference
  sits well inside the overlapping 95% CIs."

## 1. What I could reproduce, and what I could not

**Reproduced.** The dataset is public and I read the real bytes:
`Conflict_Resolution-00000-of-00001.parquet` from
`ai-hyz/MemoryAgentBench`, SHA-256
`24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45`, 8 rows
(single-hop and multi-hop × 6K/32K/64K/262K), 100 questions each — the paper's
"8 sub-task rows × 100 questions each = 800 test instances". This is the
paper's benchmark, not an analogue.

**Not reproduced, deliberately.** I did not rebuild the paper's *pipeline*
(retrieval + LLM candidate extraction + resolver) and so I do not restate its
78.0% / 94.8% / +10.8 pp. Reproducing a pipeline number would measure retrieval
and extraction, which `MEM-14` is not about, and it would inherit exactly the
confound the paper names: the +10.8 pp is pipeline-level and the resolver's own
contribution "is left to future work".

**What I did instead — and it is the thing the paper says is missing.** I
isolated the resolver. All arms receive a byte-identical candidate set, built
without any model by a fixed table of MQUAKE sentence frames
(`factconsolidation_data.FACT_FRAMES`). The *only* variable is who resolves the
conflict. On the single-hop 6K row: 455 numbered lines, **453 parsed, 2
unparsed**, 161 conflicting `(subject, property)` keys, every one of them
exactly two observations with two distinct values.

The 2 unparsed lines are one misspelled frame ("The origianl broadcaster of…")
whose two instances carry the *same* value, so they form no conflict. Reported,
not silently dropped.

**Scope.** Single-hop only. Multi-hop FactConsolidation asks compositional
questions ("the country of citizenship of the spouse of the author of…") whose
resolution is a chain of lookups, not one supersession decision; the paper's own
multi-hop numbers are 27–41%, and the failure there is composition, not
freshness. `MEM-14` is a claim about conflict resolution.

## 2. Ground truth, validated rather than assumed

Ground truth for a conflict is "the highest-serial value", which comes from how
the benchmark was *built*. Taking that on trust would make the deterministic
arm correct by definition, so I checked it against the dataset's own answers.

Of 161 conflicts, the answer set discriminates (contains exactly one of the
oldest and newest values) for **80**. On those:

| | n |
|---|---|
| highest-serial value is the dataset's answer | **72** |
| lowest-serial value is the dataset's answer | 8 |
| **agreement** | **72/80 = 90.0%** |

The 8 exceptions are not counterexamples. Every one is a case where the
*original* value is a common entity reachable by some other question in the
same row — `Canada/continent` (North America), `Australia/continent`
(Oceania), `United Kingdom/head-of-state` (Elizabeth II), four
`citizenship` keys, one `sport`. The gold set is a union over 100 questions,
not a per-conflict label, so a stale value can appear in it for an unrelated
reason. **90.0% is therefore a lower bound on ground-truth agreement**,
depressed by set-union confounding.

## 3. The deterministic arm, and why its 100% is not the finding

Each observation becomes a `claim/v1` record: `relation: has-value`
(single-valued in `bitemporal_claims.SINGLE_VALUED_RELATIONS`), the MQUAKE
property in the qualifiers (part of the bitemporal key), the serial carried in
`observed_at`, value normalised by the product's own `claims._normalize_value`.
`bitemporal_claims.as_of` then decides which belief survives.

**n = 161, source: MemoryAgentBench (real), block size 1:**

| arm | correct | wrong | abstain | accuracy |
|---|---|---|---|---|
| `vault` (`bitemporal_claims.as_of`) | 161 | 0 | 0 | **1.000** |
| `argmax` (the paper's resolver) | 161 | 0 | 0 | 1.000 |

On the 72 gold-confirmed conflicts: 72/72 for both.

**This 100% is true by construction and I will not sell it as a result.** The
ground truth is "latest observation wins" and the vault's rule is "latest
observation wins"; on a total order they cannot disagree. What the run does
establish is narrower and still worth having: this vault's supersession, fed the
paper's stream, reproduces the paper's resolver exactly — same answers on all
161 — so the vault's rule *is* the paper's recipe, expressed on an evidence
clock instead of a serial. The interesting measurements are §4 and §5.

## 4. Where the two rules diverge: the total-order assumption

The paper needs a total order and says so. This vault does not have a serial. It
has `observed_at`, which `claims._require_observation` binds to the daily block
the evidence was cited from, at **one-second resolution**
(`<daily-id>T<HH:MM:SS>Z`). Two claims lifted from one block share an instant,
and `bitemporal_claims._require_distinct_observation` then **refuses by name**:

> `bitemporal_ambiguous_observation: conflicting claims … share observation …;
> the evidence carries no order between them`

`--block-size k` models exactly that: k consecutive serials collapse into one
observation instant. k=1 is the paper's total order; larger k is a vault whose
compile parts yield several claims at once.

**n = 161, source: MemoryAgentBench (real):**

| block size | correct | wrong | abstain | abstention rate |
|---|---|---|---|---|
| 1 | 161 | 0 | 0 | 0.000 |
| 2 | 159 | 0 | 2 | 0.012 |
| 4 | 158 | 0 | 3 | 0.019 |
| 8 | 156 | 0 | 5 | 0.031 |
| 16 | 152 | 0 | 9 | 0.056 |
| 32 | 147 | 0 | 14 | 0.087 |
| 64 | 136 | 0 | 25 | 0.155 |
| 128 | 126 | 0 | 35 | 0.217 |
| 256 | 82 | 0 | 79 | 0.491 |
| 512 | 0 | 0 | 161 | 1.000 |

**The wrong column is zero at every granularity.** Coarser evidence costs the
vault answers, never truth. Every lost answer is a named refusal, not an error.

The alternative is not hypothetical. `argmax_observed` is the same
argmax applied to the clock the vault actually has — a plain "latest timestamp
wins", which is what most implementations do:

| block size | `vault` correct / wrong / abstain | `argmax_observed` correct / wrong / abstain |
|---|---|---|
| 1 | 161 / **0** / 0 | 161 / **0** / 0 |
| 32 | 147 / **0** / 14 | 147 / **14** / 0 |
| 256 | 82 / **0** / 79 | 82 / **79** / 0 |
| 512 | 0 / **0** / 161 | 0 / **161** / 0 |

Identical accuracy, opposite failure mode. Where the order is unreadable the
vault says so; `max` silently returns the value it happened to see first, which
in an ascending stream is the **stale** one. At block size 512 that is 161
confidently wrong current-value answers against 161 refusals. This instantiates
the paper's own limitation — "cannot handle partial orders" — and shows what the
vault does about it.

**Live-vault calibration for this axis.** Measured today with the product's own
`evidence_resolver.daily_entries` over `knowledge/daily/`: 10 files, 1,532,166
bytes, **6,024 entries, all of them citable**, of which **387 (6.42%) share
their `observed_at` with another entry the same day**. So collisions are real in
this vault at second resolution, at roughly the k=16–32 point of the sweep. Two
conflicting claims lifted from the *same* entry always tie and are always
refused.

## 5. The LLM baseline — and the claim does not survive it on accuracy

The baseline gets the *same* 161 conflicts and is told the rule outright:

```
Here is a list of facts. Newer facts have larger serial numbers, so when two
facts disagree the one with the larger serial number is the current one.

1. The chairperson of Fatah is Mahmoud Abbas.
232. The chairperson of Fatah is Moshe Kahlon.

Question: currently, what is the chairperson of Fatah?
Answer with the value only, on one line, with no explanation. If the facts do
not answer the question, reply exactly: NO ANSWER
```

This is the LLM's **best case**, and deliberately so: two candidates instead of
the paper's 455-fact context, the tie-break rule stated in the prompt, an
explicit abstention token offered. If the deterministic rule were going to win
on accuracy anywhere, it would not be here — and it does not.

**n = 161, source: MemoryAgentBench (real), block size 1, provider default
(chdir to a neutral directory before every call):**

| arm | correct | wrong | abstain | accuracy | Wilson 95% CI | total time |
|---|---|---|---|---|---|---|
| `vault` | 161 | 0 | 0 | **1.0000** | [0.977, 1.000] | 0.013 s |
| `llm` | 160 | **1** | 0 | **0.9938** | [0.966, 0.999] | 2247.4 s |

**Δ = 0.62 pp, and it is not a difference.**

- The `NEW-122` calibration floor is **8.7 points**; 0.62 pp is **14× below**
  it.
- Exact McNemar on the discordant pairs (b = 1, c = 0): **p = 1.0000**.
- The confidence intervals overlap across almost their whole width.

So: **"no LLM in conflict resolution" cannot be claimed as an accuracy win.**
On this task, at this isolation, the provider reproduces the deterministic rule
to within one question out of 161, which is noise. The roadmap asked for the
claim "with a number"; the honest number refutes the accuracy reading of it.

What the claim *does* survive on is everything else, and those margins are not
marginal:

| | `vault` | `llm` |
|---|---|---|
| accuracy | 1.0000 | 0.9938 (n.s.) |
| seconds per conflict | **0.000080** | 13.96 |
| **cost ratio** | — | **174,351×** |
| repeatable byte-for-byte | **yes** | no |
| can abstain on an unreadable order | **yes**, by name | not offered one |
| needs a provider, a network, a budget | **no** | yes |

This also matches the paper's own quieter result: on LongMemEval
knowledge-update "the deterministic pipeline does not beat the LLM baseline; the
6.6 pp difference sits well inside the overlapping 95% CIs." The +10.8 pp
headline is a *pipeline* effect, and my measurement is the resolver-only
comparison the paper left open. Isolated, the resolver's accuracy contribution
on this task is **zero within noise**. That is a real finding about the paper's
claim as much as about ours.

### 5.1 The one error is noise, and it is measured here rather than imported

A second full pass over the same 161 conflicts, same prompt bytes, same
provider:

| run | correct | wrong | accuracy | total time |
|---|---|---|---|---|
| `llm` run 1 | 160 | 1 | 0.9938 | 2247.4 s |
| `llm` run 2 | **161** | **0** | **1.0000** | 1643.0 s |
| `llm` pooled | 321 / 322 | 1 | 0.9969 | 3890.4 s |
| `vault` (both runs) | 322 / 322 | 0 | 1.0000 | 0.026 s |

**The single error did not reproduce.** The provider disagreed with itself on
1 of 161 items between two byte-identical passes — **0.62 points of
self-disagreement**, which is exactly the size of the vault-versus-LLM gap in
§5. The gap and the noise are the same magnitude because they are the same
thing.

This is the `NEW-122` phenomenon measured on *this* task rather than carried
over from another: 8.7 points on 23 questions there, 0.62 points on 161 here.
Any claim of a deterministic accuracy advantage on this benchmark would have
been an artefact of running the baseline once.

The vault's two runs are byte-identical to each other, which is not a lucky
result — it is what "deterministic" means, and it is the property being sold.

## 6. Cost

Best of 5 runs over all 161 conflicts, same machine:

| arm | total | per conflict |
|---|---|---|
| `vault` | 12.89 ms | **80 µs** |
| `argmax` | 0.20 ms | 1.3 µs |
| `llm` (322 calls, both runs) | 3890.4 s | 12.08 s |

Per-run the provider varied by 37% on wall clock alone — 13.96 s/call in run 1
against 10.20 s/call in run 2 — while the deterministic arm did not vary.

The deterministic resolver is **≈151,000× cheaper per conflict** and needs no
provider at all. For a memory system that resolves conflicts on every read, that
is the argument — not accuracy.

## 7. Honest limits

1. **One row.** Single-hop 6K. The 32K/64K/262K rows carry the same 455-fact
   list padded with distractors; padding changes retrieval, not the resolver,
   which is what this stand isolates.
2. **The deterministic 100% is construction-true** (§3). Only §4 and §5 carry
   information.
2b. **The LLM arm is the LLM's best case** (§5): two candidates, the rule given
   in the prompt, abstention offered. A harder condition — the full 455-fact
   context, the rule withheld — would very likely favour the deterministic rule,
   and the paper's +10.8 pp suggests it does. I did not measure that, because
   it stops being a resolver comparison and becomes a retrieval comparison.
3. **No pipeline number.** I do not claim 78.0% or +10.8 pp; I did not run the
   paper's pipeline and my numbers are not comparable to its table.
4. **Ground-truth agreement is 90.0% and is a lower bound** (§2), not a clean
   label set.
5. **The claims subsystem has never run in this vault.** Checked today with the
   product's own `claims.parse_claim_ledger` over all of `knowledge/`: **0 pages
   with a `## Claims` ledger, 0 claim records**, and `cache/claims.sqlite3` does
   not exist. Every number here is from the benchmark. The vault's conflict rule
   has never resolved a real conflict in this vault, and `bitemporal_claims`
   has **no production caller at all** — traced in the graph,
   `index_as_of` has 0 inbound callers and `as_of` is reached only by
   `index_as_of` and its own tests.
6. **`observed_at` is second-resolution and evidence-bound**, so the abstention
   rate is a property of how the compile pipeline chunks a day, not a tunable.
7. **Multi-hop untested** (§1).

## 7b. The claim, restated so it is true

`MEM-14` asked to "claim «no LLM in conflict resolution» with a number". The
number does not support the claim in the form it was asked. Restated to what was
measured:

> This vault resolves memory conflicts with no LLM, at **80 µs** per conflict
> against **13.96 s** for a provider asked the same question — **174,351×**
> cheaper — reproducing the provider's answer on **161 of 161** conflicts of the
> MemoryAgentBench FactConsolidation single-hop row, where the provider scores
> 160/161 and then 161/161 on a byte-identical repeat. The deterministic rule is
> **not more accurate** (Δ 0.62 pp, exact McNemar p = 1.0000, and the provider
> disagrees with itself by that same 0.62 pp between two runs).
> It is cheaper, repeatable, and it **refuses by name** when the evidence
> carries no order instead of returning a stale value: across a granularity
> sweep from 1 to 512 observations per instant it produced **0 wrong answers at
> every point**, converting 100% of unresolvable cases into named abstentions,
> where the same argmax on the same clock returned the stale value **161 times
> out of 161**.

That is a defensible claim with numbers behind every clause. The original
phrasing implied an accuracy advantage that this measurement does not find.

## 8. What remains

- `bitemporal_claims` is unreachable from the product. Until something calls it,
  "no LLM in conflict resolution" describes a mechanism, not a behaviour of the
  running system.
- The 8-exception analysis in §2 is by inspection, not by a per-question label
  set; a per-conflict linkage would tighten the ground truth above 90%.
- A tie-break that is *evidence-based* rather than refusing — byte offset within
  the daily file is a real, already-recorded total order — would convert the
  §4 abstentions into answers without asking a model. Not built: it is an
  architecture change and needs sign-off.

## Sources

- arXiv 2606.01435, *Reliable Post-Retrieval Assembly for Agent Memory:
  Separating Evidence Extraction from Policy Execution* — Vikas Reddy, Sumanth
  Reddy Challaram. `https://arxiv.org/abs/2606.01435`.
- Dataset: `ai-hyz/MemoryAgentBench`, `data/Conflict_Resolution-00000-of-00001.parquet`,
  SHA-256 `24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45`.
- This vault: `scripts/bitemporal_claims.py`, `scripts/claims.py`,
  `scripts/evidence_resolver.py`, `knowledge/notes/bitemporal-claims` design note
  `docs/research/2026-08-28-bitemporal-claims.md`.
- Calibration: `NEW-122` (2026-08-28) — byte-identical prompt, this provider
  disagrees with itself on 2 of 23 questions, **8.7 points**.
