# Tests of `scripts/markdown_transaction.py`

- `tests/test_an_append_whose_images_were_pruned_can_be_asked_again.py`: An append asked again after its undo images were pruned writes, and does not raise.

`scripts/markdown_transaction.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_append_whose_images_were_pruned_can_be_asked_again.py -q`.
