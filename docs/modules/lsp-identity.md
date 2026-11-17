# Discover the managed language server for one profile, without installing it

`scripts/lsp_identity.py` and the tests that exercise them most.

`pyright_profile.discover_pyright` answers this question for Pyright, and does more than a second language needs: it accepts a project-local or system candidate and re-derives the identity from a lockfile. That precedence is defensible for Pyright because the whole identity -- package version, executable digest, lockfile evidence -- is re-derived for whichever candidate wins.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_slow_read_is_not_a_corrupt_install.py -q`.
