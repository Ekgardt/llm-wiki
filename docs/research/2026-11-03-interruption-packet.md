# Existing interruption helper as an isolated packet

Date: 2026-11-03. Designed under the approved decomposition mandate.
Base: 4abf9ccd. Production changes are not planned: qualify the already independent
`scripts/interruption.py` in place, preserving its bytes and import path.

## Sources and decision

Three independent primary publishers checked today:
- Python exception documentation: implicit context, explicit cause and traceback
  are separate state; KeyboardInterrupt and SystemExit derive from BaseException.
  https://docs.python.org/3/library/exceptions.html
- pytest assertion documentation: inspect caught exception objects and traceback,
  not only a broad expected exception class.
  https://docs.pytest.org/en/stable/how-to/assert.html
- Import Linter contracts: forbidden/independent imports are executable boundary
  rules, distinct from merely documenting a layer.
  https://import-linter.readthedocs.io/en/stable/contract_types/

Use existing stdlib-only import and subprocess guards, plus behavioral tests for
identity, priority, cycles and traceback preservation. Do not install Import Linter
or adopt newer Python exception-group APIs: compatibility remains Python >=3.10.
Moving an already independent module adds aliases without removing coupling;
rewriting exception handling changes runtime semantics without a demonstrated bug.
Retain the implementation and document its precise authority instead.

## Scope and important distinction

The helper traverses both cause and context with identity-based cycle detection,
finds interruptions, and gives them priority over ordinary cleanup errors.
`raise_collected_errors` detaches the secondary error's immediate links that reach
back to the interruption. `lsp_protocol._raise_collected_errors` deliberately
scrubs deeper chains while retaining different context evidence; these are not
interchangeable implementations. This packet will not replace the latter by a
similar-looking helper. Session/process consumers keep their existing imports.

New tests use real exception objects and nested handlers to check ordinary errors,
prior errors, wrapped interruption priority, retained traceback, detachment and
cycle termination. Existing LSP lifecycle tests remain integration obligations.
The packet includes full instructions, source, tests, pytest bootstrap, manifest,
measurement codec and research. Actual serialization must round-trip every byte
and remain <=100000 o200k_base tokens. Index/callers are refreshed before tests
are written. Native-platform results are not inferred from Linux runs.

## Qualification

No direct tests of the public helper were found by symbol search; integration
consumers exercise it through LSP startup/cleanup. Record source digest before and
after, graph provenance, test results, actual complexity and full bundle weight.


Qualification completed in the isolated candidate. Fresh built-in generation
`generation-18e46d21040e7dea-26a5e9a5` corresponds to 4abf9ccd. Callers were
inspected alongside source because the graph is incomplete; evidence is in
`logs/lsp-leaves-graph.json`. Production sources, existing position tests and
Unicode fixture are byte-identical to 4abf9ccd (`lsp-leaves-unchanged-proof.json`).

Both new packets together: 95 tests passed on Python 3.12 and Python 3.10.
Source/guide/bootstrap closure, ownership, structure, privacy and weights:
224 passed, 1 skipped. Ruff and actual complexity/shape checks passed for every
new test. No bug fix or production behavior change is claimed; these tests
qualify existing contracts rather than manufacturing a pre-change failure.

All 51 actual context artifacts round-tripped every path and byte under 100000
o200k_base tokens. Before this evidence paragraph, interruption was 72176 and
positions 79455; the largest packet was 96858. The final artifact report is
recorded with the committed checkpoint. Ownership is 402/3131 tracked files,
2729 still unassigned: this is not whole-project completion. The full regression
running at 4abf9ccd predates these test/documentation-only packets.
