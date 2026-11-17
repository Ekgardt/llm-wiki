# Fetch the two pinned models the memory reads, verify them, and stop

`scripts/install_models.py` and the tests that exercise them most.

The read path loads weights from the local Hugging Face cache only, so a vault whose cache is empty answers by words alone and says so in the trace (`model_unavailable`, `reranker_unavailable`). This is the one place the product goes to the network for a model: each pinned commit is fetched with the file list limited to what is read, and the weights file is checked against the size and SHA-256 recorded beside the revision. A file that does not match is removed and the run fails. Present and verified files are not fetched again, so the nightly can run this every night.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/doctor_helpers.py tests/install_models_helpers.py tests/test_a_bad_model_file_is_fetched_again.py tests/test_install_models.py tests/test_the_installer_brings_the_runtime_of_every_model_it_fetches.py -q`.
