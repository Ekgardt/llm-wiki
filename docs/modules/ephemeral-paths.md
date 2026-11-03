# Ephemeral path classification

`ephemeral_paths` owns host job-directory and platform temporary-root discovery,
given/resolved spellings, descendant classification and the narrower throwaway
checkout predicate. It consults environment and filesystem resolution; it is not
a pure lexical path sanitizer. Resolution's OSError fallback and ordinary Path
semantics are preserved. It neither deletes anything nor grants access.

`is_throwaway_checkout` recognizes the host jobs directory, not every directory
under the system temp root. Repository indexing, retention and transcript cleanup
keep their separate decisions, ownership and deletion protections. Qualify these
real consumers; a small pure-owner packet cannot claim their integration context.
Keep scripts/ephemeral_paths.py byte-identical. Tests cover default/configured
roots, spelling deduplication, containment and resolution failure, with native
platform skips explicit. Run `pytest tests/modules/ephemeral_paths
 tests/test_every_store_has_a_bound.py -q` as one command.

Shared canonical contract: [Maintenance qualification](context-tooling.md).
