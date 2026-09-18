# The pinning test asks only where there is something to pin

Dated 2026-09-18. A test added today refuses on macOS and Windows.

## What was found

- `tests/test_workspace_revision.py::test_the_fixture_takes_this_machines_git_configuration_out_of_the_way`
  was added so that the file's `private_calls == 1` claims rest on a checked pinning rather
  than on an assumption. It begins `assert workspace_revision._private_git_installation() is
  not None`.
- `_private_git_installation()` returns `None` whenever `git`'s resolved path is not in
  `_KNOWN_GIT_PREFIXES`: the product only takes the private-index path on a git layout whose
  system configuration and attribute locations it knows. On the macOS and Windows runners the
  layout is another one, so the function answers `None` by design, and the new test fails
  there — six jobs in CI run 35392404776.
- On such a machine there is nothing to pin, because the product never reads a system
  configuration at all: the claim the test guards is vacuous there, not violated.

## The decision

- The test keeps its whole assertion where the product offers a private installation, and
  skips where it offers none, saying so. A skip is the honest answer to "is the pinning in
  place?" on a machine that has nothing to pin; turning it into a pass would hide the day the
  product learns that layout and the fixture does not follow.

Files: `tests/test_workspace_revision.py`,
`docs/research/2026-09-18-the-pinning-test-asks-only-where-there-is-something-to-pin.md`.
