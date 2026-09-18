# Nothing personal reaches the public repository

Dated 2026-09-18. Findings I-D1, I-D2 and I-D3 of the third audit (privacy; the owner's rule
is that nothing personal lives in a public repository). The research before the fix.

## What was found

- `git grep` over the tracked tree: the name of another of the owner's projects, and the
  name of one of its worktrees, appear in `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, five
  dated research notes, four source comments (`scripts/doctor.py`,
  `scripts/integration_adapter.py`, `scripts/markdown_transaction.py`, and the docstring of
  `tests/test_writer_gate_reclaims_a_dead_projection.py`) and three test files that use them
  as sample values (`tests/conftest.py`, `tests/test_bitemporal_claims.py`,
  `tests/test_a_less_reliable_claim_does_not_close_a_better_one.py`).
- Absolute home paths (`/home/<user>/…`) appear in 31 tracked files: the same documents, the
  three personal scripts under `docs/enforcement/` (in docstrings only), one comment in
  `scripts/answer_budget.py`, and several tests that use real machine paths as fixture data.
  The benchmark result files no longer carry any — they were normalised to `<vault>` in the
  first round.
- The guard that exists, `tests/test_nothing_private_reaches_the_public_repository.py`, reads
  only `git ls-files knowledge/`. Everything above is outside its reach, which is why it was
  green the whole time.
- Widening the guard found a name the audit had not: a **third** project of the owner's,
  named in twelve tracked files (the changelog, three dated documents, two research notes,
  three source comments and three tests), because the audit grepped for the two names it
  already knew and this guard read only `knowledge/`. It is scrubbed here as
  `another-project`, together with one generated project key in the developer document.
- Running the widened guard with the live vault's real project list (73 names, read on this
  machine, never written into a test) reports no offending tracked file after the change.
  Before it, the same run named those twelve files.
- `docs/enforcement/*` is the owner's personal tooling for machine-local gates under
  `/etc/claude-code`, referenced by nothing in the product but pinned by three tests. It is
  not deleted here; it is listed for the owner.

## Practice on this date

- Removing this content now matters more than it looks, and also less: GitHub's own guidance
  on removing sensitive data says that after a history rewrite "the commits with sensitive
  data may still be accessible elsewhere: In any clones or forks of your repository, Directly
  via their SHA-1 hashes in cached views on GitHub, Through any pull requests that reference
  them"
  (<https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository>,
  fetched today). So the value of this change is in what the repository says from here on;
  history is not rewritten, and no such claim is made.

- A deny-list of the actual private names would itself publish them. The mechanism already
  in the guard is the right one and needs no such list: the names of the owner's other
  projects are the directories under `knowledge/projects/`, which are gitignored, so on the
  owner's machine the guard derives them and on a clean checkout it says it cannot and skips.
  The fix is to widen what that guard reads, not to write a name into a test.
- Two tracked files must be allowed to contain the pattern, because holding it is their
  purpose: the guard's own examples (`/home/someone/`) and the DLP fixture in
  `tests/test_structure.py` (`/home/alice/private.md`). They are named in the guard with that
  reason; everything else tracked is swept.

## The decision

- Documents: every absolute machine path becomes a placeholder — `<vault>`, `<home>`,
  `<other-repo>`, `<other-repo-worktree>`, `<other-repo-parent>` — and the two private
  project names become `other-project` and `other-worktree`. Each document keeps its
  measurements and its meaning; only the machine layout and the identities go.
- Source comments name "one project" instead of the project.
- Test fixtures use neutral sample values (`project-alpha`, `project-beta`) and a synthetic
  root (`/repo/…`) instead of real paths, so the evidence they encode is unchanged.
- The guard sweeps every tracked text file, not only `knowledge/`, for both a home path and
  an address; the other-project check runs over the same widened set. Two things it must not
  call a leak, both decided from the repository rather than from a list of names: an address
  on a domain RFC 2606 reserves for documentation (`.example`, `.invalid`, `.test`,
  `example.com`), and a project directory a run of this product created itself — a dated run
  label, a generated key, or a name the repository already uses in its own paths
  (`benchmark`, `refusalbench`).

Files: `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, 25 files under `docs/`,
`scripts/doctor.py`, `scripts/integration_adapter.py`, `scripts/markdown_transaction.py`,
`scripts/answer_budget.py`, `tests/conftest.py`, `tests/test_bitemporal_claims.py`,
`tests/test_a_less_reliable_claim_does_not_close_a_better_one.py`,
`tests/test_writer_gate_reclaims_a_dead_projection.py`, `tests/test_answer_budget.py`,
`tests/test_dead_code_answer_default_budget.py`, `tests/test_plugin_helpers.py`,
`tests/test_repository_index.py`, `tests/test_semantic_leg_reaches_the_answer.py`,
`tests/test_a_memory_call_leaves_no_session.py`,
`tests/test_nothing_private_reaches_the_public_repository.py`,
`docs/research/2026-09-18-nothing-personal-reaches-the-public-repository.md`.
