# Durable external-call boundary for the approved compile controller

`scripts/compile_controller.py` and the tests that exercise them most.

The candidate CLI/MCP/nightly paths use this controller. The shared client prepares protected calls; semantic callers own work identity and publication. Installation and full dependency/recovery qualification remain unfinished.

The facade only re-exports names. The code lives in the fourteen
`scripts/llm_wiki/compile_controller_*.py` concern parts, each a module with its own
`compile-controller-<part>.md` guide; a test patches the part where a name is looked
up, because patching the facade's copy does not reach the part's callers.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
