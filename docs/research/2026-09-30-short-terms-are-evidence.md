# Short terms are evidence

Date: 2026-09-30. Law 9 continuation; no configuration or architecture boundary
changes. The citation gate discarded every word shorter than three characters.
A claim consisting of `Go`, `C`, `IP` or `R` consequently had no tokens and
accepted an unrelated citation without checking overlap. Eight new checks fail
on the original implementation: four mismatches and four missing content tokens.
Evidence: `logs/audit-2026-09-30-continuation-short-terms-red.txt`.

Primary sources checked today: [scikit-learn text features](https://scikit-learn.org/stable/modules/feature_extraction.html#text-feature-extraction),
[Elasticsearch stop analyzer](https://www.elastic.co/docs/reference/text-analysis/analysis-stop-analyzer),
[NLTK tokenization](https://www.nltk.org/api/nltk.tokenize.html).
These treat tokenization and stop-word filtering as separate choices and describe
their consequences. They do not establish that a short identifier is irrelevant
or that token overlap proves a claim. No new NLP dependency is introduced.

Choose the existing Unicode word tokenizer without a minimum length; retain CJK
bigrams and extend the explicit stop-word set to common short English/Russian
function words formerly removed implicitly by length. Preserve all figure,
flag, cross-script and source-span checks. Relevant short technical terms can
now participate in the necessary overlap test. An unrelated citation cannot
bypass that test merely because the claim is short.

Raising/lowering the length threshold moves the defect; a list of privileged
technical acronyms would be a fragile special case. A statistical language model
would add latency and still need evaluation; it is unnecessary for this
deterministic necessary-condition repair. Explicit stop words are a linguistic
heuristic and must be reviewed when a real term collides with them. This change
does not turn overlap into semantic entailment or claim whole-answer quality.

Qualification includes positive/negative short-term cases, function-word-only
overlap, existing figure/citation/translation regressions, Lizard and branch-shape
checks; the native repository index is refreshed after installation.

Installed-checkout verification: **106 tests passed in 30.82 seconds**, including
grounded answers, citation figures, cross-script cases, output budgets and real
Lizard/branch-shape checks. Ruff passed. Evidence:
`logs/audit-2026-09-30-continuation-short-terms-green.txt`. The complete candidate
suite running in the isolated checkout predates this separate repair and is not
claimed as validation of it.
