# Every install brings every component

Date: 2026-09-29. Status: implemented in the same change.

## The owner's requirement

"должно всё входить + Pyright, все остальные языковые пакеты пользователем ставятся
отдельно под необходимый ему язык" (2026-09-29). An install brings every Python
component of the product; of the four managed language servers only Pyright comes
with it, and TypeScript, Go and Rust stay one explicit command each
(`scripts/install_language_server.py --profile <name>`), because each is large and
useful only to someone writing that language.

## What that meant before this change

`installer_config.DEFAULT_EXTRAS` was `("semantic",)`. Not installed unless asked:
`code-graph` (tree-sitter grammars for twelve languages and Jedi; without them the
code graph falls back to regex parsing outside Python), `reranker` (torch and
transformers for the cross-encoder `BAAI/bge-reranker-v2-m3`), and `hybrid` (the two
together). Both installers then printed three `uv sync --extra ...` lines under
"Optional". The extra `full` already names `semantic`, `hybrid`, `code-graph`,
`mcp-server` and `reranker`. The benchmark extras (`retrieval-benchmark`,
`lexical-benchmark`) are development tools, not components, and stay out.

## The cost that decided how torch is installed

PyPI serves torch for Linux built against CUDA: uv's PyTorch guide says PyPI "hosts
CPU-only wheels for Windows and macOS, and GPU-accelerated wheels on Linux (targeting
CUDA 13.0, as of PyTorch 2.11.0)", and `uv.lock` here resolves torch 2.13.0 with the
`nvidia-*` runtime wheels on x86_64 and aarch64 Linux (about 800 MB on top of torch,
per the note this default replaces). The reranker runs on the CPU with int8 dynamic
quantisation; it never uses a GPU. Downloading CUDA for it would be the largest part
of every Linux install and would buy nothing.

Sources, read today:
- uv, "Using uv with PyTorch" (https://docs.astral.sh/uv/guides/integration/pytorch/):
  define the PyTorch CPU index with `explicit = true`, so only packages that name it
  are resolved from it, and point `torch` at it in `[tool.uv.sources]`, optionally
  under a platform marker.
- PyTorch, "Get Started" (https://pytorch.org/get-started/locally/, last updated
  2026-07-27): the CPU-only build for Linux comes from
  `https://download.pytorch.org/whl/cpu`; Windows and macOS default builds are CPU.
- uv, indexes (https://docs.astral.sh/uv/concepts/indexes/): an explicit index is
  consulted only for the packages pinned to it, which keeps the second index from
  serving anything else (dependency-confusion defence).

## Decision

- `DEFAULT_EXTRAS = ("full",)`: every install and every nightly update brings all
  components; `install_models` then fetches both pinned models because both
  runtimes are present.
- `pyproject.toml`: an explicit index `pytorch-cpu`
  (`https://download.pytorch.org/whl/cpu`) and `torch` sourced from it on Linux
  only; Windows and macOS keep PyPI, whose builds are already CPU. `uv.lock` is
  regenerated with the pinned uv.
- The installers no longer print the three optional `uv sync` lines; they name
  the language-server command instead. The READMEs say the same.

## Trade-offs

- Disk and time: a full install adds torch (CPU) and transformers, and the
  reranker weights (about 2.2 GB in the Hugging Face cache on this machine), to the
  e5 encoder it already fetched. That is the price of "everything", accepted by the
  owner's requirement.
- A second index: `download.pytorch.org` is PyTorch's own and serves only `torch`
  because the index is explicit. If it is unreachable, a Linux install fails at the
  dependency step with uv's message, instead of silently taking a CUDA build.
- Alternatives: keep the reranker optional (rejected by the requirement); take
  torch from PyPI on Linux (rejected: CUDA for a CPU-only model); replace torch by
  an ONNX export of the reranker as the encoder already is
  (`docs/research/2026-09-25-the-encoder-runs-without-torch.md`) — smaller, but a
  model-format change with its own verification; recorded as the next step if the
  torch download proves a burden.
