# A ledger of things and events, posted once

Dated 2026-09-22. Mechanism 2 of the plan the owner approved today
(`docs/research/2026-09-22-a-class-level-plan-to-pass-the-field.md`, item 7, with the two
rules the chemistry report added and the warning the biology report added, see
`docs/research/2026-09-22-what-the-sciences-really-lend-a-memory.md`). The owner said yes to
this architecture change on 2026-09-22; the decision page is
`knowledge/notes/ledger-of-things-and-events-decision.md` (private, as every page is) and
`docs/STRUCTURE.md` names the table and its lifecycle.

Files: `scripts/ledger.py` (new), `scripts/fact_keys.py`, `scripts/search_memory.py`,
`scripts/evidence_graph_builder.py`, `benchmark/longmemeval_vault.py`,
`benchmark/ledger_offline.py` (new), `docs/STRUCTURE.md`, `CHANGELOG.md`,
`tests/test_a_ledger_posts_once_and_counts_by_code.py` (new),
`tests/test_the_ledger_rides_in_the_generation.py` (new),
`tests/test_an_entity_page_opens_on_the_second_day_and_is_extended_by_code.py` (new),
`tests/test_the_offline_ledger_reads_the_question_not_the_label.py` (new),
`tests/test_an_error_is_not_an_answer.py` (the store double learns `post`).

## What was found in the code

- The compile-time provider call already exists: `fact_keys.key_turns` asks one batched
  question per 25 user turns in the nightly window and stores the reply under the turn's
  span hash in `cache/fact-keys/keys.sqlite3`; a turn is keyed once. The generation build
  copies the keys into the `keys` column of `search.sqlite3` (`_nightly_keys`) and nothing
  else reads the store. The reply shape is `{"<turn id>": ["fact", ...]}` and
  `_clean_keys` reads a list.
- The generation's search artifact is validated by table shape (`generation_metadata` and
  `chunks` only, `_valid_table_shapes`) and by rebuilding the chunk rows; an additional
  table in the same file is neither rejected nor a new artifact name, so it needs no
  manifest or seal change. A new file in the generation directory would need
  `_V2_OPTIONAL_ARTIFACTS` and the seal.
- The aggregation trigger (`aggregation_pass.asks_to_aggregate`, `counted_kind`) reads the
  question text and yields the head noun the question counts; that is the ledger's `kind`.
- The recorded 2026-09-18 LongMemEval run has 233 rows whose question asks for a count or a
  total; 216 are not abstention twins. Of these the reader answered 27 wrongly and 181
  rightly, 8 were refused. LoCoMo 2026-09-19 has 6 such rows, 4 wrong. No fact keys of those
  runs exist on disk (`cache/benchmarks/**/keys.sqlite3` is absent: the stand keyed only
  when `LLMWIKI_BENCH_FACT_KEYS=1` and the per-question state root was disposable), so an
  offline proof cannot use model-written records.
- The number of dataset-labelled evidence turns equals the gold count in 24 of 216 counting
  questions (5 of the 27 wrong ones): the labels mark where the answer is, not one turn per
  counted instance, so they are no oracle for a count.

## Design decisions

1. **A record** is (kind, canonical thing, event, day, quantity, whether the user said it,
   whether the day was stated, pointer to the source bytes). The pointer is the same one
   the keys carry: daily path, source digest, byte span, span digest; the `daily:... sha256:...
   block:... bytes:...` reference of `evidence_resolver` is rendered from it at citation time.
2. **Post once.** The posting key is the digest of kind, thing, event, day and pointer;
   `INSERT OR IGNORE` makes a re-run or a re-keyed turn post nothing twice.
3. **Same event.** Two records of one kind and one thing whose days lie within 30 days are
   one event unless there is evidence of a new one (a different stated quantity or a
   different event). The 30 days are the CDC's majority de-duplication window. The pair
   decision is a Fellegi–Sunter sum of field weights (thing, event, day, quantity), a
   deterministic pre-merge; the constants are stated in the module and frozen.
4. **Count by code.** `ledger.count(connection, kind, window)` returns events, distinct
   things, the tier ("confirmed" when every record is the user's own words with a stated
   date; "probable" otherwise, so the answer can say "at least N") and the pointers.
   `ledger.reconcile(stated, counted)` compares the reader's number with the ledger's and
   flags a mismatch instead of trusting either. Zero provider calls, zero tokens at
   question time.
