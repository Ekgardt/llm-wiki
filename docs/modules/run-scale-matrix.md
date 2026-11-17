# Task 28: scale and failure matrix harness

`benchmark/run_scale_matrix.py` and the tests that exercise them most.

Deterministic offline smoke by default. Heavy corpora stay serial. Exact NumPy is the only default backend; ANN adapters never become default without measured adoption evidence (recall >= 0.98 and >= 2x p95 latency improvement).

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
