# Qualified dependency contracts for composition and integration

Date: 2026-11-03. The owner explicitly approved implementing and verifying this
pilot. Approval is not qualification; the full-source gate remains unchanged
until the obligations below pass.

## Measured obstacle

At 19d78215 the enforced static full-source closure carries 211 source files /
1739710 o200k_base tokens for mcp_server.py, and 177 / 1422106 for
scheduled_nightly.py and operational_ownership.py. These are source-only lower
bounds, before tests, instructions and framing. Evidence:
logs/composition-source-closure-19d78215.json, measured by the existing
required_source_context implementation. Registry cycles still require concrete
refactoring. Independently, an application composition root eventually imports
all its providers even when they are individually well separated. Merely moving
that wiring into another file does not reduce its transitive source closure.

The user's original limit is unchanged: every complete maintenance packet <=100000
o200k_base tokens. The approved completion plan requires necessary dependency
contracts. Requiring every dependency implementation recursively in every packet
is a stronger current qualification rule; changing it needs explicit acceptance,
not a silent exception to make a count pass.

## Proposed bounded change

Implement and qualify a public dependency-contract mechanism, initially on one
real composition boundary. A consumer keeps its complete implementation, own tests,
fixtures, instructions and the full public interface source and semantic contract
of every crossed dependency. The provider's implementation, full tests and history
remain uniquely owned by its own bounded packet. No source, test or artifact is
removed from whole-project ownership. Existing full-source closure is the default.

An explicitly declared qualified dependency may stop source expansion only if:
1. A public interface describes actual call shapes, values/errors, side effects,
   state ownership, deadlines and relevant safety/recovery guarantees. A prose
   summary or signature-only stub is insufficient.
2. Binding is explicit and verified against real provider exports/call sites.
   The contract names the provider packet, implementation source digest, interface
   and semantics digests and the real qualification evidence. A relevant change
   invalidates qualification and fails closed until tests requalify the binding.
3. Static and runtime checks refuse private/undeclared access, unsupported dynamic
   import/dispatch and implementation-sensitive monkeypatches across that boundary.
   Cases the checks cannot establish retain full implementation context.
4. Provider tests and real consumer/provider integration assertions continue to
   run. Mock-only acceptance cannot qualify a provider. Integration work has its
   own bounded, owned packet using the same qualified-contract obligations.
5. Both sides independently meet <=100000 including all required context.
   Negative tests demonstrate stale hashes, omitted semantics, wrong bindings,
   private access, missing integration evidence and incomplete ownership refuse.
6. A real paired maintenance task compares correctness and total token/verification
   cost against the full-source workflow. If it cannot preserve useful outcomes,
   do not apply this model to that boundary or report savings.

Plan concrete schema and validation in docs/modules alongside existing manifests;
add a narrowly scoped optional qualified_dependencies declaration, not a global
ignore list. Store canonical interface/semantics/binding records with their owner.
Evidence must be reproducible and tied to source bytes, not an unchecked 'passed'
boolean. Existing byte reconstruction and full-source tests keep their assertions;
add explicit rejection tests for unsupported/invalid contract declarations. Refine
internal schema details under this plan only after showing these obligations.

Before broader adoption, complete one real pilot and report its results/limits.
Do not mark other packets qualified merely because the feature exists. Private
accesses or state-owner cycles still need ordinary decomposition. No new service,
framework, installation, Python/dependency change or runtime-root is approved.

## Alternatives and evidence

Continue only leaf extraction: useful and continuing, but does not alone qualify
an all-provider composition root. Hide imports behind strings, truncate source,
raise the limit or create an unbounded common packet: rejected. Model public
boundaries explicitly and verify them: chosen proposal, with extra qualification
cost and no claim of complete semantic proof from finite tests.

Three independent primary sources checked in the preceding investigation on
2026-11-03; they support interface/composition principles, not automatic guarantees:
- PEP 544, structural protocols: https://peps.python.org/pep-0544/
- Cockburn, original ports/adapters and boundary testing (2005):
  https://alistair.cockburn.us/hexagonal-architecture
- Seemann, Composition Root (2011):
  https://blog.ploeh.dk/2011/07/28/CompositionRoot/

Related: 2026-11-03-composition-context-investigation.md; approved
2026-11-02-decomposition-completion-plan.md. While approval or pilot evidence is
missing, retain the existing source-closure gate and continue independent work.
