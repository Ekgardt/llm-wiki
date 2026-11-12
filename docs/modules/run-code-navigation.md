# Measured qualification runner for the Python code-navigation facade

`benchmark/run_code_navigation.py` and the parts it was split into, each named by its concern:
- `benchmark/navigation_benchmark_deadlines.py`
- `benchmark/navigation_benchmark_environment.py`
- `benchmark/navigation_benchmark_gates.py`
- `benchmark/navigation_benchmark_git.py`
- `benchmark/navigation_benchmark_metrics.py`
- `benchmark/navigation_benchmark_mutation.py`
- `benchmark/navigation_benchmark_operator_corpus.py`
- `benchmark/navigation_benchmark_ownership.py`
- `benchmark/navigation_benchmark_queries.py`
- `benchmark/navigation_benchmark_schema.py`

The runner keeps the fixture run, the report and the command line; tests import the part
that defines a name and patch it where it is looked up.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.