5. **Recurrence gate.** `ledger.recurring(connection)` lists things with records on two or
   more days; the entity page is created or extended only then, with dated pointer lines
   appended by code under `## Ledger`, never rewritten by a model.
6. **Storage.** One table, `ledger`, inside the generation's `search.sqlite3`, copied from
   `cache/fact-keys/keys.sqlite3` at build time exactly as the keys are. Disposable and
   derived. A search artifact built before the table existed has none, and a reader then
   answers "no ledger" rather than zero.
7. **Extraction** rides in the existing fact-keys call: the reply may carry, per turn,
   `{"facts": [...], "records": [...]}`; the old list form is still read. The records are the
   model's words, like the keys; what is read to the person remains the turn.

Honest limit, from accounting's trial balance: a ledger cannot count what capture never
wrote, and it cannot count what the extraction never posted.

## The offline measurement

Without recorded records, the stand-in posts one record per user turn that names the
counted kind (thing = the noun phrase ending in the kind, event = "", day = the session
day) over the whole haystack — the complete enumeration a top-k reader never has — and
counts by code; the figure is the stated quantities when the person gave any, else the
distinct things. This measures the counting half with a zero-model extractor; the model's
records are what only a run can settle.

Result (judged rows whose question asks for a count or total, abstention twins excluded;
`benchmark/ledger_offline.py` over the recorded staging vaults):

| run | wrong before | fixed | still wrong | undecided | out of scope | right before | kept | broke |
|---|---|---|---|---|---|---|---|---|
| LongMemEval 2026-09-18 | 35 (27 answered, 8 refused) | 6 | 22 | 5 | 2 | 181 | 8 | 138 |
| — tune half | 20 | 5 | 14 | 1 | 0 | 79 | 6 | 73 |
| — decide half | 15 | 1 | 8 | 4 | 2 | 102 | 2 | 65 |
| LoCoMo 2026-09-19 | 4 | 0 | 3 | 0 | 1 | 2 | 1 | 0 |

The six fixed: bikes serviced in March, movie festivals, furniture, albums, projects, weeks
ago. The reconcile flag fired on 19 of 20 wrong answers and 141 of 151 right ones. A
mention of "bike" is not a bike and "20 playlists" is one thing: with a stand-in extractor
the ledger must stay off the answer path; the reader places its count, tier and pointers
beside the question as data and records the reconciliation in the result row. The model's
records are the thing a run has to show.

## Sources

Each was opened on 2026-09-22 by the research agents whose reports (RESEARCH-A, D, F in the
job scratch) quote the passages below; the links are the ones the reports give.

- LongMemEval, ICLR 2025, arXiv 2410.10813, https://arxiv.org/abs/2410.10813 — "For the
  multi-session reasoning questions, further representing the values with the extracted
  facts improves the QA accuracy"; for other types facts-as-values hurt. Peer-reviewed.
- CDC, *Case De-duplication Guidance*, June 2016,
  https://ndc.services.cdc.gov/wp-content/uploads/de-duplication-guidance-june2016.pdf —
  "The majority of jurisdictions de-duplicate case data in 30 days"; de-duplicate when the
  prior report lies within the window and there is no evidence of re-infection. Guidance.
- Fellegi–Sunter record linkage as applied in JMIR 2022,
  https://pmc.ncbi.nlm.nih.gov/articles/PMC9562057/ — field agreement weights are log
  likelihood ratios summed over fields; a threshold declares a match. Peer-reviewed.
- Double-entry bookkeeping, https://en.wikipedia.org/wiki/Double-entry_bookkeeping — the
  trial balance "is a partial check": it catches double posting and inconsistency, not
  omission. Reference.
- Sun, Advani, Spruston, Saxe, Fitzgerald 2023, *Nat Neurosci*,
  https://pubmed.ncbi.nlm.nih.gov/37474639/ — consolidate only what recurs. Peer-reviewed.
  Zhang et al. 2026, https://arxiv.org/abs/2605.12978 — model-rewritten consolidation falls
  below no-memory. Preprint.
- GlobalQA, https://arxiv.org/abs/2510.26205, and EC-Bench, https://arxiv.org/abs/2603.29943
  — counting fails on an incomplete set, not on arithmetic; enumeration F1 and counting
  accuracy, ρ = 0.692. Preprints.
