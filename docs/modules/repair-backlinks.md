# Give every page the backlink it owes, without asking anyone to do it

`scripts/repair_backlinks.py` and the tests that exercise them most.

The vault's own rule is that a link is reciprocal: when a page names another, the named page links back, so a reader arriving from either side sees the connection ([[add-reciprocal-backlinks-at-creation]]). The compile pass writes pages that link outward and cannot edit the pages they name, so every compile left findings that only a human could clear — and a finding that waits on a human is the one thing this vault is not allowed to produce.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_repair_backlinks.py -q`.
