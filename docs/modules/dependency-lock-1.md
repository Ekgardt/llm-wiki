# The locked dependency set, part 1 of 5

This packet owns one exact byte range of `uv.lock`: record `part-1`, which begins at "version = 1".
The source stays one file in its original place. The catalog proves the parts rebuild it byte for byte,
and every load validates the whole source, including parts this packet does not select.
After a change to the source, run `python -m tests.regenerate_module_records` on the catalog. No executable source or runtime state belongs here.
