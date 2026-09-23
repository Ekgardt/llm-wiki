# The label review comes home

Dated 2026-09-23. Files: `benchmark/review_flush_labels.py`,
`tests/test_review_flush_labels.py`, `.gitignore`,
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, `CHANGELOG.md`,
`docs/research/2026-09-23-the-label-review-comes-home.md`.

## What was found

- Four agent branches of 2026-08-25/26 survived the branch cleanup with commits
  that never reached `main`. By the five rules: two were superseded (the green-commit
  preflight by the `all-green` CI job and the `main` ruleset; the credential-evidence
  refusal by the span-hash allowlist of `export_vault.py`), one was superseded on its
  file (`repository_scope.py`, "one Git probe, one budget"), and one addressed a gap
  that is still open: `OPEN-034` in `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` waits
  for human labels; the live corpus (40 cases, private) carries `label_reviewed: false`
  on every case and `run_flush_classification.py` calls every run provisional.
- Rule 3 decides it: an accuracy figure measured against machine labels that moved
  from 20/6/14 to 5/3/32 under a different excerpt length is not a fact, and nothing
  in `main` lets the owner produce reviewed labels. The branch's tool does, and it
  hides the machine's label until the reviewer answers.
- Ported with `git cherry-pick --no-commit d5a1c745`; the `knowledge/log.md` hunk was
  dropped (the tracked log is a template since 2026-09-14) and the `.gitignore` hunk
  merged. Complexity gate and linter pass unchanged; the 67 tests of the tool, the
  benchmark and the structure pass; a dry run against the live corpus shows a case and
  records nothing on quit.

## Practice on this date

- Agreement between two raters is reported as Cohen's kappa and read on the Landis
  and Koch bands; a kappa needs a minimum sample, and the guideline derived from the
  Flack formula warns that its small answers cannot be trusted, so the tool refuses a
  kappa below 30 reviewed cases and samples 60 by default
  ([Bujang & Baharum, "Guidelines of the minimum sample size requirements for Kappa
  agreement test", Epidemiology, Biostatistics and Public Health 14(2), 2017, DOI
  10.2427/12267](https://doi.org/10.2427/12267); Landis & Koch, Biometrics 33, 1977).

## The decision

The tool joins `main` as it was written, under the audit note of 2026-08-25 that
describes it. Reviewing is the owner's act: `uv run python
benchmark/review_flush_labels.py`, verdicts beside the corpus, private by
`.gitignore`.

## Sources

- Bujang & Baharum 2017, DOI 10.2427/12267; Landis & Koch 1977, Biometrics 33:159–174.
- `git cherry main <branch>` and `git ls-tree main` over the four branches, 2026-09-23.

## Addendum, later on 2026-09-23: the corpus was the classifier's own calls

Files: `benchmark/build_flush_corpus.py`, `benchmark/review_flush_labels.py`,
`tests/test_flush_classification_benchmark.py`, `tests/test_review_flush_labels.py`,
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`.

The owner's first review showed record metadata instead of words (fixed the same
hour: the excerpt is rendered by `session_evidence.render_transcript`) and then, in 39
of the 40 cases, the classifier's own prompt: the corpus of 2026-08-23 took the 40
newest transcripts under `~/.claude/projects`, and until the `--no-session-persistence`
fix of 2026-09-14 every memory call through `claude -p` left one (entry point
`sdk-cli`). Counted 2026-09-23: 1 082 such transcripts (101 MB, newest 2026-09-17)
against 7 sessions someone held. The three verdicts recorded are one real case and two
of the classifier's own calls.

Decisions: the builder skips a transcript whose first records name entry point
`sdk-cli`; the prompt is flushed before the answer is read. `OPEN-034` stays open for
a reason the tool cannot remove: seven held sessions are fewer than the thirty a kappa
needs, so no corpus worth reviewing exists on this machine yet. The 1 082 old
transcripts are the memory's residue, not the owner's sessions, and may be deleted
(the assistant's own attempt was refused by the host as transcript tampering).
