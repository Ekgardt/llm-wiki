# Memory reflection — offline page consolidation (A-MEM evolution pattern)

`scripts/reflection.py` and the tests that exercise them most.

Runs periodically (weekly via scheduled_weekly.py) to evolve the knowledge base. Finds pages that have accumulated multiple Update sections and rewrites them into a clean, integrated narrative — folding the updates into the main text. Old content is preserved in a ## History section.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
