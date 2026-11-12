"""Helpers shared by test_historical_test_protocols_keep_their_exact_utf8_bytes.py and the test files importing them."""




def _windows_text_write(path, data, encoding=None, errors=None, newline=None):
    ending = {None: "\r\n"}.get(newline, newline)
    with path.open("w", encoding=encoding or "utf-8", errors=errors, newline=ending) as output:
        return output.write(data)
