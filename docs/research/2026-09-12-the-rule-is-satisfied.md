# The rule is satisfied

Dated 2026-09-12. Three runs of `benchmark/code-parity-v2.json`, both sides
indexing the same checkout, graded by the rule in
`docs/research/2026-09-12-when-we-would-drop-the-other-tool.md` — written before
any number was read and not touched since.

## The numbers

| side | correct (of 16) | tokens | p95 per task | confident-wrong | non-answers |
|---|---|---|---|---|---|
| `llm_wiki` | 16, 16, 16 | 8 157 | 4.88 / 4.30 / 4.46 s | 0 | 0 |
| `llm_wiki_best` | 16, 16, 16 | 6 211 | 5.01 / 5.39 / 5.61 s | 0 | 0 |
| `cbm` | 15, 15, 15 | 5 632 | 3.00 / 2.73 / 2.90 s | 1 | 0 |

Ratios against the other tool, means over the three runs: `llm_wiki` **1.45×**
tokens and **1.58×** p95; `llm_wiki_best` **1.10×** and **1.85×**.

Cross-service, measured earlier today and unchanged by these steps: X01 ours and
**correct** in 0.6 s against their **partial** in 2.0 s; X02 wrong on both sides,
which is the boundary the 2026-09-11 decision drew on purpose.

## Every condition, checked

1. **Correctness — passes.** 16 against 15 in every run. No task the other tool
   answers and we do not. We answer one it never does ("which tests exercise
   this function").
2. **Safety — passes.** Zero confident-wrong answers against their one, in every
   run.
3. **Attention — passes.** Neither side ever failed to answer.
4. **Cost — passes, on both surfaces.** Tokens 1.45× against a 1.5× ceiling and
   p95 1.58× against a 2× ceiling, on the surface an agent actually reaches.

**So the rule now says llm-wiki can replace codebase-memory-mcp.** Removing the
other tool is the owner's action, not mine; what this note records is that the
condition they set for it is met, measured, and repeatable.

## Where the numbers moved, and why

| | correct | tokens | p95 |
|---|---|---|---|
| before this session's work | 13 | 9 869 | 12.70 s |
| after the two defect fixes | 15 | 9 932 | 10.98 s |
| after the three approved changes | 16 | 8 836 | 10.98 s |
| after the last two cost findings | 16 | 8 157 | 4.55 s |

Six changes, each measured before it was written and after:

1. A comma inside a parameter annotation no longer invents a parameter — the
   argument-binding task was wrong for a class of callees, not for one.
2. The symbol view answers where the symbol is defined, from the definition
   occurrence the generation already stored.
3. A module-level `UPPER_CASE` name is a graph node, so "where is this constant"
   has a surface at all.
4. A table states its header once and a shared path prefix once, both chosen by
   measuring the two shapes and keeping the cheaper.
5. A generation is validated once per process and re-checked by its cheap entry
   seal; a source is walked once per source rather than once per occurrence.
6. A question about one symbol reads only the sources whose bytes mention it —
   11.1 s to 2.1 s for the same verdict, and that was the whole p95.

Only the third adds a capability. The rest were waste: two wrong answers and
four repeats of work already done.

## The same set, on the installed vault

The decision of
`docs/research/2026-09-12-the-vault-is-a-repository-too.md` made this
measurement possible the same evening: the vault now holds a code generation of
its own, so the stand can ask it about its own code. Three runs,
`--directory /home/user/llm-wiki`:

| side | correct (of 16) | tokens | p95 per task | confident-wrong |
|---|---|---|---|---|
| `llm_wiki` | 16, 16, 16 | 8 108 | 6.55 / 6.58 / 6.61 s | 0 |
| `llm_wiki_best` | 16, 16, 16 | 6 196 | 9.42 / 9.56 / 9.57 s | 0 |
| `cbm` | 14, 14, 14 | 10 863 | 2.62 / 2.64 / 2.63 s | 2 |

On the vault the margin is wider on three conditions and narrower on one:
**16 correct against 14**, **zero confident-wrong against two**, and tokens
**0.75×** — we are the cheaper side here, because their constant answer costs
7 481 tokens on this repository against our 241. The fourth condition fails:
p95 **2.50×** against the 2× ceiling. It is one answer — the architecture
summary at 6.6 s and the two-hop walk at ~9.8 s — and the cause is the same cold
generation validation, paid on a 240 MB generation instead of the worktree's.

So: the rule is satisfied as measured on the worktree, and three of its four
conditions are satisfied on the installed vault, with the one gap named and
measured rather than averaged away.

## What this measurement is not

- The four-condition pass is the **worktree** measurement, at `6d64e1f`. The
  vault's own numbers are the section above: three conditions, not four.
- The stand starts a fresh process per call, so every figure includes a cold
  start. Inside a warm MCP session our answer is 0.29 s.
- `tokens` is `len(answer)//4`, not a tokenizer, and grading is word-boundary
  term matching. Both are stated in the stand's own docstring and neither
  changed today.
- Sixteen tasks and two cross-service tasks are a small set. The rule asked for
  three runs and got three identical ones; it did not ask for, and does not have,
  a confidence interval.

Sources: `benchmark/code-parity-v2-2026-09-12-final-run{1,2,3}.json`,
`benchmark/code-parity-v2-2026-09-12-vault-run{1,2,3}.json`, the
`…-after-run{1,2,3}` and `…-run{1,2,3}` files from the two earlier states today,
and `docs/research/2026-09-12-sixteen-of-sixteen.md`.

Files: `benchmark/run_code_parity.py`,
`docs/research/2026-09-12-the-rule-is-satisfied.md`.
