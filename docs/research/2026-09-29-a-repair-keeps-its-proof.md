# A repair keeps its proof

Research date: 2026-09-29. This work follows the owner's development laws.
It preserves the existing runtime roots, snapshot location and scheduler contract.

The snapshot must reject overlapping resolved paths before mirroring anything.
Lexical path comparison misses symlink aliases. The existing snapshot repository
must be the standalone, remote-less repository created by the snapshot command;
an arbitrary repository is not evidence of ownership. Staging is restricted to
`knowledge/`, and previously staged foreign paths block the operation.
These checks preserve the existing local Git snapshot design. Replacing it with
another backup system would change scope without resolving the immediate defect.

The local Python runtime is 3.12; product compatibility remains Python 3.10+.
`Path.resolve` and Git's local configuration queries are available within that
baseline. These checks assume a trusted local filesystem; they do not claim to
prevent a hostile concurrent process from swapping paths after validation.

Complexity checks belong in the repository so another machine can repeat them.
Lizard 1.23.0 is locked as a development dependency for Python and JavaScript CCN.
Python's AST supplies the additional shape rules. PowerShell's native parser
exposes clauses and parent relationships, avoiding regular-expression counts of
keywords in comments and strings. The local PowerShell measurement uses verified
7.6.6; product scripts must retain Windows PowerShell 5.1 syntax compatibility.
The existing Bash parser remains tree-sitter-bash. A machine-local gate alone
cannot enforce CI, and Lizard does not provide a PowerShell or Bash parser.
More than one parser is a maintenance cost, justified by the shipped languages.

Sources checked on the research date:

- [Python pathlib](https://docs.python.org/3/library/pathlib.html): resolved paths
  include symlink resolution; lexical paths alone do not establish containment.
- [Git configuration](https://git-scm.com/docs/git-config): local configuration
  and configuration environment controls used in repository validation.
- [Lizard](https://github.com/terryyin/lizard): supported language parsers and
  per-function complexity measurements.
- [Microsoft IfStatementAst](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.ifstatementast?view=powershellsdk-7.6.0):
  clauses, parent AST relationships and parser traversal.

Validation evidence is recorded separately from design claims. At this checkpoint
the snapshot regression first failed against the original implementation and then
passed after the repair. Whole-project repair and runtime recovery remain in
progress; this page is not a declaration that the audit is closed.

The real nightly pass exposed a separate false refusal: the compiler's external
work guard rejected **any** persisted writer, including a different process.
Snapshot/compile/publication intentionally permits another process to write
between snapshot and publication; publication revalidates its preconditions.
The guard now rejects the current process's persisted writer ownership, retaining
the existing in-memory guard and the regression where another coordinator in the
same process holds the gate. A real second-process writer reproduced the old
failure; the new overlap regression and all 24 compile-hardening tests pass.
This changes neither the writer lease nor publication preconditions.

The Gitleaks exceptions were checked with 8.30.1 against the working public file
set and all 2,219 Git commits: both scans report no findings. A distinct planted
credential in an otherwise allowlisted test file remains detectable. The exact
line patterns use multiline anchors because the scanner's line slice can include
its delimiting newline; they do not exempt a file or a class of credentials.

The isolated full run exposed a test-environment assumption: a checkout below
the platform temporary directory was correctly classified as ephemeral, while
the test expected its own checkout to be permanent. That run finished with
10,299 passed, one failed, 162 skipped and 31 warnings. The fixture must supply
both its temporary root and a sibling permanent path. It must still verify
temporary and host-job paths as ephemeral and the permanent path as not
ephemeral. Moving the checkout solely to hide the failure, skipping the test,
or changing the production classifier would preserve the underlying test bug.

References checked 2026-09-29: [pytest scoped monkeypatching](https://docs.pytest.org/en/stable/how-to/monkeypatch.html),
[Python temporary-directory selection](https://docs.python.org/3/library/tempfile.html#tempfile.gettempdir),
and [Git worktree paths](https://git-scm.com/docs/git-worktree). Patch only the
module's reference to the directory provider, not the global standard-library
module used by pytest itself. This is a fixture correction with the same
assertions and no new product paths, dependencies or runtime contracts.
