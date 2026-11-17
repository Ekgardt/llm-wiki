# Compile knowledge/daily/*.md into knowledge/notes/* durable pages

`scripts/compile_memory.py` and the tests that exercise them most.

CLI: uv run python scripts/compile_memory.py # compile changed daily logs uv run python scripts/compile_memory.py --all # deprecated, does nothing and # says so: a day with a # committed receipt is never # compiled again uv run python scripts/compile_memory.py --file PATH # compile one daily log uv run python scripts/compile_memory.py --dry-run # plan only, no writes uv run python scripts/compile_memory.py --trigger auto|manual # records invocation source in # state.json; `auto` is set by # flush_memory.py when the 18:00 # hook spawns this compile, any # direct CLI run defaults to # `manual`. Surfaces as # "Automated compile pass" vs # "Manual compile pass" in # knowledge/log.md.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
