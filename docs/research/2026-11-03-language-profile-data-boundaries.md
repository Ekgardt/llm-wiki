# Language profile data without runtime discovery

Date: 2026-11-03. Base: d4a66774. Designed under the approved decomposition
mandate; implemented and qualified in the isolated candidate; evidence below.

## Observed coupling and chosen boundaries

lsp_protocol reads the notification registry from lsp_profiles. That registry
imports Pyright constants from pyright_profile, which also imports process-tree
ownership, reliable_memory and repository discovery. Immutable defaults should
not require that runtime dependency closure. Move only the five existing
freeze/thaw functions and public package/configuration/schema constants into
`scripts/llm_wiki/pyright_configuration.py`. The only product dependency is the
existing canonical JSON/hash owner. Keep identical function bodies and values.

pyright_profile retains aliases for installer, discovery, fixtures and public
imports. Its functions continue reading its own bindings, preserving intentional
package-integrity/digest monkeypatches in install tests and common fixtures.
lsp_profiles alone switches its data import directly to the lower owner. No
probe, process, filesystem permission, manifest validation or deployment changes.
The public thaw function keeps its Python serialization module identity.

The already stdlib-only `scripts/lsp_server_profile.py` is separately qualified
in place. Move its pure validation/freezing/runtime-option tests from the mixed
Pyright equivalence suite without changing their ASTs. Their absolute-path helper
moves once to a shared test support file used by both suites; retain the Windows
anchor rationale. Pyright/session/registry equivalence scenarios remain in their
original mixed suite. No duplicate helper and no assertions discarded.

## Primary sources and alternatives

Checked today:
- Microsoft Pyright documentation at the existing pinned 1.1.411 tag: current
  configuration names in this build; no upgrade or newer-release claim.
  https://raw.githubusercontent.com/microsoft/pyright/1.1.411/docs/settings.md
- Python MappingProxyType: a read-only view alone is not recursive immutability;
  retain the existing recursive freeze and fresh mutable thaw behavior.
  https://docs.python.org/3.10/library/types.html#types.MappingProxyType
- npm package-lock contract: artifact integrity and resolved package identity are
  data, while installing/verifying those artifacts is a separate operation.
  https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/
- Independent Import Linter project: enforce forbidden imports as an executable
  boundary; the existing subprocess finder suffices without a new dependency.
  https://import-linter.readthedocs.io/en/stable/contract_types/

Alternatives: lazy-importing the same runtime owner only delays coupling;
retyping constants in lsp_profiles creates competing authority; merging Pyright's
freeze/thaw with the generic profile functions changes accepted value domains.
Choose one data owner, preserved public aliases and a verified direct registry
import. Keep Python >=3.10 compatibility; upstream Python 3.10 support has ended
according to its documentation, which this structural change does not remedy.

## Planned evidence

Fresh index and callers at d4a66774; exact AST and constant fingerprint proof;
pre-move configuration/pin/manifest and registry baseline; a registry import guard
that must fail while discovery is reachable; owner isolation, public identity,
immutable nested values and unchanged hashes. Full installer/discovery and LSP
consumers remain qualification obligations. Both complete actual packets must
round-trip and fit <=100000 o200k_base tokens; source/guide/pytest closure and
actual CCN/shape checks apply. No live installation or native CI claim.

Exact refinement before implementation: ten public constants and five functions
move. The two standalone immutability tests move intact; manifest/dataclass tests
stay integrated. Generic profile pure tests include the one-runtime-only case,
so their _profile factory has one owner. The sole planned production prose edit
outside the transfer corrects freeze_profile_value's reference to the relocated
private function; it does not change that function's algorithm or value domain.
Baseline mixed profile/discovery qualification: 186 passed.

## Qualification evidence

Fresh source index `generation-18e46d8146622318-c3c3fc70` corresponds to d4a66774.
The incomplete graph reports 20 thaw callers, 4 freeze callers, 18 generic freeze
callers, 3 notification-union callers and 4 routing callers; source/import/fault
patch searches supplement it. Evidence: `logs/language-profile-data-graph.json`.

Three boundary guards failed before the transfer, including an actual isolated
registry import rejected at pyright_profile. After transfer all pass. All five
moved functions, all remaining discovery bodies and every original test function
retain AST identity. Generic profile implementation changes only a prose reference;
registry algorithms are unchanged. Ten constant representations and the public
thaw pickle match the saved baseline exactly. No old assertion was discarded.
Evidence: `language-profile-data-transfer-proof.json` and saved source/value baselines.

Initial mixed baseline 186 passed; after extraction plus boundary guards 189 passed.
Complete focused packets/integration with additional behavioral guards: 200 passed
on Python 3.12 and 200 on Python 3.10. Installer, executed-artifact receipt, language
wiring and session consumers: 324 passed, 9 native skips. Ruff and actual complexity
and shape checks passed. The full regression at 4abf9ccd is still running and
predates these changes. Native Windows/macOS workflow dispatch remains pending.

Global module, privacy, CI, complexity, source/guide/pytest closure and structure
qualification: 1246 passed, 2 skipped. All 53 actual serialized packets round-trip
all paths and bytes under 100000 tokens. Before this final evidence paragraph,
Pyright data measured 74166, generic profile values 78862; largest packet 96978.
Registry import guard remains an integration obligation pending its own packet.
Ownership 417/3147 files leaves 2730 unassigned; whole-project work continues.
These overlapping artifact sizes do not establish paired LLM-task savings.
