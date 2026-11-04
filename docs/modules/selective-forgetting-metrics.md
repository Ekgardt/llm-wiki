# Selective-forgetting metrics

Owns the deterministic calculations moved from `benchmark/run_selective_forgetting.py`
to `benchmark/selective_forgetting_metrics.py`: 22 existing functions and the two
existing dispatch tables. Cohort counts, paired rates, restore/legacy/session
metrics and gate records operate only on supplied values. The runner retains
clocks, phase subprocesses, source copying, report IO and cleanup. Its old names
are direct aliases of the same function/table objects; CLI, isolated runpy and
namespace startup remain supported.

All moved function/table ASTs and sixteen original pure tests are preserved.
Three test helper definitions move with them. The remaining twenty-four original
integration cases stay in the mixed suite. Existing None-versus-zero semantics,
four-place rounding, exact gate thresholds, boolean conversions, defaults and
exceptions are unchanged. These calculations do not validate the provenance of
supplied measurements. Empty/missing evidence and aggregate verdict behavior are
not redesigned or represented as fresh evidence of product efficacy.

The complete August 28 and September 13 reports remain historical evidence in the
packet. They describe dated runs and retired legacy retrieval paths; preserving
those reports does not reintroduce a legacy index or claim their measurements
apply to today's product. No live vault or benchmark dataset is used for this move.

## Research and qualification — 2026-11-04

Checked [Python JSON](https://docs.python.org/3.14/library/json.html),
[RFC 8259](https://www.rfc-editor.org/rfc/rfc8259), and
[bounded contexts](https://martinfowler.com/bliki/BoundedContext.html). Separate
calculation from process/IO ownership while keeping full implementations, value
semantics and explicit links. Keeping the functions in the runner couples pure
metric work to experiment execution; adding a framework or metric dependency adds
no needed capability. This is an architectural qualification, not a redesign or
scientific validation of the historical metrics.

Acceptance requires source/test AST preservation, original mixed-suite checks,
isolated import refusal, direct alias identity, native startup, actual complexity,
measured weights and exact reconstruction of a complete packet within 100,000
 o200k_base tokens. No whole-project completion or full-cycle LLM savings is claimed.
