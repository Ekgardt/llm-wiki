# The memory index holds memory — 2026-09-10

**Finding (issue #29.2).** On an installed vault the active generation held
7465 chunks; 609 (8 %) came from `knowledge/`, the rest from `docs/`,
`tests/`, `scripts/` and `benchmark/` of the llm-wiki checkout itself,
because the vault and the source are one directory and the vault generation
collects `APPROVED_CODE_ROOTS` beside `knowledge/`. A user asking `recall`
about their own decision competes with the product's test suite. It is also
the first cause named for the RU→EN failure (#29.3): the Russian text the
encoder preferred lived in `docs/`.

**Owner's decision (2026-09-10).** «Убери всё ненужное пользователю»: the
product's own code and documents do not belong in a user's memory index.

**What the field says.** Distracting documents that are topically related
but do not answer the question are the ones that lower RAG accuracy the
most; random noise is comparatively harmless (Cuconasu et al., "The Power of
Noise", SIGIR 2024, https://arxiv.org/abs/2401.14887; the 2026 replication "The Powerless Noise", https://arxiv.org/abs/2607.03615, withdraws the claimed benefit of random noise under modern prompting but not the harm of distractors). The checkout's docs
are exactly that kind of distractor for a question about the vault's own
subject matter. Karpathy's llm-wiki pattern indexes the wiki, not the tool
that maintains it (https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

**What the product already has.** Code intelligence runs on repositories
through their own generations (`scripts/repository_index.py`,
`get_architecture` modes `index`, `repositories`, `summary`, `snippet`), and
`get_architecture` / `find_dead_code` read a directory directly. Nothing
that answers a code question reads code out of the vault generation. The
generation manifest records `policy.code_roots`, so a generation without code
is an ordinary generation (`fact_keys.py` already builds one). `provenance.py`
keeps `APPROVED_CODE_ROOTS` as the list of what counts as a code-shaped path.

**Decision.** The vault generation collects `knowledge/` only:
`corpus_snapshot.VAULT_CODE_ROOTS = ()` is read by the builder
(`scripts/doctor.py`), the freshness probe (`scripts/freshness_watch.py`)
and the grounded-answer snapshot (`scripts/query_memory.py`), so the three
cannot disagree. The owner indexes this checkout as a repository like any
other when working on the product. The next nightly publishes the smaller
generation; nothing is deleted by hand.

**Measure after.** Re-run the users' four RU→EN queries against the new
generation before touching the encoder (#29.3): the fixture in
`2026-09-10-a-question-in-russian-and-a-note-in-english.md`.
