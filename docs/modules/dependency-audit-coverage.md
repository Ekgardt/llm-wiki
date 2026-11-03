# Dependency audit coverage

The CI-only verifier in `scripts/check_dependency_audit.py` compares the universal
uv.lock registry inventory with a successful OSV JSON report. It does not install,
resolve, update, or contact a service. Its inputs are UTF-8 TOML and JSON files;
its output is a coverage count, or a failure explaining the mismatch.

Every registry package's exact name/version must appear. Multiple versions of one
package remain separate entries. Missing/extra pairs, empty inventory, skipped
records, known advisories and invalid names/versions fail. A local root project
(source virtual/editable `.`) is not an external dependency; other source kinds
fail explicitly. Registry URLs are not emitted. Exact equality is deliberate:
a changed export or report representation needs review rather than silently
normalizing away evidence.

CI retains the environment-specific requirements checks and additionally exports
all groups/extras to pylock.toml for both advisory feeds. The strict OSV pass writes
the report consumed by this verifier. A successful coverage check is not proof
that every possible vulnerability is known to those advisory feeds. The earlier
PyPI pass is retained for complementary coverage, including its visible vendor
package limitation. No ignored advisory or Python support-floor change is added.

Run `pytest tests/modules/dependency_audit_coverage`; workflow wiring remains in
`tests/test_ci_policy.py` as a separate integration obligation. The packet owns
its implementation, synthetic regression fixtures, guide and research. Real lock
and live advisory checks remain whole-project inputs, explicitly outside this
unit qualification. Full-project completion still requires their ownership.

Source: [audit investigation](../research/2026-11-03-all-marker-dependency-audit.md).
