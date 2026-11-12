# BEAM local Parquet qualification

2026-11-03. Explicitly approved by the owner in this session: implement and
verify this plan. Qualification is still pending. No live installation is proposed.

## Reproduced gap

At7673d57b, benchmark/beam_data.py::load_rows imports pyarrow.parquet only
when the input exists. PyArrow is absent from pyproject.toml, uv.lock and the
qualified Python3.14 base+dev environment. Passing an existing temporary file
raises ModuleNotFoundError(name='pyarrow') before decoding; this probe is not a
claim that its deliberately invalid bytes form Parquet. Evidence:
logs/beam-missing-dependency.json. The seven original BEAM tests pass but do not
exercise real Parquet loading. Similar stdlib JSON adapters need no dependency.

## Proposed bounded change

Declare a separate optional benchmark-data extra with pyarrow==26.0.1 and update
the universal lock through uv. Keep it out of the base install. Add real temporary
Parquet round-trip and invalid-file tests, exercising the existing loader and
converter without model calls, downloading a corpus or pretending synthetic
fixtures are benchmark results. Preserve the original seven assertions, paths,
formats and runtime behavior; do not substitute another Parquet implementation.

Run these tests in a separately provisioned Python3.14 environment and in an
explicit Linux/Windows/macOS CI matrix with this extra installed. The dedicated
suite must fail when the dependency is absent, not silently skip. Place the dedicated suite at tests/modules/beam_data/parquet_qualification.py,
explicitly invoked by that matrix and the optional verification command. It
inherits the normal tests/conftest.py guards. Ordinary test_*.py discovery keeps
the base-only contract; the explicit optional suite has no importorskip and
cannot pass without PyArrow. Include both suites in its full maintenance packet. Existing full-source context, import boundary, CCN and exact packet
checks remain mandatory. Add clear optional installation instructions, synchronized
in all README languages if those files change. No version bump or release.

## Research and alternatives

Checked2026-11-03: Apache Arrow26.0.1 documents Python3.11–3.14 support.
Official PyPI JSON lists cp314 wheels for Linux x86_64/aarch64, Windows amd64,
and macOS arm64/x86_64. Wheel availability is not executed platform qualification;
macOS wheels name minimum macOS12.0, manylinux wheels glibc2.28. Follow existing
project platform contracts; do not infer broader support from a filename.

Alternatives: adding PyArrow to base imposes a substantial unrelated binary
library on every memory installation; swapping to pandas/another engine changes
the reader surface and still adds a dependency; leaving it undeclared keeps the
reproduced failure. An optional extra expresses the existing benchmark-only need.
No security qualification is claimed yet: lock resolution and the existing exact
version advisory audits must pass before acceptance.

Sources: [Arrow installation](https://arrow.apache.org/docs/python/install.html),
[PyPI release metadata](https://pypi.org/pypi/pyarrow/26.0.1/json),
[PyPA optional dependencies](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#dependencies-optional-dependencies),
[uv extras and resolution](https://docs.astral.sh/uv/concepts/projects/dependencies/#optional-dependencies).
Apache, PyPA and Astral are independent primary sources. They support the proposed
dependency mechanism, not a fresh benchmark effectiveness result.
