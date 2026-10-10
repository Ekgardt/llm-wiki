# Raw row positions belong to one layout

Checked 2026-10-07. Optional-context preparation still measures the complete request for every prospective page. Whole-prompt DLP and token counters are not assumed additive, and optional context can change protected aliases and model-visible row numbers.

A smaller mechanical repetition existed inside each layout: every selected line decoded the same complete raw UTF8 source and counted LF bytes in its prefix. A production-layout regression measured 31 repeated decodes for a 30-line neutral source. The replacement derives character-row starts, byte-row starts and visible LF ordinals once per exact retained byte object inside the existing layout scope. It retains strong byte ownership and resets the scope in finally. It stores no evidence-binding, policy, filesystem or publication verdict.

CRLF, blank lines, UTF8, absent final LF and every selected physical span retain their positions. Existing projection, companion, hash, ambiguity and evidence checks still run. Protected transport remains fresh for each different context; the complete appended prompt and dispatch retain their fresh scans. Public unscoped calls keep their existing behavior.

Python 3.10 qualification includes complete prompt/schema/binding equality, all selected optional pages, multipart gaps, native containers, protected aliases, invalid policy and exception/reset controls. On a neutral 750-row fixture with 12 optional pages, complete packing plus layout/binding verification used 1.358 CPU seconds with the old position logic and 1.058 with the index. The fixture ran on a shared host. This supports the mechanical experiment, not a whole-day speed or token-quality claim. Full repeated request rendering remains.

Primary references checked on the date above:

* [Python 3.10 Context Variables](https://docs.python.org/3.10/library/contextvars.html): context-local state and token reset provide scoped ownership and restoration.
* [OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html): external content remains untrusted and validation boundaries must remain active.
* [W3C PROV-DM](https://www.w3.org/TR/prov-dm/): a derived view of a specific version does not replace source authority.

Cross-context DLP/binding memoization, additive optional-page accounting, arbitrary offer caps and raised deadlines were rejected. The experiment changes no persistent format, setting, source identity, model choice or capacity.

Installed qualification: the combined capacity and position change passed 177 related tests on actual Python 3.10, with no skips. An ordinary CLI dry run completed all 16 still-pending parts of a 1,241,829-byte daily source, preserved its hash, and accepted two source-bound historical pages through four Codex gpt-6-luna/max calls. The complete measured cycle used 1,493.884 wall seconds, 1,271.844 process CPU seconds, 251,788 input and 27,140 output tokens. Cache-read tokens are already included in input. Cost is unknown. This establishes completion of that dry run; it establishes neither durable publication nor a paired whole-cycle token improvement. Earlier interrupted trials and unknown usage remain recorded privately.

The unscoped prefix conversion remains necessary for existing standalone source-choice and prompt calls, whose character offsets are not restricted to raw LF row starts. Its removal requires qualification of a separate raw-only scope without extending reuse of protected bases or binding decisions. Legacy cleanup is therefore incomplete. Within the existing layout, absent raw row starts fail closed. This compatibility path is not used as a substitute for the scoped index.
