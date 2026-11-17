# An update names the Python it needs

Date: 2026-11-09. Status: implemented.

## The defect

The nightly fast-forwards the checkout and syncs dependencies afterwards. Once the
project requires Python 3.14, a vault without that interpreter would take the commit,
fail the sync, and stay on code its old environment cannot import. Every later nightly
would then die at import, before the code that retries the sync.

## Sources checked 2026-11-09

- [uv: Python versions](https://docs.astral.sh/uv/concepts/python-versions/): how uv
  discovers interpreters and when it may download one.
- [uv `python find`](https://docs.rs/crate/uv/0.11.21/source/src/commands/python/find.rs):
  the command fails when no installed interpreter meets the request. Measured here:
  `uv python find '>=3.14,<3.15'` exits 0 and `'>=3.99'` exits 2.
- [PEP 649](https://peps.python.org/pep-649) and PEP 749: deferred annotations are why
  this code cannot run on an older Python, so the gate must parse and import without them.
- [Packaging guide: dropping older Python versions](https://github.com/pypa/packaging.python.org/blob/44313e5db4d729eede0bcb91c08d6ec93e89c5c8/source/guides/dropping-older-python-versions.rst):
  `requires-python` is the published contract for which interpreters a release runs on.

- [uv: Python versions](https://docs.astral.sh/uv/concepts/python-versions/) again, for the
  recovery: `uv python install` fetches a managed build into uv's own directory and a
  later `uv python find` discovers it, without touching the system Python.
- [python-build-standalone: running](https://docs.astral.sh/python-build-standalone/running/):
  the managed builds are self-contained, so a machine with no Python 3.14 can run one.
- [Upgrading a self-hosted application](https://massivegrid.com/blog/self-hosted-app-upgrade-strategy/)
  and [OneUptime's upgrade notes](https://oneuptime.com/blog/post/2026-09-06-upgrade-self-hosted-oneuptime/view):
  the general rule of checking an upgrade's prerequisites before applying it.

## Alternatives

- Refuse the commit before the merge (chosen for the update itself). The checkout stays
  on working code and doctor says what to install. No moving parts beyond one `git show`
  and one `uv` call.
- Download the interpreter before the merge, with rollback on failure (rejected). An
  unattended download with a rollback path adds more states than the defect justifies,
  and refusing already keeps the vault working.
- Download the interpreter once the checkout is already past the environment (chosen for
  the gate). This is a different position from the rejected one: the merge has happened,
  nothing can run until the interpreter exists, there is nothing to roll back, and
  nobody is present at 03:00. The installer still does not download, because a person
  is there to be told.
- Fix only the sync order (rejected). A vault already past the commit still could not
  start, because import fails first.

## What ships

`self_update._python_requirement_block` reads `requires-python` from the fetched commit
and asks `uv python find`. An unmet requirement ends as `skipped` /
`python_requirement_unmet` with `required`, and `doctor` prints the exact
`uv python install` command from that record.

`python_series.require_supported_python` is the first thing the nightly does. It imports
only the standard library of the oldest Python a vault may still have, and no project
module: everything that needs the new Python runs on the new Python. On an older
interpreter it finds Python 3.14, asking uv to fetch it when the machine has none; has
that interpreter run `self_update.py --repair-environment`, passing the old environment's
site directories so the extras and groups the operator chose survive; then reruns the
nightly on the repaired environment. Each way this can stop has its own message. A
refusal from uv reports only its exit code, because this module has no redactor to run
uv's output through; the repair child redacts its own. The rerun is bounded by the
nightly's own `worst_case_seconds()`, read on the repaired Python, plus one probe
interval for uv's start.

## Trade-offs

The first update onto this commit is carried out by the previous release, which has no
such check. From that merge until the first nightly recovers, up to a day, hooks fail
because the environment cannot import the code; rerunning the installer closes that
window immediately. The recovery downloads a Python build at night on such a vault, which
is the only way it can come back without a person. Replacing a Windows virtual environment
while its Python is running has not been verified on a real Windows host.
