# Install one profile's pinned language server. A separate, explicit action

`scripts/install_language_server.py` and the tests that exercise them most.

`knowledge/notes/read-only-lsp-navigation-engine-decision.md` makes installation an operator action, never something a navigation query triggers. This module is that action for any profile in `lsp_profiles.REGISTRY` other than Pyright, which keeps its own installer (`scripts/install_pyright.py`) with its own descriptor-relative containment and atomic no-replace publication.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_download_that_resets_is_tried_again.py tests/test_a_pinned_zip_is_unpacked_as_a_zip.py tests/test_a_platform_without_a_pin_is_refused_by_name.py tests/test_a_toolchain_is_proven_before_it_is_published.py -q`.
