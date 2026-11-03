# Large artifact maintenance contexts — investigation, not implementation

Date: 2026-11-03. Whole-project decomposition still has individual authoritative
files larger than the user ceiling: uv.lock, contradiction-v1 benchmark JSON and
the historical developer-audit status. The existing whole-file packet machinery
cannot truthfully claim these files fit. They remain unassigned until supported.

Candidate principle under the approved completion plan: logical record views
with original byte spans, preserved source path/digest, explicit context records,
and an independently checked whole-file coverage/reconstruction proof. Never
rewrite an authoritative lockfile or benchmark merely to shrink LLM context.
File ownership must not become complete when only one span has been assigned.
No source/code dependency may be replaced by a summary through this mechanism.

Current observations: contradiction-v1.json is a top-level object with cases,
categories, provider, relation_metadata, source and version. Audit status has
236 heading-looking lines and the durable-compile-progress history has 92;
those counts are not validated Markdown section boundaries. No parser or view
format has been implemented in this investigation.

Sources checked 2026-11-03 (three independent publishers):
- TOML 1.0 specification: table arrays, quoted keys and multiline strings mean a
  naive split on a textual [[package]] is not a general TOML parser. Its site
  also exposes 1.1; a view reader must follow the actual lock syntax and installed
  tooling rather than silently assume a current-spec upgrade.
  https://toml.io/en/v1.0.0
- CommonMark 0.31.2 distinguishes headings from fenced/indented code and HTML
  blocks. Regex heading lines alone do not prove independent semantic sections.
  https://spec.commonmark.org/0.31.2/
- Python JSONDecoder.raw_decode returns an end index in decoded text, not an
  original UTF-8 byte offset. A future lossless view must preserve whitespace,
  punctuation and encoding, with no reserialization-based equality claim.
  https://docs.python.org/3/library/json.html#json.JSONDecoder.raw_decode

Alternatives needing evaluation: actual repository-file splitting with consumer
migration versus derived context views. Gzip/base64 are storage encodings, not
readable maintenance contexts; summaries alone lose authoritative details.
Full source and record cross-references, safe parser behavior, deterministic
coverage, update invalidation and test ownership remain open design obligations.

## Concrete next slice: verified record catalogs (2026-11-03)

The compile history itself is 380666 bytes / 77044 o200k_base tokens. A storage
owner cannot carry that complete history plus its mandatory instructions, code
and tests under 100000. The history combines storage, model dispatch, capture,
planning, publication, repair, installation and later validation. It must stay
intact; it is not replaced by a new guide or declared owned through one excerpt.

Implement a test-only `module-records/v1` catalog: original tracked relative path,
whole-source byte length/SHA-256, and ordered named logical records with labels,
start/end UTF-8 byte offsets and individual SHA-256. Validate the complete catalog
against the original every time: exact keys/version, unique IDs, nonempty records,
contiguous nonoverlapping coverage from byte zero to EOF, complete UTF-8 boundaries,
individual hashes and exact concatenation. Changed source or record bytes fail;
there is no implicit clipping, normalization, reserialization or stale reuse.

Initial eligible source classes are documentation Markdown, benchmark JSON and
the dependency lockfile named by the approved plan. Executable source/test imports
and mandatory full guides/instructions cannot be replaced by records. Catalogs
record reviewed logical boundaries, not quota-sized arbitrary chunks. Format-aware
catalog generation is separate from byte validation: a digest does not prove a
heading is outside a code fence or a JSON record is semantically independent.
The initial historical Markdown has no fenced/indented code, HTML blocks or
blockquotes in the inspected file; that observation is not a general Markdown
parser claim. Each chosen group needs semantic review and required cross-record
context. JSON/TOML generation remains a separate qualification step.

A packet's optional `records` selects catalog IDs. It must also carry the complete
catalog, record validator/loader source and full record-context guide. The existing
lossless serializer emits original selected bytes under reserved record keys;
source path, byte range and digest remain in the included catalog. All existing
whole-source, test, bootstrap, instruction and guide checks still apply.

