# Deterministic breadcrumb receipt

The existing scripts/breadcrumb_decision.py owns deterministic receipt validation
and daily-journal planning from an already complete BreadcrumbBundle. Its only
local dependencies are breadcrumb_protocol, breadcrumb_documents, canonical
formats and the shared capture decision byte policy. It performs no publication,
clock lookup, provider execution or runtime-state read.

Canonical event-object bytes must match manifest and anchor digests and sizes.
The receipt binds occurrence, source head digest and daily operation plan. Small
inputs remain whole in the journal; large inputs retain exact permanent-source
links under the existing physical receipt bound. A reference does not truncate
the permanent source. Occurrence time fixes the day and heading; retries do not
reclassify or substitute current time. read_decision requires exact regenerated
receipt bytes, not merely a matching plan hash. All existing function bodies,
values, public paths, error messages and wire bytes are preserved.

BreadcrumbBundle now belongs to the existing protocol, with one frozen class and
an identical storage alias. Its legacy pickle module name remains; deserializing
old Python pickle may import storage. The independent receipt path uses complete
in-memory values only. A receipt alone grants no transaction, terminal completion,
queue release or evidence cleanup authority.

The packet includes the complete original decision tests, protocol/document
implementations, schemas, bounded IO source closure, formats and policy, all owner
guides and full required history. Tests keep real original input/receipt assertions,
large-input links, altered content/day, noncanonical JSON and repeatability.
Additional import guards refuse storage, queue, provider and publishing modules.
Actual CCN/shape, Ruff, runtime integration and exact <=100000 token reconstruction
remain acceptance conditions, not evidence of useful full-cycle LLM savings.

## Boundary research — 2026-11-04

Keep the existing deterministic receipt cohesive; separate storage from the
carrier instead of introducing a new module for one dataclass. Original field
order and fully qualified pickle identity are compatibility contracts.
[Python dataclasses](https://docs.python.org/3.14/library/dataclasses.html) and
[pickle](https://docs.python.org/3.14/library/pickle.html) were checked today;
[bounded contexts](https://martinfowler.com/bliki/BoundedContext.html) and
[pytest integration](https://docs.pytest.org/en/stable/explanation/goodpractices.html)
were checked in this session. No dependency or runtime version changes. The
approved decomposition mandate permits this internal boundary; live installation
remains separately controlled.
