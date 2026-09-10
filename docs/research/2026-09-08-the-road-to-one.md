# The road to 1.0 — what actually stands between run 1 and a perfect score — 2026-09-08

Owner's direction: no Ollama arm for now; aim at 1.0 and take the levers
that bring us closest to it. This note replaces guesswork with the error
classes of run 1 and what the literature says cures each.

## 1. The ceiling is not 1.0, and where it really is

- The official judge (GPT-4o) agrees with human experts on >97% of labels
  (arXiv:2410.10813 §meta-evaluation; a GPT-4o-mini re-check on 100
  questions gave 95%, κ=0.897 — AMA-Bench, arXiv:2602.22769). About 3% of
  every score is judge noise, so a real system tops out near 0.97.
- Oracle GPT-4o — the gold sessions handed to the reader — scored 82.4%
  on LongMemEval-S (paper; Emergence AI blog). Today's leaders are at 0.95
  with stronger readers (GPT-4.1, gpt-5-mini). The last decade of points is
  reading, not finding.
- Retrieval is close to solved with plain hybrid search: BM25 + MiniLM
  gives recall@10 = 98.6%, recall@20 = 99.4% on LongMemEval-S
  (https://github.com/rohitg00/agentmemory/blob/main/benchmark/LONGMEMEVAL.md);
  a local FTS5 + vectors + graph RRF stack reaches recall@10 = 0.955
  (https://dev.to/bozbuilds/perfect-retrieval-recall-on-the-hardest-ai-memory-benchmark-running-fully-local-5dhc).
  Ours: right session in the twelve 98%.
- Verbatim chunks beat extracted facts and summaries: 82–85% vs 65–72% on
  LongMemEval in a controlled ablation (arXiv:2601.00821). Our raw-value
  design is the right one; Mem0-style extraction is not the road to 1.
- Emergence AI: match on turns, retrieve whole sessions, rank sessions by
  NDCG of their turns after a cross-encoder, chain-of-thought before the
  answer — 82.4% with GPT-4o, 86% with their best reader, above the oracle
  (https://www.emergence.ai/blog/sota-on-longmemeval-with-rag).
- Other 2026 entries: Memanto 89.8% (typed schema, arXiv:2604.22085),
  TiMem 76.9% (arXiv:2601.02845), HORMA (22% of the tokens, arXiv:2606.11680),
  MemReranker-4B, a reasoning-aware reranker beating bge-reranker
  (arXiv:2605.06132). None above 0.954.

## 2. Our errors, by the shape of the question (run 1, 186 answerable)

```
shape             n   right  wrong  silent  accuracy
count / sum       76    53     12     11      0.70
date arithmetic   24    16      2      6      0.67
preference        12     8      2      2      0.67
fact lookup       74    62      3      9      0.84
```

Read one by one (`second-look-n200-seed101-r1.judged.jsonl`):

- **Counts undercount.** Every wrong multi-session answer is "2" for a gold
  of 3, "3" for 4: instruments 2/4, tanks 2/3, festivals 3/4, delivery
  services 2/3, dinner parties 2/3, feed 50 lb/70 lb. The model counted
  what it saw; one instance per question never reached it. Silences of the
  same shape: "the evidence supports only one half of the comparison"
  (Japan+Chicago, HelloFresh, Facebook comments, charity goal).
- **Dates are narrated, not computed.** "How many days ago" answers
  describe the event and its date and stop, or the time window is applied
  strictly: "four weeks ago" resolved to one exact day whose only evidence
  is a file header → `unsupported_time_scope`. Gold allows ±1 day; the
  questions mean "about four weeks".
- **Preference questions refuse for lack of event evidence** ("no listing
  of cultural events this weekend"), while the rubric only asks that the
  answer use what the user said about themselves.
- **Knowledge-update refuses on conflict** (two weekdays for the cocktail
  class → `conflicting_evidence`), where the type's rule is "the latest
  wins"; "before the Air Fryer" needs the previous value, not the current.
- **Fact-lookup silences** are the gate dropping a single claim whose
  evidence is there in a neighbouring sentence (stand mixer, tennis racket).

## 3. What cures each class — the levers that move us toward 1

| class | cure | expected |
|---|---|---|
| counts | gather to saturation: pull every session about the entity (fact keys + entity query), deliver whole sessions, **enumerate every instance with its date and citation, then count** — enumerate-then-count is the reading fix, saturation is the retrieval fix | 0.70 → ≥0.90 |
| dates | the model names the event date with a citation; **code computes** the days/weeks (deterministic, ±1 day tolerated); relative windows widened to ±25% of the span | 0.67 → ≥0.92 |
| preference | never refuse: retrieve the user's stated traits and answer from them; the gate checks the persona citation, not the event | 0.67 → ≥0.92 |
| knowledge-update | latest-dated claim wins on conflict; "before X" returns the value superseded by X — both already in the compiled layer's supersession | 0.72 → ≥0.95 |
| lookups | answer mode on the stand plus claim-guided re-retrieval (failed claim → new query, once) | 0.84 → ≥0.95 |

Weighted over run 1's mix, the five together land at roughly 0.92–0.94;
the whole-session and fact-key levers of the plan carry the rest toward
the 0.97 ceiling. These are estimates from the counts above, not
measurements.

## 4. What this changes in the plan

- Ollama arm: parked at the owner's direction.
- New levers, ahead of everything except runs 2–3: enumerate-then-count
  with saturation retrieval (counts are 41% of the questions and our
  worst class), deterministic date arithmetic, persona-grounded
  preference, latest-wins for knowledge-update.
- Kept from before: whole-session delivery, fact keys, time filter,
  answer mode, claim-guided loop, both layers on the stand.
- Each lever: its own dated note if it changes design, one run of 200,
  keep if gain > 0.035.

## 5. The search engines, revisited (owner's question, same day)

`docs/research/2026-09-07-what-the-search-engines-do.md` found four things
and then shelved three of them on one measurement: "the answer was in the
prompt equally often for right and wrong answers (57% vs 58%), so retrieval
is not the discriminator". Section 2 above shows that measurement was too
coarse: for counts, *one* instance being in the prompt reads as "gold in
prompt", while the missing instance is exactly what makes the count wrong.
Retrieval of the *set* is the discriminator for 41% of the questions. So:

- **Google's fan-out** (one question → 5–11 concrete sub-queries, run in
  parallel, merged) is the saturation retrieval of lever 15. For "how many
  festivals": queries for each festival named so far, "film festival",
  "attended", by month. Adopted, as the retrieval half of lever 15.
- **Perplexity's plan-then-execute, first layer tuned for recall** is the
  reading half of lever 15: enumerate, then count; and widen the first
  layer (24 candidates) before the cross-encoder narrows. Adopted.
- **Yandex Spectrum, serve several readings instead of choosing** is the
  answer to "what if the shape detector is wrong": run the plain prompt and
  the enumerate prompt both, adopt the one whose claims verify. Also the
  model for stand answer mode beside product refusal. Adopted as the
  fallback rule inside lever 15.
- **Baidu, the graph as a reasoning step before the prompt** — "how many
  different X" is a query to structure. Our graph leg never fires today;
  fact keys (lever 2) give it entities to fire on. Adopted with lever 2.

What stays unadopted: Baidu's ERNIE-style retriever training (needs
labelled pairs we do not have) and a learned router.
