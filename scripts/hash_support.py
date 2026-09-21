"""Shared, deterministic test helpers -- no network, no randomness.

Use ``sha256_of`` in place of manually copied hex digests: it hashes the
exact bytes (or file) a test actually produced or fed in, via the standard
library only, never via the production function under test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_of(data: bytes | Path) -> str:
    """``sha256:<hex>`` of ``data``'s bytes, or of a file's bytes when given
    a ``Path``. Deterministic, offline, and independent of any production
    digest function -- a test asserting against this value is checking the
    production code's output, not echoing it back to itself."""
    payload = data.read_bytes() if isinstance(data, Path) else data
    return "sha256:" + hashlib.sha256(payload).hexdigest()
