# Code language classification

`code_languages` owns the immutable complete suffix map, classifier version,
canonical-map digest identity and final-suffix lookup. The production file stays
byte-identical. Classification is case-insensitive and lexical, using PurePath;
it performs no filesystem access, content sniffing, shebang parsing or model call.
Unknown suffixes return None. The LSP profile registry has a separate contract.

Two complete-map/identity tests moved intact from the corpus suite. Its independent
expected map now has one test-support owner, shared with unchanged collector
parameterization. Corpus/code-graph delegation, immutability through the graph
alias and classifier-generation invalidation remain integration obligations.
Direct tests cover both path flavors and unsupported/hidden/trailing suffixes.

Only standard-library imports are permitted; no collector, parser or runtime
owner may enter this module. No new limit, language, dependency or schema.
Run `pytest tests/modules/code_languages tests/test_corpus_snapshot.py
 tests/test_code_graph.py tests/test_generation_maintenance.py -q`.
The packet includes exact source/tests, independent fixture, bootstrap and
measurement tooling. Source classification does not promise parser availability.

## Structural reference retained from the overview

### Code classification and named repository refusal

The existing `code_languages` and `repository_refusal` owners remain independent,
with their present imports and behavior. Separate packets will own suffix-map
identity and named refusal values; corpus/index/CLI integration remains with
callers. See [research](../research/2026-11-03-code-classification-refusal-packets.md).
