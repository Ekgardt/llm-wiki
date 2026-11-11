"""Helpers shared by test_compile_streamed_capture_validation.py and the test files importing them."""

import io
import zipfile


def _packed(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return output
