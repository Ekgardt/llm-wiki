# Reviewed correction cannot accept an old source cache

Date: 2026-10-09.

A source plan cached before current critic feedback could bypass the correction path. A causal reproduction stored an accepted empty plan or an accepted reviewed plan, then attached newly validated rejection feedback. `_cached` returned the previous plan without considering that feedback; an empty cache result could therefore erase known unresolved source work before the empty-rewrite guard ran.

An attempt with critic feedback now skips the old source-plan cache and reaches normal drafting, evidence binding, critique and publication. Attempts without feedback retain the existing cache path. Cache records are not deleted, converted into authority or rewritten merely by refusal. Three causal tests failed before the guard and pass after it, including exact feedback and existing-cache preservation. No schema, action-key format, environment contract, model, storage path or attempt count is changed.

Research checked 2026-10-09: [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching), [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) and the original [Self-Refine paper](https://arxiv.org/abs/2303.17651). Their provider prefix caches are different from this repository's semantic-plan cache; they do not establish its authority. The relevant principle is retaining the actual changed input and validated feedback when generating a correction. The decisive evidence is the local source-plan reproduction and the existing no-empty-rewrite contract.

Adding a new persistent correction cache or expanding existing cache identity/schema was unnecessary. Revalidating current correction can cost another call; that cost belongs to whole-cycle accounting and cannot be hidden by accepting an old plan. Full source and context, complete feedback, original validation and normal caching for unchanged input remain intact.

Source: `tests/test_compile_repair_feedback_cannot_use_an_old_cache.py`; unchanged critic/source-work and compiler cache guards; private `step7-reviewed-repair-cache-red-20261009.log` and related qualification reports.
