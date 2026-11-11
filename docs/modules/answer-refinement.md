# Answer refinement policy

This owner retains `aggregation_pass.py` and `refusal_pass.py` byte-identical.
They prepare and interpret refinement work on an answer: counting/refusal
triggers, count units, mention ordering, clustering replies and query prompts.
Callbacks are supplied by callers. `query_memory` retains protected model calls,
retrieval, deadlines, calendar precedence and second-answer adoption or fallback.
This owner creates no model client, database, scheduler or runtime state.

Complete `reply_json` source and its guide remain in the packet. Its one-document
rule is shared by clustering and query replies. No dependency contract removes
implementation context. Imports remain at the existing public paths.

## Preserved behavior

Counting triggers combine declared count/sum derivations with existing English
question shapes. Blocking orders mentions; it does not merge identities. The
supplied clustering callback decides the groups; existing index checks and
nine-item chunks remain. Count-unit wording, prompts, edge detection and the
12-query fanout cap retain their existing values and historical rationale.

Refusal policy recognizes a non-answered status or nonempty dropped claim text.
It prepares one missing-evidence prompt, delegates to the supplied callback and
uses the same query parser, retaining the five-query cap. Empty/malformed replies
keep existing empty results. There is no new claim that finite parsing or policy
tests prove model reliability, correct entity identity or citation entailment.
The consumer owns when these policies are applied and whether a second answer
replaces the first. Existing wider-fetch and failed-second-look tests remain.

## Ownership and verification — 2026-11-03

Nineteen original pure tests move intact from four mixed integration files, with
their original helpers/data. No integration function, decorator or assertion is
changed. Tests for collection, grounded answers, calendar precedence, candidate
fusion and fallback remain at their original entry points. New refusal and
import-boundary checks complement the original evidence.

The two focused September8/September19 papers are owned here. Broad September7
planning papers and shared September14 reply-JSON rationale remain complete
context dependencies with separate historical ownership. Historical numerical
claims are dated evidence, not a new measurement of today's product.

The approved autonomous plan permits qualification of existing cohesive owners.
Alternatives were duplicated unit tests, whole-query source context for pure
policy changes, or new protocol wrappers. Exact test transfer and the existing
concrete dependency keep the boundary simple. Three independent primary sources
reviewed on this date are
[Fowler bounded contexts](https://martinfowler.com/bliki/BoundedContext.html),
[Import Linter contracts](https://import-linter.readthedocs.io/en/stable/contract_types/)
and [PEP 544](https://peps.python.org/pep-0544/). No new framework, runtime version
or RAG algorithm is introduced. Source-only closure is 5683 tokens in three files;
it is not the complete maintenance packet or a task-cost saving.

Baseline: 45 original mixed/integration tests pass. Require exact definition/data
transfer, unchanged production digests, focused and original integration tests,
actual CCN/shape checks, complete source/ownership gates and a full byte-reversible
packet of at most 100000 tokens. No release or live installation is implied.

## Structural reference retained from the overview

[Answer refinement](answer-refinement.md) owns existing counting/refusal
policies, prompt formation and interpretation of supplied callback replies.
`query_memory` retains protected model execution, retrieval, deadlines, calendar
precedence and second-answer adoption/fallback. Existing algorithms, prompts,
limits and public imports remain unchanged; pure tests move with their policies.
