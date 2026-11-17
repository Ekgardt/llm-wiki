# Queue v3 candidate

`llm_wiki.queue_v3_candidate` decides whether a built Reliability v3 queue database is sound enough to adopt, including the digests of ordinary purge records. It only reads the candidate. Adoption itself stays in `queue_core`.

Own tests are under `tests/modules/queue_v3_candidate`. The owned files and the complete maintenance
context are listed in the `-context.json` manifest next to this guide.
