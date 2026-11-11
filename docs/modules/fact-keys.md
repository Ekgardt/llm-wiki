# Fact keys beside the turn: short user facts that point at the turn they came from

`scripts/fact_keys.py` and the tests that exercise them most.

LongMemEval's key expansion (arXiv:2410.10813, CP2): index each round under the facts the user stated in it as well as under its text — +9.4% recall, +5.4% accuracy. Dense X Retrieval (arXiv:2312.06648): a short self-contained statement is the unit that is found best. And what is found must not be what is read: a fact a model wrote is never a citation, so the key points at the user's turn, and the turn is what the reader gets.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
