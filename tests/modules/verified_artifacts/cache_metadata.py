"""One timestamp setup shared by cache and graph/search integration tests."""
import os
import time
from pathlib import Path


def _aged(path: Path, seconds: float) -> os.stat_result:
    """Put the file's mtime in the past, as an artifact published earlier is."""
    when = time.time() - seconds
    os.utime(path, (when, when))
    return path.stat()
