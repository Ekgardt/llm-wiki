# Tests of `scripts/llm_wiki/install_resource_builder.py`

- `tests/test_the_installer_builds_the_resources_its_platform_asks_for.py`

`scripts/llm_wiki/install_resource_builder.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_installer_builds_the_resources_its_platform_asks_for.py -q`.
