"""Regression: skill-manifest hashing must be line-ending independent.

`tasks`-style integrity manifests pin LF-content hashes (the .md files are
stored LF in git). The hashing used raw `read_bytes()`, so on a checkout with
`git autocrlf=true` the working-tree files are CRLF and every file's hash
mismatched the LF manifest — `verify_skill_integrity()` failed on Windows while
passing on Linux/CI. `_hash_md_bytes` normalizes CRLF/CR -> LF before hashing.

These pin that the normalization (a) makes CRLF and LF content hash identically
and (b) leaves an LF file's hash exactly where it was, so the existing committed
LF manifest stays valid.
"""

from __future__ import annotations

import hashlib

from equipa.security import _hash_md_bytes


def test_crlf_and_lf_hash_identically() -> None:
    lf = b"# Title\n\nbody line one\nbody line two\n"
    crlf = lf.replace(b"\n", b"\r\n")
    cr = lf.replace(b"\n", b"\r")  # classic-Mac lone CR
    assert _hash_md_bytes(crlf) == _hash_md_bytes(lf)
    assert _hash_md_bytes(cr) == _hash_md_bytes(lf)


def test_lf_hash_is_unchanged_from_raw_sha256() -> None:
    """An LF file's normalized hash equals its plain sha256, so the existing
    LF-content manifest does not need regenerating."""
    lf = b"line a\nline b\n"
    assert _hash_md_bytes(lf) == hashlib.sha256(lf).hexdigest()


def test_no_trailing_newline_is_stable() -> None:
    assert _hash_md_bytes(b"no newline") == hashlib.sha256(b"no newline").hexdigest()
