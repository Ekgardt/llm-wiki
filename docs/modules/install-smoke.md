# Bounded production-profile smoke test for a local LLM-Wiki install

`scripts/install_smoke.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_failed_smoke_names_what_failed.py tests/test_a_smoke_checks_the_install_not_the_vault.py tests/test_doctor_waits_out_a_busy_admission.py tests/test_install_smoke.py tests/test_the_smoke_report_goes_to_a_file.py -q`.
