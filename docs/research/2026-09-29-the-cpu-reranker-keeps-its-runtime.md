# The CPU reranker keeps its runtime

Research checked on 2026-09-29. The existing reranker loads its model on CPU
and applies dynamic int8 quantization. A locked hybrid sync previously selected
the PyPI Linux CUDA distribution even when a compatible CPU torch was installed.
The extra and the lock now select the CPU index on Linux and Windows; macOS
keeps its PyPI build. Torch remains at the already locked 2.13.0 version.

Three independent primary sources establish the choice:

- [uv's PyTorch integration](https://docs.astral.sh/uv/guides/integration/pytorch/)
  documents accelerator-specific indexes, extra/platform source selection and
  explicit indexes that cannot supply unrelated dependencies.
- [PyTorch's installation guide](https://pytorch.org/get-started/locally/)
  distinguishes CPU and accelerator installations by the execution environment.
- [The PyPA project specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
  separates optional dependency contracts from tool-specific configuration.

Alternatives were automatic host detection, accepting CUDA dependencies, and
replacing the inference engine. Automatic selection would change the locked
environment with the host; CUDA adds unused packages to this CPU execution
path; an engine replacement has a larger compatibility and model-validation
cost. An explicit source is the smallest change matching the existing runtime.
It relies on uv's source extension, supported by the required uv 0.12.3. A
future GPU reranker must revise this contract and qualify its own environment.

The updated lock removes the unused CUDA dependency graph. A locked hybrid
dry run against the existing CPU environment resolves 129 packages, checks 58,
and makes no changes. The original lock attempted to replace CPU torch and
install NVIDIA packages. Platform CI remains required before release.
