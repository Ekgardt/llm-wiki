# The repository ships no memory — 2026-09-10

**Decision (owner, 2026-09-10).** «Примеры знаний точно не нужны … убери всё
ненужное пользователю из репозитория.» Nothing under `knowledge/` that is
somebody's memory is published any more: the 89 pages under
`knowledge/notes/` (61 architecture decisions of this product and about 25
demonstration pages), the two synthetic daily logs, and `knowledge/log.md`
leave the repository. `knowledge/index.md` stays tracked as the empty
skeleton the index builder renders for a vault with no pages, because
`CLAUDE.md` imports it on every session start. The directory READMEs and
`knowledge/projects/_template/state.md` stay: they describe the layout and
scaffold a project; they are not memory.

**Why.** Issue #19: a fresh install treated the shipped pages as the user's
memory — 89 "curated pages" before the user wrote one, and the first session
opened with another project's guard rails (`.gitkeep`…). The owner's vault and
the public source are one directory (single-directory decision, 2026-08-21),
so the owner's own decision pages were published as "examples". For a user
they are nothing; for the owner they remain in place, private, exactly as
every other page under a denied directory.

**What the field does.** Karpathy's llm-wiki pattern starts the wiki from
the user's own first page
(https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f); Basic
Memory ships an onboarding skill and an empty tree, not sample notes
(https://docs.basicmemory.com/integrations/skills); Obsidian keeps its help as a
separate vault the user opens on purpose. No memory product studied ships
another person's memory as the default content.

**What is kept where.** The architecture decisions remain the owner's private
pages and the references to them in `CLAUDE.md`, `docs/STRUCTURE.md` and the
research notes stay as they are: they name the owner's record, and the
contracts themselves are stated in those documents. Tests that read the
decision pages from the repository are removed; tests of the contracts in
`CLAUDE.md`, `docs/STRUCTURE.md` and the specs stay
(`tests/test_structure.py`, `tests/test_quality_guards.py`,
`tests/test_audit_fixes.py`, `tests/test_index_publishes_only_public_pages.py`).
`rebuild_memory_index.py` reads the allowlist as before; with only
`README.md` allowlisted it publishes nothing, which is the point.

**Not done here.** The corpus of the memory index (issue #29.2, the product's
own code in `recall`) is a separate decision and note.

**Follow-up found by CI.** `benchmark/run_benchmark.py --legacy-only` ran in
CI over `git ls-files knowledge/notes` with a Recall@5 = 100 % gate on the
sixty frozen queries; with only the README tracked it would have measured
nothing and failed. The legacy-60 and current-generated gates are retired
(the README's historical table with them; the numbers stay in
`benchmark/baseline-2026-07-16.md`), and `run_benchmark.py` is now only the
retrieval-v2 entry point. The synthetic `retrieval-v2.json` corpus and the
LongMemEval stand are the measurements that remain, and both are
self-contained.
The vault-application stand (`benchmark/run_vault_application.py`, seven
cases whose gold pages and expected tokens were the owner's decision pages)
goes the same way, and the daily-heading reader test now exercises the
producer in a temporary vault instead of reading this repository's daily logs.
