# The contradiction benchmark cases, part 8 of 8

This packet owns one exact byte range of `benchmark/contradiction-v1.json`: record `part-8`, which begins at "{"block_id":"03:59:00","category":"quarantine","existing_claim":{"auth".
The source stays one file in its original place. The catalog proves the parts rebuild it byte for byte,
and every load validates the whole source, including parts this packet does not select.
After a change to the source, run `python -m tests.regenerate_module_records` on the catalog. No executable source or runtime state belongs here.
