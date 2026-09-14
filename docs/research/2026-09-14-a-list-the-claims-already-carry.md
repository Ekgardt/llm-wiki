# A list the claims already carry

Dated 2026-09-14. After the invalid-JSON rows
(`docs/research/2026-09-14-the-document-after-the-notes.md`), five more stand
rows ended as `verification_or_gate` errors rather than answers or abstentions.
This is the research before the fix.

## What the rows say

Read from the stored `raw_reply` of `lme500.jsonl` and `lme500-hybrid.jsonl`:

- **Four abstentions refused for their citations.** `a2f3aa27`, `118b2229`,
  `e56a43b9` (`conflicting_evidence`) and `gpt4_18c2b244`
  (`insufficient_evidence`): each has a stated reason and **zero claims**, and
  cites 2 to 5 spans — the spans that conflict, or the ones that cover only part
  of the question. `_require_abstention_shape` refuses when `claims` *or*
  `citations` is non-empty, and the whole reply became
  `GroundedQAError: abstention statuses require a reason and no factual claims`.
  The error names claims; the rows had none.
- **One answer refused for a list it could derive.** `1da05512`: status
  `answered`, five claims, every one with `citation_ids`, and no top-level
  `citations` field. Schema validation refused it —
  `$: missing required properties ['citations']` — before any gate looked at a
  claim. The schema's own description of a citation says "The model names the
  evidence by citation_id; every other field is filled from the manifest this
  process built", and `_verified_citations` does exactly that. The list is the
  set of ids the claims already carry.

## Practice on this date, and this file's own history

- Claim-level verification — a claim is kept or dropped on its own evidence — is
  the attribution practice this file adopted on 2026-09-02; answer-level
  rejection over something beside the claims is what it stopped doing
  (`verify_grounded_answer`, `_require_answered_shape`: "refusing the whole
  answer over something beside it").
- Grounded-generation evaluation scores the claims a response makes against their
  cited sources; a response with no claims asserts nothing to ground
  ([measuring RAG groundedness](https://www.openlayer.com/blog/measuring-rag-groundedness-complete-evaluation-guide),
  [grounded language model](https://futureagi.com/glossary/grounded-language-model/)).
  An abstention's citations are its reasoning, not assertions.
- Robust handling of model JSON derives what is derivable and validates the rest,
  rather than refusing a document over a redundant field
  ([robust JSON extraction for LLM responses](https://github.com/OpenMind/OM1/issues/1700)).

## The decision

1. **An abstention is refused only for what makes it not an abstention:** a claim,
   or no stated reason. Citations beside it ground nothing and are dropped, so the
   abstention reaches the caller exactly as a claimless, citationless one always
   has — no downstream reader ever saw an abstention with citations, and none
   will. They are not verified first: a list that is discarded cannot fail.
2. **An answer without a `citations` list gets the list its claims carry**, one
   `{"citation_id": …}` per distinct id in claim order, before schema validation;
   `_verified_citations` fills the rest from the manifest as for every answer, and
   every claim still passes its gates. A document that has a `citations` list is
   untouched, so a list that disagrees with the claims is still caught where it
   was.

Why not the alternatives:

- **Keep an abstention's citations.** It would put a new shape in front of every
  consumer (MCP rendering, the refusal pass, the stand) for no measured gain.
- **Make `citations` optional in the schema.** The verifier would then need a
  second path for a missing list; deriving it keeps one path.
- **Leave both to constrained output.** That is the next step for the Claude
  backend only, and prompt-only providers would keep losing these replies.

Expected effect, stated as a bound: the four rows become abstentions and the one
answer reaches the citation gates. Whether the answer is kept and judged correct
is not measured here.

Files: `scripts/query_memory.py`, `tests/test_a_list_the_claims_already_carry.py`,
`docs/research/2026-09-14-a-list-the-claims-already-carry.md`.
