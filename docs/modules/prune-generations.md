# Collect the evidence-graph generations nothing reads any more

`scripts/prune_generations.py` and the tests that exercise them most.

`CLAUDE.md` calls `cache/` disposable and regenerable, and every published generation is immutable after activation. Neither sentence collected anything: the only removal path the product had, `GenerationCatalog.discard_unactivated`, refuses any generation that was ever activated, which on a vault that builds nightly is all of them but one. Measured here on 2026-08-29: 35 registered generations, 6.3 GB, the oldest from 2026-08-21, growing about 180 MB a night.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_removal_past_its_point_of_no_return_finishes.py tests/test_an_abandoned_publication_is_collected.py tests/test_one_bad_generation_does_not_stop_the_prune.py tests/test_prune_generations.py -q`.
