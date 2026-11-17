# Task 19: atomic full Evidence Graph generation builder

`scripts/evidence_graph_builder.py`, `scripts/repository_indexing.py` and the tests that exercise them most.

Builds an unpublished generation directory under ``<state_root>/cache/evidence-graph/generations/<generation-id>/`` while existing readers continue using the prior active generation. The build pipeline is:

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
