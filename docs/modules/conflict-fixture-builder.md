# Conflict fixture preparation from Parquet

Owns benchmark/build_conflict_fixture.py at its existing path. The byte-identical
builder reads a local upstream Parquet file, selects the first row whose first
question identifier has the requested prefix, delegates all fact interpretation
to factconsolidation_data, binds the input bytes by SHA256, writes the existing
sorted UTF-8 JSON and prints its existing summary. Importing the builder does
not load optional PyArrow. It adds no acquisition service or product runtime.

Missing rows, malformed input and missing files fail before writing output.
The existing write itself is a direct Path.write_text, not a transactional write;
this owner does not claim crash atomicity or change that historical CLI contract.
Prefix matching, first-row selection, default row and output formatting remain
unchanged. No upstream data is downloaded during qualification, and synthetic
Parquet cases do not revalidate the historical dataset or effectiveness claims.

The complete packet includes the factconsolidation_data source and its full
guide, original research history, tests, actual bootstrap and instructions.
The shipped conflict set remains owned by factconsolidation-data. Regular tests
cover pure selection and lazy import without PyArrow. The explicitly invoked
parquet_qualification.py requires the existing locked benchmark-data extra;
missing PyArrow fails collection, never skips. Real Parquet/CLI tests are run
separately from their parent directory on Linux, Windows and macOS in the same
optional-data CI job. No dependency, installer or live-vault change is needed.

```sh
uv run --locked --extra benchmark-data pytest tests/modules/conflict_fixture_builder -q
uv run --locked --extra benchmark-data pytest tests/modules/conflict_fixture_builder/parquet_qualification.py -q
```

## Boundary research — 2026-11-03

The approved autonomous decomposition mandate permits qualifying this existing
CLI separately from parsing and resolution. Folding it into the pure parser
would mix optional file decoding with deterministic fact interpretation;
splitting its small IO sequence further would add indirection. Its only local
dependency remains explicit and fully included. The current PyArrow26.0.1 lock
already supports this optional reader; the base installation stays independent.

Sources reviewed: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[pytest fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html),
[Arrow read_table](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.read_table.html)
and [write_table](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.write_table.html).
The initial generic Arrow documentation URL returned404; the versioned API
pages were checked instead. These support the boundary and real file checks,
not new benchmark quality claims. Native results are reported only after CI.
