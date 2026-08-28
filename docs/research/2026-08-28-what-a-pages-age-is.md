# What a page's age is (`NEW-127`)

Date: 2026-08-28. Rule 2 research for one design decision: replacing `mtime` as
the recency term in `archive_stale.py`. Companion measurement:
`docs/research/2026-08-28-selective-forgetting-measured.md` (`MEM-12`), which
found the defect. Policy unchanged — the 90/180/365-day type windows and the
access reprieve are decided behaviour (`MEM-06`, `knowledge/log.md`
2026-08-25). Only the signal changes.

---

## 1. The defect, restated as a question

`archive_stale._is_stale` read `md.stat().st_mtime`. `mtime` answers *when was
this file last written*. The archiver needs *when did this page last change*.
On a vault that is also a git checkout and also its own runtime, those are not
the same number, and the difference runs one way only: every content-preserving
touch makes a page look younger, so the forgetting clock restarts and nothing
is ever old.

Measured on the live vault, working tree clean (`git diff --name-only HEAD --
knowledge/notes` empty):

| | pages |
|---|---|
| notes scanned | 107 |
| tracked, byte-identical to HEAD | 76 |
| of those, mtime more than a day newer than the last content change | **53** |
| largest gap | **38.0 days** |
| archivable today, by mtime | **0** |

The oldest page in the vault looked 11.2 days old. Its content had not changed
in 49.3 days. The shortest type window is 60 days, so on mtime the first page
in this vault could not have been archived for another **48.8 days** — the
age-based forgetting path had never fired, and could not.

Who resets it: `git checkout` and branch switches (tracked files only), index
rebuilds, the nightly backlink writer (`NEW-84`, 2026-08-24), any restore or
copy of the tree. None of them change a byte.

## 2. What the field does about it

This is a known and well-documented failure mode, not a local discovery.

* **`actions/checkout` #468** — after checkout "all files have their
  modification times set to the action's execution time", not their commit
  times. Any CI-built or freshly-cloned tree has uniform, meaningless mtimes.
* **ArcadeAI/safeword #282** is the same defect with the same shape as ours, in
  a dependency-readiness guard: "A rebase (or `git checkout`, or fresh clone)
  rewrites those input files' mtimes to *now* **without changing their
  content** … Result: the guard reports **stale forever**." Its diagnosis is
  the sentence worth keeping: *"mtime is fragile to any content-preserving
  operation."* Its fix is to decide on a content fingerprint and fall back to
  mtime only when no fingerprint exists.
* **Documentation tooling reached the same answer earlier.** MkDocs'
  `git-revision-date-localized` plugin exists precisely because "file
  modification times aren't reliable in CI/CD environments, because the
  checkout process doesn't preserve original file timestamps", and reads
  `git log` for the last commit that touched the page instead. Its known
  failure mode is instructive too — a shallow clone has no history, so the
  plugin ships `fallback_to_build_date` rather than refusing to build.
* **Cargo** (`fingerprint::find_stale_file`) and Redmine #44240 record the
  mirror-image trap: mtime *older* than expected after a content-preserving
  copy, so a real change goes undetected. mtime is unreliable in both
  directions; git history and content hashes are not.

Two things transfer directly. First, the answer is *git's record of when these
bytes were written*, not the file clock. Second, every serious implementation
carries an explicit fallback for the case where history cannot answer, and says
so out loud rather than failing closed.

## 3. The rule adopted

`committed_content_times(root)` maps a vault-relative note path to the commit
time of the bytes now on disk, for pages git can vouch for: **tracked and
unmodified**. If the file's content equals HEAD's, the last commit that touched
that path is the commit that wrote what is there now, so its commit time is the
content-change time exactly — this is the safeword fix's "fingerprint first",
with `git diff` doing the comparison instead of a stored hash.

