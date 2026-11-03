# Static knowledge-writer analysis

`scripts/check_knowledge_writers.py` stays byte-identical at its public path.
It reports statically recognized writes to covered knowledge paths. It does not
execute inspected source, import writers, authorize runtime mutations or replace
the recoverable transaction boundary.

## Inputs, outputs and scope

`scan_source(path, source)` returns existing frozen WriterFinding values: path,
line, API, approval flag and enclosing function. Python uses AST-based path and
binding propagation; JavaScript, PowerShell and shell use the existing statement
and expression scanners. Unsupported suffixes yield no findings. Syntax errors
from Python parsing propagate.

Repository discovery reads supported executables under scripts and integrations,
plus root installers/setup files. It decodes UTF-8 strictly but skips unreadable
or undecodable files, as before. Entrypoint discovery uses its supplied filename
set and preserves its separate error behavior. The CLI accepts an optional root,
prints findings and returns 1 if any reported finding is unapproved, otherwise 0.
It does not write files. The existing covered-path expression, canonical mutation
bindings and scoped transaction/archive exceptions remain unchanged.

This is a bounded static guard, not a complete proof of absence of unsafe writers:
the existing Python analysis makes at most four summary passes and two loop
passes, models selected AST forms and cannot resolve arbitrary dynamic behavior.
The other language parsers are not full language interpreters. Discovery can skip
unreadable files. These inherited limitations are documented, not introduced or
relaxed by decomposition. Actual writer behavior, CAS/recovery and the repository
entrypoint matrix retain their own tests and remain necessary.

## Qualification — 2026-11-03

The approved autonomous plan permits an existing cohesive owner to remain at its
current path. A new analysis framework would change dependencies and detection
behavior; folding mutation implementations into the analyzer would destroy this
boundary. Neither is needed to qualify its maintenance packet.

Sources checked: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[import contracts](https://import-linter.readthedocs.io/en/stable/contract_types/),
[structural protocols](https://peps.python.org/pep-0544/),
[Python 3.14 AST](https://docs.python.org/3.14/library/ast.html) and
[CodeQL data-flow distinctions and limits](https://codeql.github.com/docs/writing-codeql-queries/about-data-flow-analysis/).
These inform boundary and limitation reasoning; CodeQL and Import Linter are not
installed and no new completeness guarantee is inferred.

Nineteen source-level test definitions move with identical ASTs, decorators and
embedded fixtures. The original whole-repository discovery, writer matrix and
execution/recovery tests remain unchanged. Temporary-tree discovery and actual
CLI tests verify reported paths, exit behavior and that inspected code does not
run. Static/runtime import guards keep transaction implementations outside this
owner. Complete source, tests, instructions and historical context must fit
100000 tokens and reconstruct byte-for-byte. The full September 18 orphan-tools
record remains shared historical context, not a claim that its unrelated tools
are owned here. No paired LLM cost improvement or live installation is claimed.


## Preserved structure reference — 2026-11-03

The scanner reports source findings; transaction enforcement and the actual writer
behavioral matrix remain separate responsibilities.
