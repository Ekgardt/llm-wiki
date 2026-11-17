# Interruption propagation

`scripts/interruption.py` owns traversal of Python exception cause/context links,
interruption discovery and priority when collected cleanup errors are raised.
It has only standard-library dependencies and owns no files, threads, processes
or cancellation tokens. The existing public path and implementation are unchanged.

KeyboardInterrupt/SystemExit retain identity and original traceback. Ordinary
errors retain the existing first-error policy. Cause/context cycles are visited
once by identity. The shared raise helper detaches immediate secondary links
that lead back to the interruption. It does not recursively rewrite deeper
secondary evidence. The protocol's deeper scrub policy is separate and remains
in lsp_protocol; similar names are not evidence that the policies are equivalent.

Consumers are lsp_process, pyright_session and lsp_protocol. Their actual lifecycle
integration suites remain additional obligations; this packet independently tests
exception identity, ordering, cycles, traceback and cleanup-context behavior.
Run `pytest tests/modules/interruption -q`. Full instructions, source, tests,
bootstrap, measurement codec and research are in the context manifest. No move,
alias, dependency, new limit or compatibility-floor change is introduced.

## Structural reference retained from the overview

### Interruption propagation (qualified in place)

The existing `scripts/interruption.py` remains the shared stdlib-only owner of
exception-chain traversal and collected-error priority. Its immediate-link
cleanup remains distinct from the protocol's deeper chain scrub. No runtime
behavior or path change is planned. See [research](../research/2026-11-03-interruption-packet.md).