Everything else keeps the file clock: untracked (this vault's private pages), locally
modified, no repository, git absent, git slow (20 s bound), git failing for any
reason. The map is simply empty and the archiver behaves exactly as it did
before.

Where both are known, **the older of the two wins**. Both are upper bounds on
when the content last changed — a write sets mtime, a commit records bytes that
already existed — so the older one is the honest answer, and it has a property
worth stating: this signal can only ever make a page *more* archivable than
mtime did, never less. Measured over all 107 live pages: 53 pages became older,
the largest gain 38.0 days, **0 pages became younger**. No page that the old
signal would have archived is now spared.

### What this changes on the live vault, and what it does not

| | mtime | content change |
|---|---|---|
| pages aged by git / by file clock | 0 / 107 | **76 / 31** |
| oldest archivable-type page | 11.2 d | **49.3 d** |
| first page to reach its own window | in 48.8 d | **in 12.1 d** |
| archivable today | 0 | **0** |

(Snapshot of 2026-08-28; this vault is live and other agents commit to it, so
the page counts drift between runs — a re-measurement two hours later read
108 pages, 75 by git, and the same 53 / 0 / 49.3 d / 12.1 d / 0.)

**Still zero, and that is the correct answer.** Nothing in this vault is
genuinely past its window: the oldest archivable-type page is 49.3 days old
against a 60-day window. The machinery is now correct and merely untested by
time — the difference is that the horizon is 12 days away instead of 49, and
that the clock now runs instead of being reset every checkout.

## 4. Alternatives measured and rejected

* **The page's own declared `timestamp`/`date`.** Free, no subprocess, not
  resettable. Rejected on measurement: only 36 of 107 pages parse one, and it
  is a *creation* date that no writer updates, so a page rewritten yesterday
  would archive on the strength of the day it was born. The `MEM-12` note
  measured the same cohort: oldest 56 days, 0 over window — it gives the same
  zero with a worse rule.
* **A content-digest ledger under `cache/`.** Would need no git. Rejected
  twice over: `cache/` is disposable by contract, so losing it resets the clock
  — the same defect class we are fixing — and the first run after any loss
  records every page as *just seen*, making the path untestable by time again.
* **`last_accessed` from `access_tracking`.** Already in the policy as the
  reprieve, and it answers a different question: whether a page is still
  consulted, not when it last changed. Measured working at 8/8 in the `MEM-12`
  stand; nothing to change.
* **The daily-log entry that last cited a page.** The honest signal for
  *usage*, but it needs an evidence walk over every daily log per page, and it
  duplicates what the access counters already provide more cheaply.

## 5. Costs and limits, stated

* **Four git subprocesses per run**, all read-only (`rev-parse
  --show-prefix`, `ls-files`, `diff --name-only`, `log --name-only`), bounded
  by one pathspec (`knowledge/notes`) and a 20-second timeout, and run under
  `--no-optional-locks` so a read never takes `index.lock` against the same
  checkout the nightly self-update (2026-08-23) operates on. Measured on this
  vault: **0.14 s** for the history pass and **0.16 s** for a whole dry run,
  against the 120 s the weekly pass allows the archiver.
* **`git` names paths from the repository root; the vault names them from its
  own.** Measured: from a vault nested one directory inside a repository,
  `ls-files` prints `knowledge/notes/n.md` while `log` and `diff` print
  `vault/knowledge/notes/n.md`, so the two halves never meet and every page
  silently falls back to its file clock. `ls-files --full-name` plus the
  `rev-parse --show-prefix` offset makes the three agree. On this installation
  the prefix is empty, so this defends an installation shape that is allowed
  rather than one that exists here.
* **The write boundary is untouched.** `_committed_archive` and
  `_committed_restore` are the approved knowledge-writer entrypoints, and they
  call no git; the history read happens in the planning phase, before any
  mutation is proposed. `test_task14_actual_entrypoint_delegates_without_git`
  still holds for both.
* **31 of 107 pages still age by mtime**, because they are private and
  untracked — git cannot answer for them. They are exposed to the same defect
  in principle. In practice the resetting writers are checkouts, which touch
  tracked files only; a genuine rewrite of a private page is a genuine content
  change. This is a real limit, and it is the reason the archiver now prints
  how many pages each signal dated.
* **A page reverted by hand to its committed bytes** dates from its mtime, not
  from the older commit — the conservative direction.
* **Shallow clones** have no history to read, so pages fall back to the file
  clock; the same trap MkDocs documents. Nothing detects this today beyond the
  count the archiver prints.
* **The same defect is still live in three other readers**, none of which this
  pass may touch: `page_facts._age_days` (the answer path's "past its own
  window" warning — the *second* reader of `TYPE_AGE_DAYS`, so the two readers
  now disagree), `build_advisory` and `build_context` (mtime cutoffs deciding
  what is recent enough to inject). Recorded, not fixed.

## Sources

- [actions/checkout #468 — checkout breaks file commit time/mtime](https://github.com/actions/checkout/issues/468)
- [ArcadeAI/safeword #282 — dependency-readiness uses mtime, false-positives "stale" after rebase/checkout](https://github.com/ArcadeAI/safeword/issues/282)
- [mkdocs-git-revision-date-localized-plugin](https://timvink.github.io/mkdocs-git-revision-date-localized-plugin/) and [its repository](https://github.com/timvink/mkdocs-git-revision-date-localized-plugin)
- [Redmine #44240 — asset detection fails when copied files keep older mtimes](https://www.redmine.org/issues/44240)
- [Cargo `fingerprint::find_stale_file`](https://doc.rust-lang.org/stable/nightly-rustc/cargo/core/compiler/fingerprint/fn.find_stale_file.html)
- [git-log(1) — `%ct`, `--name-only`](https://git-scm.com/docs/git-log)
- `docs/research/2026-08-28-selective-forgetting-measured.md`, `knowledge/log.md` 2026-08-25 (`MEM-06`), 2026-08-24 (`NEW-84`)
