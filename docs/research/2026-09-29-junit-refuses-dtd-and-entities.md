# JUnit imports refuse DTD and entity processing

Research date: 2026-09-29. The parser correction is implemented and its focused
qualification passed; the wider project audit remains incomplete.

The CI timing importer accepts local downloaded JUnit artifacts. Four small
regressions demonstrate that its ordinary ElementTree parser accepts document
type declarations, including an internally expanded entity. The probes use only
synthetic text and nonexistent local paths. They do not establish file disclosure,
network requests or an actual denial of service on the installed host.

Use `defusedxml.ElementTree.parse` with DTD, entities and external references
explicitly forbidden. Preserve ordinary ElementTree results and the importer's
existing invalid-artifact error boundary. A pytest JUnit artifact does not need
DTD/entity features; rejecting those features has a security basis, rather than
an arbitrary byte or testcase limit. Predefined XML escapes remain supported.

Declare the existing locked defusedxml 0.7.1 package directly in base dependencies:
the lock currently contains it only through the optional audit environment, and
it is absent from the actual product/test interpreter. Depending on that incidental
transitive dependency would leave ordinary CLI imports broken. Its wheel is about
25 KB. No new runtime directory, process, environment contract or persisted format
is introduced. The loader remains available without requesting optional extras.

Alternatives: leave defaults and rely on the host's Expat version; implement our
own parser callbacks; scan text for declarations; or use a larger alternative
XML stack. The maintained purpose-built wrapper gives explicit rejection with
less custom security logic and no encoding-sensitive text prefilter. It does not
replace system Expat updates or guarantee protection against every resource attack
using a large ordinary XML document. No arbitrary file-size bound is introduced.

Three independent primary sources inspected today:

- [Python XML security](https://docs.python.org/3/library/xml.html#xml-security)
  recommends attention to untrusted input and the actual Expat version. This host
  reports Python 3.12.3 and Expat 2.6.1; current docs warn about versions below 2.7.2.
- [defusedxml's published documentation](https://pypi.org/project/defusedxml/)
  describes the parser protections and explicit forbid options. Version 0.7.1 is
  the published stable release checked today, originally released in 2021; its age
  is not hidden or claimed to be a new release.
- [OWASP XML prevention guidance](https://cheatsheetseries.owasp.org/cheatsheets/XML_External_Entity_Prevention_Cheat_Sheet.html)
  supports disabling unneeded DTD and external entity features.

Selection is a project-specific security tradeoff, not a claim that the wrapper
fixes every Expat vulnerability. Qualification requires hostile declarations and
ordinary reports, including a complete report path, plus lock/install consistency,
static analysis and the actual complexity gate. No parser warning is suppressed.

## Verification

The original parser failed all four rejection regressions while its 14 existing
checks passed. After the correction, the importer tests and actual Lizard/AST
complexity gate passed 32 checks in 28.31 seconds. They include UTF-16 DTD rejection,
predefined XML escape preservation, rejection through `compile_report`, and existing
complete ordinary reports. The CI policy suite separately passed 17 checks in
0.76 seconds. Ruff passed after correcting import order.

An intermediate verification run was invalid: a relative-path copy command ran
inside the destination and raised SameFileError, leaving the old parser loaded;
the isolated copy also lacked the workflow files. Its 20 failures are retained as
evidence, not counted as product regressions or successful qualification. Explicit
absolute source paths and copying the public `.github/` files corrected the harness.
The final manifest covers 1108 source/configuration files with no copy mismatches.

The locked wheel was installed with its SHA-256 required. Offline lock validation
passed, and all 97 installed packages are compatible. A current OSV query for
PyPI defusedxml 0.7.1 returned no vulnerability records; that is not a guarantee of
absence of vulnerabilities. Existing development analyzer dependencies in the
working-tree lock predate this parser correction.

Bandit still reports B405 for importing ElementTree: its only remaining uses are
four `Element` annotations and the `ParseError` exception class. All XML parsing
goes through the explicitly hardened wrapper. The B314 parsing warning disappears;
the import warning remains visible and has this source-specific disposition.
The full scan reports 352 findings and no parse errors. Evidence uses the prefix
`logs/audit-2026-09-29-completed-repair-junit-xml-`.

No alternate old parser remains. No runtime data cleanup, README version bump or
new environment/path contract was required. Private progress/log updates are
deferred until the active compiler releases its snapshot; these tests do not
qualify the entire installed system or finish the broader audit.
