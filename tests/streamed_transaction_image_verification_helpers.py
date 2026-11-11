"""Helpers shared by test_streamed_transaction_image_verification.py and the test files importing them."""
from __future__ import annotations

import lzma

from llm_wiki import markdown_prepare


def _encoded(payload, format):
    if format == 'plain':
        return payload
    if format == 'xz':
        return lzma.compress(payload, preset=markdown_prepare.IMAGE_COMPRESSION_PRESET)
    middle = len(payload) // 2
    return lzma.compress(payload[:middle], preset=markdown_prepare.IMAGE_COMPRESSION_PRESET) + lzma.compress(payload[middle:], preset=markdown_prepare.IMAGE_COMPRESSION_PRESET)


def _image(root, raw):
    path = root / 'before/000000.bin'
    path.parent.mkdir()
    path.write_bytes(raw)
    return path
