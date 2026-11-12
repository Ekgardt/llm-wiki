# Whether an archived bag member is read-only for its owner, on POSIX and on Windows

`scripts/llm_wiki/archive_read_only.py`, split out of `scripts/evidence_resolver.py`. Callers, including
`evidence_resolver`, read `archive_path_is_read_only` from this module; `evidence_resolver` keeps no alias.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
