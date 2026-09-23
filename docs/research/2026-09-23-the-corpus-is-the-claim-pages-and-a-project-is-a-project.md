# The corpus is the claim pages, and a project is a project

Dated 2026-09-23. Findings B1 and B2 of `docs/AUDIT-2026-09-23-live.md`. The owner
delegated the decision ("примени правило 2 и 4"), so this note does the research and
decides.

Files: `scripts/corpus_snapshot.py`, `scripts/session_start_project_state.py`,
`tests/test_corpus_snapshot.py`, `tests/test_slug.py`, `tests/test_project_journal.py`,
`tests/test_a_project_is_a_directory_the_owner_works_in.py`, `docs/STRUCTURE.md`,
`docs/research/2026-09-23-the-corpus-is-the-claim-pages-and-a-project-is-a-project.md`.

## What was found

- The live memory generation holds 4 846 chunks: 2 906 come from
  `knowledge/projects/*/journal.md` (9 381 780 bytes of one-event-per-line checkpoint JSON),
  984 from `state.md`/`context.md`, 956 from `knowledge/notes` (592 744 bytes). By bytes the
  index is 94 % journal. A warm search takes 2.38 s on an idle machine (5.7–6.3 s under
  a load of 4), of which the cross-encoder reranker takes 2.2 s (5.0–5.7 s) over ten
  candidates.
- `corpus_snapshot._walk_knowledge` already refuses session records with a measurement:
  importing them "moved the vault stand from hit@5 0.7 to 0.0". Journals were admitted
  through `PROJECT_FILES` with no measurement. The accepted decision
  `claim-readers-do-not-scan-the-journal` (2026-09-10) names `state.md` and `context.md` as
  the claim pages and the journal as the event log no claim reader consults.
- `knowledge/projects/` holds 85 directories. Projects were minted for a benchmark run
  directory under `cache/`, a transaction directory under `run/`, a pytest temp directory
  under `/tmp`, the provider's own temp directory (`llm_client` runs the CLI in
  `tempfile.TemporaryDirectory(prefix="llm-wiki-provider-")`), the home directory, and the
  vault itself (2 937 events). The journal path `integration_adapter._project_context` names
  a project after the hook payload's `cwd` through `_compute_slug`, which refuses only the
  vault root exactly; session start, by contrast, refuses the home directory and creates
  nothing without a project marker (`_has_project_marker`), but the journal path never asks.

## Practice on this date

- Distractors are not free. Cuconasu et al. show that documents related to the query but
  not answering it degrade RAG accuracy and must be kept out of the retriever's pool
  ([The Power of Noise: Redefining Retrieval for RAG Systems, SIGIR 2024, arXiv 2401.14887](https://arxiv.org/abs/2401.14887)).
  Checkpoint JSON shares every project's vocabulary with the pages about that project.
- A cross-encoder scores each (query, passage) pair through the full model, so its cost is
  linear in the passage tokens it reads; `BAAI/bge-reranker-v2-m3` reads up to 512 tokens per
  pair ([model card](https://huggingface.co/BAAI/bge-reranker-v2-m3)). Journal chunks fill
  that bound; note chunks mostly do not.
- The platform names its temporary directory: `tempfile.gettempdir()`
  ([Python 3 documentation](https://docs.python.org/3/library/tempfile.html#tempfile.gettempdir)).
  Anything created there is scratch by construction, including our own provider directory.
- Precedent in this vault: `owning_checkout` (2026-08-26) stopped agent worktrees from
  minting projects after "46 of 61 project journals were named `agent-<hash>`"; issue #20
  (2026-09-10) stopped the vault root. Both fixed one instance of the same class: a directory
  that is not the owner's project became one.

## The decisions

1. **The memory generation carries the claim pages, not the journal.** `PROJECT_FILES`
   becomes `state.md` and `context.md`. `journal.md` and its rotated parts stay on disk,
   authoritative, greppable, consolidated nightly — exactly as session records are kept but
   not indexed. Nothing else changes: the project journal, the checkpoint path and the
   claim readers are untouched.
2. **A directory is a project only if the owner could be working in it.** One rule in
   `_compute_slug`, so every caller (session start, session end, prompt and tool capture,
   bootstrap, the journal path) agrees: a directory that is the vault or inside it, under
   the platform's temporary directory, or the home directory raises `NotAProject`
   (a `ValueError`, so callers that already catch `ValueError` keep their behaviour).
   Session start's marker rule is deliberately not extended to the journal path: every
   observed junk class is covered by the three path rules, and a marker rule would change
   what happens for the owner's own marker-less directories, which nothing measured.
3. **Existing junk directories are not deleted by code.** They are the owner's files under
   `knowledge/projects/`; the audit lists their classes, and once journals leave the corpus
   their weight in retrieval is their `state.md` alone.
4. **Measured, not argued.** A scratch generation is built from the live knowledge with the
   new collector and the owner's 27 real questions are run against both generations:
   reranker time, and how many of the 12 candidates are notes. The numbers go into this note
   before the change is pushed.

## Cost, by rule 4

The collector reads two files per project instead of three and skips the largest; the
generation shrinks by about 9 MB of text and its vectors accordingly. `_compute_slug` gains
three path comparisons. No provider call, no new module, no new runtime directory.

## Measurement

Scratch generation built from the live knowledge with the new collector into a scratch
state root (`build_scratch.py`, 122 s): 254 sources, 1 970 chunks, against the live
generation's 333 sources and 4 846 chunks. The owner's 27 real questions were then run
against each generation in turn after the build and a first, contended arm had finished,
with nothing else of this session running; same code, same models, warm:

- with journals (live generation): 2.38 s per question, reranker 2 203 ms, 11.22 of the 12
  candidates are notes, 0.74 are journal chunks, best reranker score −8.50;
- without journals (scratch generation): 1.87 s per question (−21 %), reranker 1 805 ms
  (−18 %), 11.89 notes, 0 journal chunks, best reranker score −8.55.

The 5.7–6.3 s figure in the audit was measured while a 4-process walk measurement was
still running (load average 4.3 on 4 cores); the idle figure for the live generation is
2.38 s. No gold exists on the live vault, so the reranker's best score is the only
quality proxy, and it did not move. What moved: the index shrank by 59 % of its chunks
and 9 MB of JSON, and no checkpoint event can take a candidate slot from a page.
