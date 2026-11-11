# `tests/test_a_patch_reaches_the_code_it_stands_in_for.py`

A test that patches a facade patches a name some code reads through it.

A split leaves a public module that re-exports names its concern modules own.
The concern modules read their own globals, so a stand-in set on the facade's
copy changes nothing unless the facade itself, an attribute read
`facade.name`, or a call-time `from facade import name` reaches it. The guard
reads `scripts/` and `tests/` as source text and imports neither.

Its tests are `tests/test_a_patch_reaches_the_code_it_stands_in_for.py`.

Providers outside the module appear in the packet either whole or as their exact
derived description; the `-context.json` manifest next to this guide lists the
complete maintenance context and the tests that qualify each description.

Run `uv run pytest tests/test_a_patch_reaches_the_code_it_stands_in_for.py -q`.
