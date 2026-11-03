# Language-neutral server profile values

`scripts/lsp_server_profile.py` owns validated immutable language-server profile
values, artifact declarations, runtime-option projection and suffix registration.
It uses only the standard library and performs no installation, server discovery,
process launch or protocol communication. Existing public paths and algorithms
remain unchanged; one prose reference follows the relocated Pyright data helper.

A declaration records what an installer or session must honor; it does not prove
that the declared artifact exists or that the server is ready. Consumers retain
those checks. Freeze/thaw semantics, launch path validation, readiness policy,
platform normalization and duplicate suffix refusal retain their existing rules.
This freeze domain is distinct from Pyright's restricted default-value helper.

Ten pure tests and their _profile factory moved intact from the mixed equivalence
suite. `tests/modules/lsp_server_profile/paths.py` owns the common platform-correct
absolute path fixture once; the mixed suite imports it. The original Windows
anchor rationale is retained. Registry/Pyright/session equivalence cases remain
in tests/test_lsp_server_profile.py. No assertion is removed or weakened.

Run `pytest tests/modules/lsp_server_profile tests/test_lsp_server_profile.py -q`.
The full source, own tests/support, instructions, pytest bootstrap, guide and
measurement codec are included. Isolated-import guards refuse product dependencies.
No runtime root, cap, package dependency or minimum Python version changes.

## Structural reference retained from the overview

### Language profile data

`llm_wiki.pyright_configuration` owns immutable Pyright package/configuration
constants and their existing freeze/thaw implementation. `pyright_profile` keeps
runtime discovery and compatibility aliases; `lsp_profiles` reads the data owner
directly. The existing stdlib-only `lsp_server_profile` is qualified in place.
See [boundary research](../research/2026-11-03-language-profile-data-boundaries.md).

Full contracts: [Pyright data](pyright-configuration.md), [Profile values](lsp-server-profile.md).
