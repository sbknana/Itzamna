#!/usr/bin/env python3
"""Tests for skill hash verification (verify_skill_integrity, generate_skill_manifest).

Covers: manifest generation, integrity pass, tampered file detection,
missing file detection, missing manifest fallback, empty manifest rejection,
and corrupt JSON handling.

Copyright 2026 Forgeborn
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add parent directory for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from forge_orchestrator import (
    generate_skill_manifest,
    verify_skill_integrity,
    write_skill_manifest,
    SKILL_MANIFEST_FILE,
)


# ---------------------------------------------------------------------------
# generate_skill_manifest
# ---------------------------------------------------------------------------

def test_generate_manifest_returns_dict():
    """generate_skill_manifest should return a non-empty dict of {path: hash}."""
    manifest = generate_skill_manifest()
    assert isinstance(manifest, dict)
    assert len(manifest) > 0, "Manifest should contain at least one file"


def test_generate_manifest_hashes_are_valid_sha256():
    """Every hash in the manifest must be a 64-char lowercase hex string."""
    manifest = generate_skill_manifest()
    for rel_path, file_hash in manifest.items():
        assert len(file_hash) == 64, f"Hash for {rel_path} is not 64 chars: {file_hash}"
        assert all(c in "0123456789abcdef" for c in file_hash), (
            f"Hash for {rel_path} contains non-hex chars: {file_hash}"
        )


def test_generate_manifest_includes_prompts_and_skills():
    """Manifest should include files from both prompts/ and skills/ directories."""
    manifest = generate_skill_manifest()
    has_prompts = any(k.startswith("prompts/") for k in manifest)
    has_skills = any(k.startswith("skills/") for k in manifest)
    assert has_prompts, "Manifest should include files from prompts/"
    assert has_skills, "Manifest should include files from skills/"


def test_generate_manifest_hash_matches_file_content():
    """Spot-check: the manifest hash for a known file matches its SHA-256 over
    LF-normalized content. Hashing is line-ending independent (see
    equipa.security._hash_md_bytes), so the expected value is computed over
    CRLF/CR -> LF normalized bytes — otherwise this would fail on a checkout
    with git autocrlf=true (CRLF working tree vs LF manifest)."""
    manifest = generate_skill_manifest()
    # Pick the first file and verify
    rel_path = next(iter(manifest))
    expected_hash = manifest[rel_path]
    file_path = REPO_ROOT / rel_path
    normalized = file_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    actual_hash = hashlib.sha256(normalized).hexdigest()
    assert actual_hash == expected_hash, (
        f"Hash mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}"
    )


# ---------------------------------------------------------------------------
# write_skill_manifest
# ---------------------------------------------------------------------------

def test_write_skill_manifest_creates_valid_json():
    """write_skill_manifest should produce a valid JSON file with expected keys."""
    # Use the real function (writes to the actual manifest path)
    result = write_skill_manifest()
    assert "version" in result
    assert result["version"] == 1
    assert "generated_at" in result
    assert "files" in result
    assert len(result["files"]) > 0

    # Verify the file on disk is valid JSON
    data = json.loads(SKILL_MANIFEST_FILE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["files"]) == len(result["files"])


# ---------------------------------------------------------------------------
# verify_skill_integrity
# ---------------------------------------------------------------------------

def test_verify_passes_with_current_manifest():
    """With a freshly generated manifest, verification should pass."""
    write_skill_manifest()
    assert verify_skill_integrity() is True


def test_verify_returns_true_when_manifest_missing():
    """If manifest file doesn't exist, verification should pass (backward compat)."""
    with patch("forge_orchestrator.SKILL_MANIFEST_FILE", Path("/nonexistent/skill_manifest.json")):
        assert verify_skill_integrity() is True


def test_verify_fails_on_tampered_file():
    """If a file hash doesn't match, verification should fail."""
    write_skill_manifest()

    # Read the manifest, corrupt one hash
    data = json.loads(SKILL_MANIFEST_FILE.read_text(encoding="utf-8"))
    first_key = next(iter(data["files"]))
    data["files"][first_key] = "0" * 64  # fake hash

    SKILL_MANIFEST_FILE.write_text(json.dumps(data), encoding="utf-8")

    assert verify_skill_integrity() is False

    # Restore valid manifest
    write_skill_manifest()


def test_verify_fails_on_missing_file():
    """If the manifest references a file that doesn't exist, verification should fail."""
    write_skill_manifest()

    data = json.loads(SKILL_MANIFEST_FILE.read_text(encoding="utf-8"))
    data["files"]["prompts/nonexistent_file_12345.md"] = "a" * 64

    SKILL_MANIFEST_FILE.write_text(json.dumps(data), encoding="utf-8")

    assert verify_skill_integrity() is False

    # Restore valid manifest
    write_skill_manifest()


def test_verify_fails_on_empty_files_dict():
    """An empty 'files' dict in the manifest should be rejected."""
    data = {"version": 1, "generated_at": "2026-01-01T00:00:00Z", "files": {}}
    SKILL_MANIFEST_FILE.write_text(json.dumps(data), encoding="utf-8")

    assert verify_skill_integrity() is False

    # Restore valid manifest
    write_skill_manifest()


def test_verify_fails_on_corrupt_json():
    """Corrupt JSON in the manifest should cause verification to fail."""
    original = SKILL_MANIFEST_FILE.read_text(encoding="utf-8")

    SKILL_MANIFEST_FILE.write_text("{invalid json!!!", encoding="utf-8")
    assert verify_skill_integrity() is False

    # Restore
    SKILL_MANIFEST_FILE.write_text(original, encoding="utf-8")
