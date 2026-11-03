# Saved compile answer records

This owner encodes and validates the existing compile-model-answer/v1 and
compile-candidate-result/v1 envelopes. It retains all 17 representation and call
binding functions without algorithm changes. The three completed-refusal labels
are shared with the controller. Canonical JSON remains NFC-normalized, sorted
and float-free; this is the existing format, not a byte-preserving text codec.
Usage keeps unknown values distinct from reported zero.

Work, source, stage, sequence and model-call identity bind the saved answer.
Delivery task identity may change through redelivery; an artifact reference may
be attached after publication. _require_same_call retains these existing rules.
The queue still verifies its actual lease, task family, artifact hash and SQL
transaction. This module neither reads artifacts nor authorizes replay. A decoded
transport result is not a semantically approved compile plan.

The controller keeps four used direct function aliases and the refusal constant.
Remove them only when its call paths and existing helper consumers no longer use
those names. The queue's retained-answer check imports this owner directly; its
separate settlement resolver still depends on the controller and is not claimed
to be decomposed here. No duplicate decoder, model transport or runtime store exists.

Tests retain seven pre-transfer response encodings/decoded values and eight
malformed-record refusals, including existing NFC normalization. Binding tests
distinguish delivery changes from changed calls. Real queue/controller recovery,
source overlap and protected dispatch tests remain unchanged integration gates.
Four relevant dated history records are shared through the complete verified
catalog; their broader controller history is not owned by this codec.

Source: ../research/2026-11-03-compile-answer-record-boundary.md.
Shared canonical contract: [Compile progress](compile-progress.md).
Shared canonical contract: [Model value records](model-records.md).
Shared canonical contract: [Context budget values](context-budget.md).
Shared canonical contract: [Canonical formats](formats.md).
Shared canonical contract: [SQLite deadlines](sqlite-deadlines.md).
Shared canonical contract: [Exact record contexts](context-records.md).

## Structural reference retained from the overview

### Saved compile answer records

`scripts/llm_wiki/compile_answer_records.py` owns existing saved model/candidate
answer bytes and their call binding. The queue's retained-answer verifier imports
this owner directly; the controller retains four used function aliases and the
shared completed-refusal set. No transport, SQL, artifact IO, repeat authorization
or settlement moves into the record owner. Remaining queue/controller dependencies
are separate work. See the [compile answer record contract](compile-answer-records.md).
