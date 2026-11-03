# Vault editorial policy

This owner contains the unchanged scripts/vault_editorial.py and scripts/vault_log.py.
They define editorial page names, backlink exemptions, literal-link exclusions,
the project-template directory and the private log name. Consumers keep their
existing imports. No source file, runtime path or naming policy moves.

Editorial membership is case-sensitive and based on basenames. The three frozen
sets have different purposes and are not interchangeable. Template paths use the
existing Path.resolve behavior, which can observe filesystem links and errors;
this helper is not a pure lexical operation or a containment boundary. The module
does not discover pages, modify them, publish knowledge, rotate logs or choose a
runtime root. Those responsibilities remain with consumers.

The private log remains knowledge/log.local.md. The tracked knowledge/log.md is
only the shipped template. Git exclusion and template checks belong here; the
actual compile/query/session-start writer-and-reader integration stays in its
original test file. Migrator exemptions stay with the actual migrator tests.
The complete gitignore, shipped template and September 14 research record are
included as packet context. Existing public constants remain supported.

## Qualification — 2026-11-03

The approved whole-project plan permits internal ownership boundaries. Keeping
these small existing modules in place avoids unnecessary compatibility wrappers.
Duplicating policy in consumers would risk drift; including every runtime consumer
would defeat independent qualification. No dependency or behavior change is needed.

Research checked [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[dependency contracts](https://import-linter.readthedocs.io/en/stable/contract_types/)
and [Python 3.14 paths](https://docs.python.org/3.14/library/pathlib.html).
The last source distinguishes concrete filesystem paths from pure paths; this
owner preserves that distinction instead of claiming IO-free resolution.

Qualification preserves production bytes and original test function ASTs. Isolated
imports, frozen-name semantics, real template resolution, Git ignore behavior and
retained consumer tests check the boundary. Complete packet measurement and exact
reconstruction enforce the user's 100000-token limit. No full-cycle LLM savings,
native-platform completion or live installation follows from these checks alone.


## Preserved structure reference — 2026-11-03

Existing editorial-name and private-log constants retain their source paths;
publication, migration and actual log writers remain consumer responsibilities.
