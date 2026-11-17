# A bitemporal reading of frozen claim/v1 records

`scripts/bitemporal_claims.py` and the tests that exercise them most.

Two clocks, both already carried by `claim/v1` and neither invented here:

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_less_reliable_claim_does_not_close_a_better_one.py tests/test_bitemporal_claims.py tests/test_conflict_resolution_stand.py -q`.
