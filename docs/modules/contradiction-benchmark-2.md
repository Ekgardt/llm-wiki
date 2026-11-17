# The contradiction benchmark cases, part 2 of 8

This packet owns one exact byte range of `benchmark/contradiction-v1.json`: record `part-2`, which begins at "{"block_id":"00:37:00","category":"equality","existing_claim":{"author".
The source stays one file in its original place. The catalog proves the parts rebuild it byte for byte,
and every load validates the whole source, including parts this packet does not select.
After a change to the source, run `python -m tests.regenerate_module_records` on the catalog. No executable source or runtime state belongs here.
