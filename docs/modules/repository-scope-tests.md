# Tests of `scripts/repository_scope.py`

- `tests/test_a_revision_holds_only_what_it_proves.py`: The workspace revision holds only relevant files, nested server configuration included.
- `tests/test_code_kernel_helpers.py`: Shared code-kernel fixture and helper contract tests.
- `tests/test_generation_reuse_scope_identity.py`: The reuse gates ask identity, so a commit alone no longer defeats them.
- `tests/test_repository_scope_identity_guard.py`: No new site may decide repository identity by comparing whole scope records.

`scripts/repository_scope.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_revision_holds_only_what_it_proves.py tests/test_code_kernel_helpers.py tests/test_generation_reuse_scope_identity.py tests/test_repository_scope_identity_guard.py -q`.
