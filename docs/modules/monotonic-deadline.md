# Whether an absolute monotonic deadline has passed

`scripts/llm_wiki/monotonic_deadline.py` holds `deadline_reached`, the single clock read the
doctor, generation maintenance and Reliability v3 validation use to stop at their caller's
absolute deadline. Callers read it as `monotonic_deadline.deadline_reached(...)`, so a test that
stands in for the clock reaches every check without depending on the internals of a larger module.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
