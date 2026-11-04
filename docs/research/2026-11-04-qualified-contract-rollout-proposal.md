# Conditional use of qualified dependency contracts

Date: 2026-11-04. Status: explicitly accepted by the owner on 2026-11-04; no canonical
source omission has been enabled. This follows the approved pilot, not a waiver
of the development laws or the 100000-token limit.

## Concrete evidence and limitation

The answer_cost → answer_budget pilot produced two independently correct repairs,
with both arms passing their own and cross-applied tests. The instrumented pair
reported 1526320 versus1353352 participant input+output tokens across both phases;
the first phase alone favored full source. Coordinator preparation, review and
repair costs were not metered separately. Full-cycle/project savings remain
unproven; the earlier uninstrumented pair is additional historical cost. These
results support feasibility on this one boundary, not universal equivalence.
See 2026-11-03-dependency-contract-paired-maintenance-result.md and
2026-11-03-dependency-contract-instrumented-pair.md for failures and limitations.

A full-source-only rule also has a structural limit: the measured MCP entrypoint
closure at19d78215 was1739710 source tokens before tests and authority. Moving
imports to another file does not remove transitive implementations. After direct
IO imports reduced memory_state's source closure to31686tokens, a conservative
complete-context seed including its full dependency guides, bootstrap, authority,
recovery tests and historical reports is112625tokens, before its new owner guide,
manifest and remaining tests. That seed is not a qualified packet or proof that no
further safe documentation decomposition is possible. It does demonstrate why a
small source file is not enough. Evidence: logs/memory-state-owner-context-proposal.json.

## Requested architectural acceptance

Allow an explicit qualified_dependencies mode in canonical maintenance manifests,
initially for the already-tested answer_cost → answer_budget boundary. A consumer
retains all its implementation, tests, fixtures, instructions and complete public
interface/semantics for each crossed dependency. Each provider retains its full
implementation, tests and history in its separately qualified packet. The deployed
product still executes the real provider; no stub replaces runtime behavior.
All files and records still require unique ownership, and each complete provider,
consumer and integration packet still must fit100000 o200k_base tokens.

This is permission to implement and validate the mode and extend its applicability
only where evidence supports it, not automatic acceptance of any boundary. Keep
full source as the default. Unsupported private/dynamic access, implementation-
sensitive patching, incomplete semantics, missing ownership or failed integration
must refuse qualification. Do not hide imports or exempt expensive tests. Break
real state-owner cycles and oversized owned implementations by ordinary refactoring.

## Implementation and review gates

1. Preserve the current full-source manifest behavior and all its negative tests.
   Add an explicit declaration naming provider, complete interface source and
   semantic record, consumer accesses, provider packet and integration evidence.
2. Bind to exact source bytes and actual exports/call sites; include qualifier/helper
   code, schema, Python runtime and installed dependency identity in evidence.
   Use fresh qualification, not an unchecked persisted success cache.
3. Run provider and real consumer/provider tests, access/patch/binding checks and
   packet reconstruction. Reject stale or substituted evidence, omitted fields,
   undeclared accesses and missing interface semantics. Prose or signatures alone
   cannot stand in for the contract. Preserve existing error/deadline/ownership/
   side-effect/cleanup behavior explicitly.
4. Keep prototype full-source and contract versions comparable. Existing paired
   evidence applies only to its tested task/boundary. New representative maintenance
   work must include failures, verification, coordination and preparation costs;
   unknown costs stay unknown. Do not infer general or full-cycle savings from
   the participant-only result. A boundary with inadequate evidence remains
   unqualified even after this architecture is accepted.
5. Report acceptance per boundary, current token counts, limitations and uncovered
   files. Whole-project completion still requires complete ownership and all
   applicable local/native gates. No live deployment or installation is authorized.

No new agents are authorized by this proposal. If further independent paired agent
work is necessary, its scope and permission remain separate. Existing local test,
source inspection, documentation decomposition and full-source qualification can
continue while the architectural question is pending.

## Alternatives and research

Continue full-source leaf qualification and exact document transfers: useful,
but alone insufficient for a root importing the whole application. Increase the
limit, omit source silently, use hidden dynamic imports or create an unbounded
shared bucket: rejected. Qualified interfaces are a normal modularity mechanism,
with real semantic-contract and verification costs that must remain visible.

Rechecked three independent primary sources2026-11-04:
[PEP544](https://peps.python.org/pep-0544/) describes explicit structural interfaces,
[Cockburn's original2005 article](https://alistair.cockburn.us/hexagonal-architecture)
explains testable application boundaries, and
[Seemann's2011 composition root](https://blog.ploeh.dk/2011/07/28/CompositionRoot/)
explains why top-level wiring sees all providers. These are foundational sources,
not newly published evidence or a proof of LLM savings. No DI framework or runtime
protocol conversion is proposed.
