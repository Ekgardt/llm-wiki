# Tests of `scripts/install_state.py`

- `tests/test_install_state_health_names_every_record_it_cannot_trust.py`

`scripts/install_state.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_install_state_health_names_every_record_it_cannot_trust.py -q`.
