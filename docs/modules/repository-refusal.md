# Named repository refusal

`repository_refusal` owns RepositoryIndexRefused and the repository-index/v1
refusal envelope. Its production file remains byte-identical. One module owns
the exception so running repository_index as __main__ and importing its sibling
helpers share a class. Do not move that class into an executable entry point.

The constructor stores message, stable reason and details. as_dict returns a
fresh outer mapping; existing details precedence is preserved, including keys
that overlap status/schema. It does not sanitize arbitrary details or promise
that an exception instance is pickle-compatible. The class import identity is
stable. No duplicate class, runtime imports, new schema or limits are introduced.

Direct envelope and import-boundary tests complement, not replace, the CLI test
that a non-Git checkout produces JSON and exit 2. Extraction-ceiling and slow
checkout tests continue to qualify downstream handling.
Run `pytest tests/modules/repository_refusal
 tests/test_a_refusal_class_lives_outside_the_script.py
 tests/test_a_ceiling_is_a_named_refusal.py
 tests/test_a_slow_worktree_does_not_end_the_pass.py -q`.
The complete packet retains source, tests, instructions, bootstrap and codec.
