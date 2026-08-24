# Which pages count as current, and who gets to decide

Date: 2026-08-24
Reason: the retrieval stand missed four questions out of ten. Two of the gold
pages had zero chunks in the corpus. The collector keeps a page only when its
status is literally `active`, and this vault writes `accepted` on accepted
decisions. Before changing that rule I checked what the practice actually is.

## What was measured here first

Statuses present in `knowledge/notes/` on 2026-08-24: `active` 28, `accepted` 4,
`superseded` 2, `preliminary` 2. The active generation
(`generation-18ce8f0f77583532-d3ac7755`, 1190 sources, 5992 chunks) carries 59 of
the 68 notes. The nine absent ones are exactly the eight non-`active` pages plus
`README.md`. Four of them are decisions in force, including
`system-symlink-ancestor-decision.md`, which the stand asks about and never
finds at any depth.

The same rule is applied again at query time in three more places
(`scripts/search_memory.py`: the FTS clause `lower(status) = 'active'`,
`_superseded_now`, `_legacy_vector_excluded`), so fixing only the collector would
have changed nothing.

Meanwhile `scripts/lint_memory.py` and `scripts/rebuild_memory_index.py` each
define `RETIRED_STATUSES = {"superseded", "archived"}` and treat everything else
as current. Two answers to one question, in one repository.

## What the practice says

**Decision records.** The four-state minimum is proposed → accepted →
deprecated / superseded, and `accepted` is the state a decision carries while it
is *in force*; MADR's status line offers `proposed | rejected | accepted |
deprecated | … | superseded by ADR-0123`. So a rule that treats `accepted` as
not-current inverts the vocabulary it is reading.

**Retrieval.** Current knowledge-base guidance says to exclude superseded
versions, drafts, and unapproved content by metadata filter, and to archive or
flag by age rather than to allow-list one status word. Freshness is expressed as
a signal to filter on, not as a single blessed value.

Neither source suggests an allow-list of one word. Both describe a small set of
terminal states and treat everything else as live.

## What follows for this repository

The rule is stated as a closed list of **retired** words rather than a closed
list of current ones:

    RETIRED_STATUSES = {"superseded", "archived", "deprecated", "rejected"}

`archived` is this vault's own word (the lint and the index already use it);
`deprecated` and `rejected` come from the decision practice above and cost
nothing to honour before a page uses them. Everything else — `active`,
`accepted`, `preliminary`, `proposed`, absent — is current and retrievable.

The direction of the failure decided the shape. Under an allow-list, a status
word nobody anticipated makes the page **invisible**, and nothing reports it: the
page is on disk, in git, in the index, and absent from every answer. Under a
deny-list, an unanticipated word makes a retired page **findable**, which a
reader can see and correct. For a memory system the first failure is the worse
one, and it is the one that actually happened here.

One definition, imported by the lint, the index map, the collector and the query
filters, so the two answers cannot drift apart again. The SQL form is generated
from the same set rather than written twice.

## Sources

- [About MADR](https://adr.github.io/madr/) — status line vocabulary.
- [Architecture Decision Records (ADRs): The 2026 Guide, Catio](https://www.catio.tech/blog/architecture-decision-record) — proposed → accepted → deprecated / superseded as the four-state minimum, accepted meaning in force.
- [Architecture Decision Records: Templates and Operational Patterns, hidekazu-konishi.com](https://hidekazu-konishi.com/entry/architecture_decision_records_templates_and_operations.html) — deprecated means "no longer applies, no replacement".
- [Knowledge Base Engineering for AI Chatbots: The 2026 Practice, Heeya](https://heeya.fr/en/blog/knowledge-base-engineering-for-ai-chatbots-2026) — metadata filters exclude expired and superseded content; exclude drafts and superseded versions.
- [Optimizing Knowledge Bases for Effective RAG Pipelines, Unstructured](https://unstructured.io/insights/knowledge-base-optimization-for-enterprise-rag-pipelines) — freshness pipelines archive or flag by age rather than allow-list one status.
- [How Do RAG Systems Handle Outdated Information?, Am I Cited](https://www.amicited.com/faq/how-do-rag-systems-handle-outdated-information/) — stale index shows up as a drop in recall@K.
