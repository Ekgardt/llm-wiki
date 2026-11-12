# Build a classification corpus from this machine's real sessions

`benchmark/build_flush_corpus.py` and the tests that exercise them most.

`OPEN-034` asked how often a session holding a decision, a fix or a gotcha loses it on the way into durable memory. The measurement stand existed; the corpus did not — the one shipped in the repository is nine synthetic public cases, and the register said a real number was impossible here because there was no installed runtime. That stopped being true when the vault and the source became one directory: the real transcripts are on this machine.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
