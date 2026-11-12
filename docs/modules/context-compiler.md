# Adaptive Context Compiler

`scripts/context_compiler.py`, `scripts/build_advisory.py` and the tests that exercise them most.

Materializes L0/L1/L2 representations for each captured source and packs them into one shared token budget. Query memory and get_context compile captured evidence here; other context producers share the final packing boundary through compile_context_items.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_context_compiler.py -q`.
