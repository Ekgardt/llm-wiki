# One pipeline phase of the zero-silent-loss durability stand, in its own process

`benchmark/durability_child.py` and the tests that exercise them most.

The orchestrator (`benchmark/run_durability.py`) launches this driver with the trial vault named in the environment (`LLM_WIKI_ROOT`, `LLM_WIKI_STATE_ROOT`, `MEMORY_LLM_PROVIDER=fake`) and, for a killed trial, a stage name at which the process shoots itself with SIGKILL. SIGKILL cannot be caught, so every try/except boundary in the product is bypassed exactly the way a real process death bypasses it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
