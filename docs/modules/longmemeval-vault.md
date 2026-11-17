# Answer one LongMemEval question from a disposable llm-wiki vault (MEM-10)

`benchmark/longmemeval_vault.py` and the tests that exercise them most.

Run as a subprocess, one process per question, because the product resolves `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT` at import time: this module sets both to a throwaway directory *before* any `scripts/` import, so nothing here can touch the live vault.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
