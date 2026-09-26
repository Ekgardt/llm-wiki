# An extra is chosen by what nothing else pulled in

Date: 2026-09-26. Audit 2026-09-26, regression list item 9.

## What was wrong

Nothing records which extras the operator installed, so `self_update.chosen_extras`
reads the environment: an extra counts as chosen when a distribution only it
names is installed. `huggingface-hub` is named only by `semantic`, but
`transformers` (the `reranker` extra) requires it. A vault with only the reranker
therefore read as a semantic choice, and the nightly update synced `semantic`
(ONNX Runtime and the encoder's stack) that the operator never chose.

## Decision

`_installed_distributions()` now returns each installed distribution with the
distributions it always requires (requirements under an `extra ==` marker are
left out, since they arrive only when that extra of the package is chosen). An
extra is chosen when one of its exclusive distributions is installed and no
installed distribution outside the extra requires it. `semantic` is then chosen
by `onnxruntime`, which nothing else pulls in; the reranker's
`huggingface-hub` no longer counts.

## Source

Python documentation, `importlib.metadata`, fetched 2026-09-26 from
https://docs.python.org/3/library/importlib.metadata.html:

- `requires()`: "Return the declared dependency specifiers for the named
  distribution package."
- Example: `requires('wheel')` →
  `["pytest (>=3.0.0) ; extra == 'test'", "pytest-cov ; extra == 'test'"]`.

So the requirements are available per installed distribution, and a requirement
that only applies to an extra carries an `extra ==` marker, which is why those
are excluded from "pulled in".

## Files

- `scripts/self_update.py`
- `tests/test_an_update_says_what_it_did_not_bring_into_force.py`
