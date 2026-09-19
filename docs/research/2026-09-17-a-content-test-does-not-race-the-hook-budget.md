# A content test does not race the hook budget

Dated 2026-09-17. Second CI round of the third audit's fixes: two Windows jobs failed in
`tests/test_capture_hooks.py` (a breadcrumb "not written", a replay "did not exit").

## What was found

- Finding C-F8 gave every hook writer a deadline inside its host's timeout: 3 s for prompt and
  tool breadcrumbs, 7 s for the session-end tag. Past it the writer gives up and records the
  reason, which is the designed behaviour: the host would kill the hook anyway.
- The failing tests check what the breadcrumb says, not how fast it is written. The failed
  test ran 3.16 s — the budget. In the last green run (before the deadline existed) the same
  tests took 1.4–5.6 s on the Windows runners, shard by shard: the first append into a fresh
  vault creates the runtime databases, and a loaded runner needs longer than a hook may wait.
- The project already has the rule for this: `tests/slow_machine.py` — "a bound on a hang is
  not a measurement", bounds come from one place and scale with the machine.
- pytest documents the mechanism used: "fixtures … can be requested by every test without
  being named with `autouse=True`", and markers are read with
  `request.node.get_closest_marker` (<https://docs.pytest.org/en/stable/how-to/fixtures.html>,
  <https://docs.pytest.org/en/stable/example/markers.html>; known from the documentation,
  not re-fetched today).

## The decision

- `tests/conftest.py` gives every test the slow-machine bound as the two append budgets, by
  name, so a module another test reloaded is patched too. Tests of the budgets themselves
  (`tests/test_every_hook_writer_gives_up_before_its_host.py`) carry the marker
  `shipped_append_budgets` and see the shipped values; the marker is registered in
  `pytest_configure`.
- The product is unchanged.

Files: `tests/conftest.py`, `tests/test_every_hook_writer_gives_up_before_its_host.py`,
`docs/research/2026-09-17-a-content-test-does-not-race-the-hook-budget.md`.
