# A green main tags its version

Date: 2026-09-24. Audit item B-9 of `docs/AUDIT-2026-09-24-live.md`.

## Question

The version stayed at 4.0.0 for a month of changes, and the READMEs already tell
a reader to verify and install `v4.1.0`, a tag that does not exist. Which version is
next, and how does the tag stop depending on someone remembering it?

## Sources

- Keep a Changelog 1.1.0 (fetched 2026-09-24, https://keepachangelog.com/en/1.1.0/):
  "The same types of changes should be grouped"; six types (Added, Changed,
  Deprecated, Removed, Fixed, Security); at release time the Unreleased changes move
  into a new version section.
- Semantic Versioning 2.0.0 (fetched 2026-09-24, https://semver.org/): new
  backward-compatible functionality and deprecations ship in a minor release;
  "Major version X MUST be incremented if any backwards incompatible changes are
  introduced to the public API."
- GitHub Actions, "Events that trigger workflows" — `workflow_run` (fetched
  2026-09-24,
  https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#workflow_run):
  it runs when another workflow completes, only if the workflow file is on the
  default branch; `branches` filters the triggering run's branch; the triggering
  run's result is `github.event.workflow_run.conclusion`.

## Findings (facts)

1. `## [Unreleased]` holds every change since 2026-08-25 under fourteen type
   headings: Changed three times, Added three, Fixed three, Removed two.
2. `pyproject.toml` and `uv.lock` say 4.0.0; the three README badges say 4.0.0;
   their "Verifying a release" sections name `v4.1.0`.
3. `v4.0.0` is an annotated tag the owner created by hand; nothing in CI tags.
4. The release checklist (`CONTRIBUTING.md`) ends with "Only then: tag, push".
   After a merge nobody is at the keyboard, so the tag was the step that did not
   happen.

## Decision (conclusion)

- The next version is 5.0.0, not the 4.1.0 the READMEs named. The Unreleased
  section removes supported surface: Cursor and Antigravity are no longer
  supported platforms, an upgrade from v3.3.0–v3.4.0 no longer imports the JSON
  queue, and the legacy index files are no longer read. Each is backward
  incompatible for someone, and SemVer makes that a major release.
- The Unreleased section becomes `## [5.0.0] — 2026-09-24` with each type once, in
  the order Added, Changed, Deprecated, Removed, Fixed; the entries keep their
  order within a type.
- `pyproject.toml`, `uv.lock`, the three README badges and their release
  verification commands say 5.0.0.
- A new workflow, `release-tag`, runs on `workflow_run` of `tests` on `main` when
  it succeeded for a push, checks out that exact commit, and runs
  `scripts/release_tag.py`: when the commit's `pyproject.toml` version has a
  `## [X.Y.Z]` section in `CHANGELOG.md` and no `vX.Y.Z` tag exists, it creates the
  annotated tag on that commit, with the section and the release manifest as its
  message, and pushes only that tag. An existing tag is never moved. Only this job
  has `contents: write`.

## Edited files

- `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `README.md`, `README.ru.md`,
  `README.zh-CN.md`, `CONTRIBUTING.md`
- `scripts/release_tag.py`, `.github/workflows/release-tag.yml`
- `tests/test_a_green_main_tags_its_version.py`
- `docs/AUDIT-2026-09-24-live.md`

## Uncertainty

The workflow file takes effect only once it is on `main`, so the first run it can
make is the one after the merge that brings it; the job's first live run is its
real test.
