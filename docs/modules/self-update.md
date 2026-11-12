# Advance the vault's own checkout, fast-forward only, or say why not

`scripts/self_update.py` and the tests that exercise them most.

The owner's requirement is that the product improves without him typing `git pull`. The danger is that this working tree holds both the product's source and his knowledge, and the runtime keeps it dirty by rewriting the tracked index and log on every compile. So the rule is not "clean tree required" — that would be an off switch — but "no file this update would change may be modified here".

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/an_untracked_copy_of_the_update_does_not_stop_it_helpers.py tests/test_a_sync_removes_what_the_lock_no_longer_names.py tests/test_a_verified_first_install_then_follows_main.py tests/test_an_untracked_copy_of_the_update_does_not_stop_it.py tests/test_an_update_keeps_what_is_installed.py tests/test_an_update_only_to_what_main_holds.py tests/test_an_update_says_what_it_did_not_bring_into_force.py tests/test_self_update.py -q`.
