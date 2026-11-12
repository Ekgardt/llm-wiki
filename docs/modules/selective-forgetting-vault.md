# Measure selective forgetting on a throwaway copy of this vault (MEM-12)

`benchmark/selective_forgetting_vault.py` and the tests that exercise them most.

Memory Agent Bench scores four competencies; this stand covers the fourth, *selective forgetting*, as a property rather than a claim: given material that should be forgotten and material that must not be, does a query still surface the forgotten item, does the retained item survive, and does the reverse transition return exactly what it took away.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
