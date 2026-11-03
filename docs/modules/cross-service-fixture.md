# Cross-service Git fixture

Owns `benchmark/build_cross_service_fixture.py` and
`benchmark/code-parity-cross-service-v1.json`, unchanged at their existing paths.
The builder creates two local Git repositories containing a client POST/GET
and service route handlers. The task JSON names the expected routes and source
citations. X02 deliberately exposes the existing navigation boundary: fixture
creation is not evidence that the product follows execution inside another
repository. The parity runner is a separate consumer.

The builder uses standard-library Python and an installed Git executable. It
writes UTF-8 sources, configures identity locally and commits both repositories.
An existing repository directory is refused. Construction is sequential, not
transactional: if the client destination already exists, the service repository
may already have been created. This existing behavior is preserved and tested.
It accesses only the explicit output location and does not install a runtime,
run a server or make an HTTP request. Git's normal process environment still
applies to the command; tests isolate Git configuration in their own temporary
directory. No FastAPI or requests installation is needed to write fixture text.

Tests inspect actual Git commits, exact emitted content, CLI output and both
existing-directory refusal paths. They retain source and task bytes and use the
normal pytest bootstrap. Import guards prevent runtime dependencies. The full
packet includes tests, task data and the complete September 11 route/binding
history, and must reconstruct every path and byte under 100000 o200k_base tokens.

## Decision evidence — 2026-11-03

This cohesive builder can be qualified in place under the approved autonomous
decomposition plan. Splitting its small implementation would add indirection;
merging the parity runner would mix fixture construction with execution and
evaluation. No source, command, data or runtime contract changes.

Reviewed primary sources: [bounded contexts](https://martinfowler.com/bliki/BoundedContext.html)
supports explicit responsibility boundaries; [pytest temporary directories](https://docs.pytest.org/en/stable/how-to/tmp_path.html)
supports isolated filesystem tests; [Git init](https://git-scm.com/docs/git-init)
documents real repository creation. This project-specific boundary is an
inference from the code and its consumers, not a universal prescribed layout.
