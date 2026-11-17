# Zero-silent-loss durability stand: trial vaults, kills, recovery, audit

`benchmark/durability_stand.py` and the tests that exercise them most.

One trial = one freshly adopted temp vault, one session_end capture driven through the real adapter -> queue -> worker chain in subprocesses (`benchmark/durability_child.py`), one SIGKILL at a named pipeline boundary, then the documented recovery (repeated capture-worker runs, with lease expiry simulated by moving lease timestamps into the past — the same clock advance the product's own tests use) and a byte-level audit of everything durable.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
