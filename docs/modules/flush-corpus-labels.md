# Classification corpus label semantics

Owns benchmark/flush_corpus_labels.py: the two immutable reading records,
first-JSON-object parsing, known-kind/short-marker selection, verbatim quote
grounding, case construction and agreement statistics. These are pure operations
on supplied text and records. Transcript discovery, product evidence rendering,
provider prompts/calls, schema validation and CLI output remain in
benchmark/build_flush_corpus.py. No provider or runtime module belongs here.

The move retains all existing algorithms, error messages, regexes, limits,
rounding and dictionary fields. In particular, one real sentence grounds a
stitched quote; a fabricated quote voids the reading, a nothing-kept reading
requires none, and disputed cases retain both readings. Kappa ignores void quote
readings, remains unavailable below the existing 30-case floor or when expected
agreement is one, and retains three-decimal rounding. These labels are model
agreement evidence, not independently established truth.

The former builder explicitly adds its own benchmark directory to the import
search path, alongside its existing scripts path, so direct-script and namespace
module invocation both locate the same owner from a neutral working directory.
It re-exports all moved names as direct aliases. Existing imports
continue to refer to the same objects; there is no second implementation. These
aliases remain while compatibility imports use the old path and can be removed
only after all supported consumers migrate. Definition-module metadata naturally
names the new owner. No repository pickle or metadata-sensitive consumer was
found; old class lookup remains available through the alias. Five original pure
test functions move with unchanged bodies; product and orchestration tests stay
in tests/test_flush_classification_benchmark.py, including alias integration.

Qualification covers literal AST preservation, normal import isolation, original
assertions and additional malformed/Unicode/quote/marker/degenerate-agreement
cases, actual CCN/shape and Ruff. The packet includes both complete historical
research records, bootstrap and instructions. It must reconstruct all bytes and
fit 100000 o200k_base tokens. The old documents retain their dated conclusions;
the September decision superseded August's manual review, not the historical
record itself. No live corpus, transcript, provider or installed vault is read
or modified by the isolated tests.

## Boundary research — 2026-11-03

A single pure labels module groups the records with their interpretation and
agreement measure. Keeping it in the IO builder requires importing capture and
provider infrastructure to parse a JSON answer; one module per small helper
would fragment a single responsibility. The approved broad decomposition plan
permits this internal boundary while preserving public commands and data.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[pytest integration practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html),
and [Python 3.14 dataclasses](https://docs.python.org/3.14/library/dataclasses.html).
They support explicit boundaries, isolated qualification and preserving dataclass
identity/fields. They do not validate the historical labels or introduce new
thresholds. The runtime remains the qualified locked Python 3.14 environment.
