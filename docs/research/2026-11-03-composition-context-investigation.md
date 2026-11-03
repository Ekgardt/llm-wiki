# Composition entry points and complete maintenance context — investigation

Date: 2026-11-03. No qualification gate is changed by this investigation.

The current static closure follows every local production and test import through
implementation source. This correctly exposed missing test consumers and remains
the enforced rule. It can also make an application entry point carry most of the
product even after implementation responsibilities are separated. Measure actual
source-only closure before deciding whether that obstacle exists; packet totals
must additionally include instructions, contracts, tests and framing.

The approved completion plan asks for complete necessary dependency contracts,
not a prescribed number of new files or a second product. A possible later
refinement is an explicit, qualified public dependency contract: full provider
interface source plus semantics, real provider/consumer contract tests and exact
binding validation. This must not be an unchecked summary, a blanket import
exception or a string-based loading trick that hides implementation dependencies.
Private accesses, monkeypatch sites, dynamic loading and implementation-sensitive
integration tests may still require full provider source and further decomposition.

Open obligations before any design acceptance: identify a concrete representative
entry point, show which source is necessary for its own responsibility, preserve
all assertions, prove the provider contract matches real behavior and call shape,
refuse unsupported accesses, show update invalidation, retain unique whole-project
ownership and separate integration qualification, and measure a complete real
maintenance task. Do not mark current packets incomplete merely because a future
alternative exists, nor claim the whole project can finish using an unbuilt model.

Primary sources checked 2026-11-03 (three independent publishers):
- PEP 544 (accepted, Python 3.8+) provides structural interfaces. It does not
  validate arbitrary runtime semantics or security. https://peps.python.org/pep-0544/
- Cockburn's original 2005 ports/adapters article distinguishes application logic
  from external adapters and calls for tests that detect boundary leaks.
  https://alistair.cockburn.us/hexagonal-architecture
- Seemann's 2011 Composition Root article places wiring at the application entry
  point and distinguishes it from library responsibilities; no DI container is
  required by that principle. https://blog.ploeh.dk/2011/07/28/CompositionRoot/
These are established principles checked today, not new benchmark evidence.

Rejected as completion shortcuts: omit source merely to fit a quota, weaken
existing closure assertions, replace real integration with mocks, change imports
to evade AST discovery, or declare an unbounded shared bundle outside the budget.
The current full-source gates remain in force while this investigation continues.

## Concrete progress and remaining gate problem

At f238153c, five connected model value classes are independent of transport.
The next investigated seam is the queue retained-answer verifier's controller
import, followed by actual call recovery/resolution. These can remove concrete
reverse dependencies without changing any context gate. They do not by themselves
solve the application entry-point closure problem: a thin composition root can
still import every qualified subsystem.

A possible qualified-interface model must be designed and tested before use.
Candidate obligations are: an explicit public interface and complete semantics;
real provider contract tests in the provider's own full packet; exact implementation
and interface bindings whose changes invalidate prior qualification; consumer
checks that reject undeclared/private access and unsupported dynamic dispatch;
consumer integration assertions preserved; implementation-sensitive monkeypatch
or corruption tests retaining the actual implementations they exercise. Merely
listing module names in an exemption array meets none of these obligations.

This would refine the original plan's necessary-dependency-contract requirement,
not permit a hidden unbounded shared implementation. Every implementation,
integration fixture and historical record still needs a unique accountable owner
and a bounded complete packet. Real paired maintenance tasks must demonstrate
that the contract view preserves useful outcomes across changes, including failed
attempts and verification cost. No such model is implemented or accepted here;
current full-source closure remains mandatory.
