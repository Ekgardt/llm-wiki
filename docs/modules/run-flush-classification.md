# Measure what session classification keeps and what it drops

`benchmark/run_flush_classification.py` and the tests that exercise them most.

The audit asked how often a session that held a decision, a fix, or a gotcha ends up without it in durable memory. That number was unknown: the only tests covered response parsing. This runner scores the whole classification step — the product's own prompt, one provider, the product's parser — against a labelled corpus, and gates on three numbers.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
