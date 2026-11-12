"""The original shared source-build declaration factory."""
from pathlib import Path

from lsp_server_profile import SourceBuild


def _build(**overrides) -> SourceBuild:
    fields = {
        "module": "golang.org/x/tools/gopls",
        "version": "v0.23.0",
        "toolchain_relative": Path("go/bin/go"),
        "binary_relative": Path("bin/gopls"),
    }
    fields.update(overrides)
    return SourceBuild(**fields)
