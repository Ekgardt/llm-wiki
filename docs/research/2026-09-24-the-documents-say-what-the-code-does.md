# The documents say what the code does

Dated 2026-09-24. Audit items B-1, B-2, B-5, B-9, B-10, C-8 and C-13
(`docs/AUDIT-2026-09-24-live.md`).

Files: `README.md`, `README.ru.md`, `README.zh-CN.md`, `docs/USER-GUIDE.md`,
`docs/ARCHITECTURE.md`, `docs/STRUCTURE.md`, `docs/DEVELOPER-AUDIT-STATUS-2026-08-14.md`,
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, `docs/ROADMAP-v5.md`, `scripts/doctor.py`,
`tests/test_the_documents_say_what_the_code_does.py` (new), `tests/test_doctor.py`,
`pyproject.toml`, `CHANGELOG.md`, `docs/AUDIT-2026-09-24-live.md`,
`docs/research/2026-09-24-the-documents-say-what-the-code-does.md`.

## What was found

- **B-1.** The three READMEs (`:293`, `:296`, `:284`) tell a reader to keep
  `cache/index.sqlite` and the vector cache and say retrieval falls back to them; USER-GUIDE
  says search "falls back to the legacy BM25 index", its migration steps 2 and 6 contradict
  each other, and its rollback says retrieval "resumes through legacy FTS/vector/Lance
  paths". The legacy index was retired on 2026-09-23 and LanceDB on 2026-09-07; the code
  reads none of them (`docs/STRUCTURE.md:677`). The same README paragraph says no embedding
  model or reranker is selected; the product pins `intfloat/multilingual-e5-small`
  (`embedding_model.py`) and `BAAI/bge-reranker-v2-m3` (`reranker.py`) and uses both by
  default.
- **B-2.** ARCHITECTURE (`:312`), USER-GUIDE (`:674`) and STRUCTURE (`:236`) say the system
  performs no automatic Git operation; the nightly update fetches and fast-forwards, which
  the owner allowed on 2026-08-23 for that one bounded case (CLAUDE.md §1).
- **C-8.** USER-GUIDE and STRUCTURE show `build_context.py --slug`; the slug is positional.
- **Cognee.** STRUCTURE (`:195`) says the Cognee extra "are removed during implementation";
  they are gone.
- **C-13.** `DEVELOPER-AUDIT-STATUS-2026-08-14.md`, `…-08-18.md` and `ROADMAP-v5.md` open with
  a branch, a tag and a registry that no longer exist and no sign they are history.
- **B-5.** The private decision `derived-evidence-generation-decision.md` is still `active`
  and says the legacy caches "remain readable during migration"; the newer
  `the-generation-is-the-only-index-decision.md` does not supersede it (rule 12), so a
  question about the retirement is answered with the old page first.
- **B-10.** The audit said backup is "not wired in". Checked: the nightly reclaim step takes a
  Git snapshot of `knowledge/` into `~/llm-wiki-snapshots/` (no remote, by the owner's
  choice; `snapshot_knowledge.py`), the last one at 2026-09-24 03:00. Restic
  (`private_vault_backup.py`) is the optional encrypted off-machine path the owner declined.
  What is missing is a signal: no doctor check reads the snapshot, so a failing snapshot
  would be silent.
- **B-9.** `pyproject.toml` says 4.0.0; `[Unreleased]` holds about a thousand lines and the
  READMEs tell a reader to install `v4.0.0`, which has none of the fixes since 2026-08-25.

## Practice on this date

- Keep a Changelog 1.1.0: "Keep an Unreleased section at the top to track upcoming changes",
  moved into a dated version section at release (https://keepachangelog.com/en/1.1.0/,
  fetched 2026-09-24); Semantic Versioning 2.0.0: MINOR "when you add functionality in a
  backward compatible manner" (https://semver.org/, fetched 2026-09-24). The changes since
  4.0.0 add functionality and change no public contract a caller relies on — 4.1.0.
- A backup is only as good as the signal that it ran: freshness of the last snapshot is the
  check (the same "period plus grace" rule as the scheduled passes,
  `2026-09-24-the-weekly-pass-has-its-own-record.md`).

## The decisions

1. Every sentence above is corrected in all three READMEs, USER-GUIDE, ARCHITECTURE and
   STRUCTURE; the three READMEs change together (release rule), checked by
   `tests/test_readme_i18n.py`. A test pins that no document tells a reader to keep the
   retired caches or that the system performs no automatic Git operation.
2. The three historical documents get a dated banner naming them history and pointing to
   the current audit.
3. The old decision page is marked `superseded` with `superseded_by`, in the private vault.
4. Doctor gets a `backup` check: the snapshot repository's last commit is at most two days
   old (a nightly period plus a day of grace); absent or older is `degraded`.
5. Version 4.1.0: `[Unreleased]` becomes `[4.1.0] — <date of merge>`, `pyproject.toml` says
   4.1.0, and the READMEs name `v4.1.0`. The tag is created on the merged commit.

## Sources

- Keep a Changelog 1.1.0 — https://keepachangelog.com/en/1.1.0/ — fetched 2026-09-24.
- Semantic Versioning 2.0.0 — https://semver.org/ — fetched 2026-09-24.
- `git -C ~/llm-wiki-snapshots log -3` on the live machine, 2026-09-24.
