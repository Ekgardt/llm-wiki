# BEAM data conversion

Owns the existing `benchmark/beam_data.py`, its seven original tests in
`tests/test_beam_becomes_a_question_this_stand_can_read.py`, and the qualification
tests under `tests/modules/beam_data`. Source and original assertions are retained.
The converter reads a supplied Parquet file and emits the existing question JSON
format. Missing input raises DatasetUnavailable; malformed Parquet is refused
by the real PyArrow reader. It never invents a replacement dataset. Generated
fixtures qualify IO and normalization only, not benchmark quality.

The original tests also call the existing LongMemEval shape/abstention helpers;
their full implementation and owner guide remain in this packet. The complete
September 6 BEAM history retains the earlier benchmark scope and limitations.

## Optional dependency and verification

PyArrow 26.0.1 is pinned in the optional `benchmark-data` extra. It is not part
of the base or default installation. For this benchmark's Parquet operations:

```sh
uv sync --locked --extra benchmark-data
uv run --locked --no-sync pytest tests/test_beam_becomes_a_question_this_stand_can_read.py tests/modules/beam_data -q
uv run --locked --no-sync pytest tests/modules/beam_data/parquet_qualification.py -q
```

The explicit `parquet_qualification.py` selection intentionally requires PyArrow
and never skips when it is absent. Ordinary `test_*.py` discovery keeps the base
suite dependency-free. The separate CI matrix installs the extra on Linux,
Windows and macOS and selects the real Parquet suite. Its result is required by
the aggregate CI gate. No base/production environment is modified for these tests.
Run the optional file separately: selecting its parent directory in the same
pytest command can remove the nonstandard filename during collection deduplication.

The optional suite writes real temporary Parquet, checks UTF-8/nested rows and
converted output, and checks empty and invalid files. Existing seven assertions
remain unchanged. Import isolation allows the declared third-party PyArrow
dependency while refusing runtime/provider dependencies. The complete serialized
packet must reconstruct every byte within 100000 o200k_base tokens.

## Approved decision — 2026-11-03

The owner explicitly approved the [dependency proposal](../research/2026-11-03-beam-parquet-dependency-proposal.md).
Adding PyArrow to the base would impose an unrelated binary package on every
memory installation. Replacing the existing reader changes behavior and still
adds a dependency. A separate extra expresses the current benchmark-only need.
The package and actual OS runs require qualification; available wheels alone
are not a pass. The approved Python 3.14 runtime remains unchanged.

Primary sources rechecked today: [Apache Arrow installation](https://arrow.apache.org/docs/python/install.html),
[release metadata](https://pypi.org/pypi/pyarrow/26.0.1/json),
[PyPA optional dependencies](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#dependencies-optional-dependencies),
and [uv extras](https://docs.astral.sh/uv/concepts/projects/dependencies/#optional-dependencies).
The choice of this boundary follows the existing reader and explicit approval.

## Local evidence — 2026-11-03

The base environment passes 10 original/boundary/context checks. The optional
file fails collection without PyArrow, as required, and passes all four real
Parquet tests in the separate locked Python 3.14.7 environment. The initial
combined directory/file command silently omitted those four cases; inspecting
JUnit caught it. CI and the documented commands now select that file separately,
with a regression guard on its invocation. CI/environment checks pass 33 tests;
registry/context/structure checks pass 711. Original source and seven test
functions remain byte-identical. CCN/shape and Ruff pass.

Both PyPI and OSV audits report no known vulnerabilities; the universal OSV
coverage check accounts for all 122 locked versions. PyPI cannot identify the
vendor torch +cpu build, which the mandatory OSV pass covers. Native Windows
and macOS Parquet runs are pending; local success is not their qualification.
