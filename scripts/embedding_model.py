"""The one place that names the embedding model.

The search runtime and the generation builder encode text with it, and each
used to carry its own copy of the name. A copy that drifts is worse than no copy
at all here: queries embedded by one model and pages by another still produce
numbers, and the numbers are meaningless.

The model is multilingual on purpose: the pages of this vault are written in
English and Russian, and its owner asks in both. Measured on 996 questions across
the four language crossings, with the reranker below and fusion weights 1:2,
`BAAI/bge-m3` put the answer first on 52.8 % against 37.9 % for
`intfloat/multilingual-e5-small`, and a Russian question over English pages went
from 4 % to 46 % (owner message 17933).
"""
from __future__ import annotations

EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
# The ONNX export the repository ships at that revision: the graph, and beside it
# the weights it names as external data, which `scripts/install_models.py`
# verifies after a download.
EMBEDDING_GRAPH_FILE = "onnx/model.onnx"
EMBEDDING_WEIGHTS_FILE = "onnx/model.onnx_data"
EMBEDDING_WEIGHTS_SHA256 = "1eebfb28493f67bba03ce0ef64bfdc7fc5a3bd9d7493f818bb1d78cd798416b4"
EMBEDDING_WEIGHTS_BYTES = 2266820608
EMBEDDING_TOKENIZER_FILE = "tokenizer.json"
# Everything the encoder reads, and so everything the installer fetches.
EMBEDDING_READ_FILES = (EMBEDDING_GRAPH_FILE, EMBEDDING_WEIGHTS_FILE, EMBEDDING_TOKENIZER_FILE)
EMBEDDING_DIM = 1024
