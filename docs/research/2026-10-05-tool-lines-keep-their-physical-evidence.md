# Captured tool lines keep their physical evidence

Date: 2026-10-05. Candidate qualification; installation and the complete
compile/nightly cycle are separate requirements.

A captured tool event is one indented physical JSON line. A line can fit the
existing 16,384-character evidence contract while its UTF-8 bytes exceed a
16-KiB source part. Previously, packing could separate its two halves. The
ordinary evidence binder then inspected each part separately and refused a
complete quote as missing or ambiguous.

Keep the existing physical parts and their individual receipts. Group parts
crossing a canonical `post_tool_use` JSON line into one source unit; retain an
already committed companion as context when its other half is pending. Reject
incomplete cover before producing model input. Bind a complete ordinary quote
through that unit, check the original timestamp, original bytes and hash, and
recheck that the selected canonical parts cover the complete reference.

Tool evidence remains ordinary v1 physical evidence. It gains no native-user
selector or v2 authority. The complete JSON, including the tool target, stays
visible. User projections, unknown JSON and the on-disk partition algorithm
retain their existing contracts. No source bytes, occurrence, history or
retained attempt are removed. Grouping is not proof of model capacity; the
known attempt budget must independently admit the whole source unit.

The compiler resolves the actual Codex model before packing and keeps that
attempt's selected descriptor through context refresh, retries and dispatch.
Fit and dispatch share the smaller of the declared batch budget and a verified
native advertised window. A fallback uses its own descriptor and capacity;
unknown capacity stays unknown. This change retains the existing packing target
and optional-context policy. It does not qualify a larger target or justify
the old target, answer reserve or slack. Those remain separate audit work.

The original budget/refresh regression failed three checks and passed one:
final fit and dispatch inherited a larger batch window, and refresh discarded
the selected model. Six related integration checks now also cover resolution
before packing, one retained descriptor and fallback separation. The native
read-only check prepared both genuine physical cuts with the selected model
and executable, without an inference or publication. It is a local estimated
input check, not an exact full-wire token count or an enforced output cap.

Noncanonical JSON containing nonfinite numbers retains the unknown-source
partition contract. Both JSON parsing and canonical validation must succeed
before a line can gain grouping authority. Three negative checks exposed a
canonical-validation exception in the first candidate; the failed run is
retained and the validator now returns the ordinary nonmatching result.

Alternatives rejected: changing historical byte partitions invalidates receipt
identity; quoting fragments weakens physical evidence; treating tool targets as
user text changes authority; dropping targets or duplicate events loses context.

The corrected original regression had four failures and three passes. A
subsequent whole-quote regression exposed the independent binder defect. The
combined candidate passed 450 related tests; all changed functions and nested
callables measured CCN at most five. Two genuine physical cuts replayed with
exact original references. This is not model-quality, full-cycle token,
Windows, or Python 3.10 runtime qualification. The first fixture mistakenly
counted the separate journal header as a JSON half; its failure was retained,
then the corrected fixture reproduced the actual split on the original code.

Source and design rationale, checked 2026-10-05:

- [CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/): indented code retains
  literal content; presentation boundaries do not redefine physical evidence.
- [Unicode 17.0, chapter 2](https://www.unicode.org/versions/Unicode17.0.0/core-spec/chapter-2/):
  encoded byte length and character length are different quantities. This is
  the basis for testing the character contract separately from source bytes.
- [W3C PROV-DM](https://www.w3.org/TR/prov-dm/): provenance retains entity and
  activity identity and derivation. Here, retaining every original part and
  binding the complete original source is the application-specific choice.
- Existing `evidence_resolver._daily_part_bounds`, `EvidenceResolver`,
  `compile_memory._bound_evidence_block`, and the existing complete-part
  generation/receipt contracts supply the actual authority rules. The external
  standards do not establish this project's implementation correctness.
