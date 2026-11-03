# CI timing evidence compiler

Owns the existing scripts/ci_timing_report.py, its closed JSON report schema and
complete tests/test_ci_timing_report.py. Production and schema bytes remain unchanged.
It reads supplied run JSON and JUnit artifacts; the CLI writes the requested report.
It does not run GitHub jobs, change timeouts or operate the memory runtime.

The existing acceptance rules require five successful attempts of one workflow run
at the supplied commit, known timing classes, unique attempt/job/artifact/test
identities, finite nonnegative durations, required full-job JUnit and twenty percent
headroom for the nearest-rank p95. These are retained historical report requirements,
not new limits or proof that every current CI execution qualifies. XML parsing keeps
DTD, entity and external-reference refusal through the installed defusedxml parser.
No historical success or latency is asserted by assigning this owner.

The original test functions, fixtures and decorators remain unchanged. Only their
validator import moves from the compatibility exporter to llm_wiki.formats.schema;
identity was verified before the change. Both full dependency implementation and
[formats owner guide](formats.md) remain in context. Shared workflow, CONTRIBUTING
and the complete September 18 retention note are context, not this module's property.

Run the original test file and tests/modules/ci_timing_report with normal pytest
bootstrap. Static imports and a fresh-process import guard prevent dependencies on
executors; real XML, file and CLI assertions retain their behavior. Full serialized
context must fit 100000 o200k_base tokens and reconstruct exact paths/bytes. Python 3.14
is the approved runtime; OS qualification remains separately reported.

## Decision evidence — 2026-11-03

The current built-in graph confirms compiler/test callers; source and workflow
search supplement its partial graph. Baseline 21 cases pass. Keeping this cohesive
compiler in place avoids path churn; merging workflow execution into it would add
unrelated responsibility, while smaller arbitrary fragments have no demonstrated
benefit. No new dependency or public behavior is introduced.

Fresh primary sources: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html)
for explicit responsibility, [import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/)
for enforceable dependency direction, [Python 3.14 XML security](https://docs.python.org/3.14/library/xml.html)
and [defusedxml](https://github.com/tiran/defusedxml) for retaining explicit XML
refusal rather than replacing it with a generic parser. Python documentation now
names 3.14.10; qualification uses the existing locked 3.14.7 environment, with no
runtime upgrade in this change.


## Preserved structure reference — 2026-11-03

The existing [CI timing report owner](ci-timing-report.md) compiles retained
workflow/JUnit measurements and owns its closed report schema. Its CLI, XML refusal
policy and numerical acceptance rules are unchanged. Workflow execution and job
configuration remain separate; complete workflow and contributor instructions are
included as context. Own tests import the existing canonical schema validator.