Ownership is distinct: optional `ownership.records` may claim only records the
packet actually contains. One source has one catalog authority; duplicate record
owners, competing catalogs and whole-file/record ownership conflicts fail. A
tracked source leaves the unassigned list only when every record has exactly one
owner. Partial ownership stays explicit, including missing IDs. The reporting
schema will distinguish whole-file ownership from record ownership and count a
fully covered source only once. No record-only claim counts an entire source as
complete. Runtime database and product file formats are unchanged.

First implement and qualify the byte validator, then integrate measurement and
ownership with synthetic omission/tamper/coverage tests, then register actual
history records and qualify the storage owner. Do not call this completed until
real catalogs reconstruct their originals and all packets fit. Physical artifacts
remain unchanged. The existing complete source import closure remains in force.

Additional independent primary sources checked today:
- W3C Web Annotation distinguishes text-position from data-position selectors and
  source state. This project uses explicit UTF-8 byte offsets, not its text-offset
  convention; no W3C serialization conformance is claimed.
  https://www.w3.org/TR/annotation-model/
- RFC 6920 describes content-derived identities. Hashes bind bytes, not semantic
  completeness or permission; those require the separate coverage/ownership checks.
  https://www.rfc-editor.org/rfc/rfc6920
Together with the Python, CommonMark and TOML primary sources above, these support
explicit source identity and format-aware boundaries. New third-party parsers,
product dependencies, source splitting and source-closure exemptions are rejected
for this slice.

Implementation boundaries: `tests/module_records.py` validates exact partitions;
`tests/module_record_context.py` loads contained catalogs and selected entries;
`tests/module_record_ownership.py` checks global record ownership. They form one
record-context owner. The existing contained-path primitive moves unchanged to
`tests/module_context_paths.py`, with its old measurement import retained as an
alias; this avoids a measurement/record-loader cycle and duplicate containment
logic. Measurement integrates optional records without dropping existing paths.
Whole-file ownership remains distinct from complete/partial record coverage.
The inventory's next schema is `module-ownership/v2`; this is test-tool metadata,
not a runtime database or public product format change.

## Qualified infrastructure checkpoint (2026-11-03)

The validator, contained loader, measurement integration and v2 ownership report
are implemented in the candidate. No production source or original historical/data
artifact changed. Actual catalogs remain the next step; none is yet counted as an
owned original. The former contained-path body has one owner and its existing
measurement import remains an alias for the demonstrated documentation consumer.

Eight measurement regressions first failed because selected records were ignored;
eight ownership regressions first failed because records had no accounting. Five
additional omission cases exposed missing enforcement of complete authority files.
All now pass. Mandatory full AGENTS/CLAUDE/laws/docs instructions/STRUCTURE cannot
be replaced by records. The synthetic cyclic-import fixture now counts twelve
files instead of seven because it includes those five required authority files;
its original dependency and cycle assertions remain intact.

Validation: 1704 passed / 2 skipped across module and repository policy checks;
then the final expanded record/context/ownership suite passed 219 on Python 3.10.
Actual function CCN/shape and Ruff checks pass. An initial integration run had one
untracked-ownership failure before new metadata was staged, correctly enforcing
tracked ownership; staging resolves it without weakening the guard. Earlier JSON,
path and missing-module red runs are retained in private module-record-* logs.
All 67 emitted packets reconstruct original whole-file paths/bytes. Record tests
also reconstruct selected bytes and independently validate the entire source
partition. Actual catalog proof remains required when catalogs are introduced.
The inventory still reports incomplete: 537 owned / 3262 tracked, 2725 unassigned.
No native platform or useful LLM task-cycle saving is inferred from these checks.

## Whole-file owner with shared record context

The first real catalog review exposed an inventory inconsistency: a source already
owned as a whole file could also be used as shared record context with no record
ownership, yet appear in partially_owned. A new regression reproduced that false
partial listing. Coverage now excludes whole-owned sources from record completion
accounting, while the existing whole/record ownership-overlap refusal remains.
All nine record-ownership cases pass, including duplicate and overlapping claims.
This corrects report classification; it neither changes record bytes nor grants
ownership to a context-only selection. The separate red/green logs are retained.
