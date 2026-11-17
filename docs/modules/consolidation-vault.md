# Answer one LongMemEval question twice — without and with consolidation (MEM-13)

`benchmark/consolidation_vault.py` and the tests that exercise them most.

MEM-13 asks whether this vault's consolidation earns its cost. The only way to attribute a difference to consolidation is to change nothing else, so both arms run in **one process, on one vault, over one ingest**:

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
