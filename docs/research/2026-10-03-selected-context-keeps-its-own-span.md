# Selected context keeps its own span

Research and reproduction date: 2026-10-03. Python compatibility remains 3.10.
No model, provider, runtime directory, MCP tool or environment contract changes.

The context compiler implicitly expanded each selected chunk to its whole page
when the page was below 1,500 bytes. Its separate 2,000-byte heading expansion
and 200-byte neighbor cutoff had no measured basis. Several chunks from the same
small page consequently repeated that page. When mandatory evidence exceeded the
shared budget, get_context retried without evidence. Its L1 overview can still
contain facts, so absence of L2 is not proof that every fact disappeared.

Three red regressions use real temporary Markdown and the real corpus collector:
all selected chunks lose their L2 spans under the budget that holds those spans;
a selected fragment becomes a whole small page; and an explicitly permitted
neighbor above 200 bytes is omitted despite fitting the caller's expansion bound.

The default now keeps the selected chunk's exact verified span, matching the
existing query_memory reading policy. Whole-page and heading expansion remain
explicit caller choices. A following section is included whole when it fits
that explicit expansion budget; no extra 200-byte cutoff is imposed. The existing
legacy `_chars` argument names continue to bound UTF-8 source bytes. Final output
still obeys the shared context budget. The duplicate private default budget is
removed in favor of DEFAULT_CONTEXT_BUDGET. The unused internal compiler-version
constant and the unsupported cache claim in the module introduction are removed;
source-hash item identities remain. This is not a product release or version bump.

Paired actual stdio MCP runs use identical 1,290-byte synthetic Markdown with four
fact paragraphs. With a 4,096-token requested budget, the baseline returns L0/L1
and no L2; the candidate returns all five selected L2 spans. Both contain all four
facts, so this measures evidence representation, not an improvement in answer
accuracy. With the same 10,000-token budget, both supply five L2 items and all four
facts: context text falls from 8,312 to 3,130 UTF-8 bytes, reported estimated context
tokens from 4,156 to 1,565, and the response JSON from 10,423 to 5,157 bytes.
Full cycles including initialization and process closure are 4.482s and 4.377s;
one pair does not establish a latency improvement. No generative model is used
by these get_context checks. This is output-context economy at equal fixture
coverage, not measured end-to-end model token savings or complete audit closure.

Alternatives considered: increasing the overall budget retains repetition;
retuning the three thresholds introduces new unmeasured numbers; disabling all
expansion removes an existing explicit capability; trained compression adds
models and provenance work without being needed to eliminate this duplication.
Exact default spans with explicit expansion retain the existing capability and
use the existing single budget. Narrower default surrounding context can matter
for some questions, so whole-cycle answer qualification remains separate.

Independent primary sources checked on the research date:

- [LongMemEval, v2, ICLR 2025](https://arxiv.org/abs/2410.10813v2)
  separates indexing, retrieval and reading and evaluates downstream answers.
  It supports keeping retrieval coverage separate from answer quality.
- [RECOMP, v1, ICLR 2024](https://arxiv.org/abs/2310.04408)
  evaluates query-aware compression and selective augmentation. Its trained
  compression result does not justify a fixed character cutoff in this product.
- [Lost in the Middle, TACL 2024](https://aclanthology.org/2024.tacl-1.9/)
  finds that added context can hurt performance depending on position and task.
  Its tested models differ from the installed providers; local paired evidence,
  rather than its numerical results, supports this particular repair.

These studies do not establish universal best thresholds for current models.
The fixture and paired MCP artifacts are retained in the private audit logs;
qualification and installed verification are recorded there separately.
