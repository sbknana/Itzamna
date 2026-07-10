#!/usr/bin/env python3
"""
Test suite for feature flags in forge_orchestrator.py.

Tests verify:
1. is_feature_enabled returns default when dispatch_config is None
2. is_feature_enabled reads from config features dict
3. is_feature_enabled falls back to defaults for missing keys
4. Unknown feature names default to False
5. load_dispatch_config deep-merges features (partial override preserves other flags)
6. load_dispatch_config with no features key uses all defaults
7. DEFAULT_FEATURE_FLAGS contains all 12 expected flags
8. dispatch_config.example.json features section matches DEFAULT_FEATURE_FLAGS
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path so we can import forge_orchestrator
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_orchestrator import (
    DEFAULT_FEATURE_FLAGS,
    is_feature_enabled,
    load_dispatch_config,
)


class TestIsFeatureEnabled(unittest.TestCase):
    """Tests for is_feature_enabled()."""

    def test_none_config_returns_default_true(self):
        """When dispatch_config is None, returns the default flag value."""
        # forgesmith_lessons defaults to True
        self.assertTrue(is_feature_enabled(None, "forgesmith_lessons"))

    def test_none_config_returns_default_false(self):
        """When dispatch_config is None, returns False for disabled defaults."""
        # hooks defaults to False
        self.assertFalse(is_feature_enabled(None, "hooks"))

    def test_reads_from_config_features(self):
        """Reads feature value from config['features'] dict."""
        config = {"features": {"hooks": True}}
        self.assertTrue(is_feature_enabled(config, "hooks"))

    def test_falls_back_to_default_for_missing_feature(self):
        """If feature not in config['features'], falls back to DEFAULT_FEATURE_FLAGS."""
        config = {"features": {"hooks": True}}
        # forgesmith_lessons not in config, should fall back to default (True)
        self.assertTrue(is_feature_enabled(config, "forgesmith_lessons"))

    def test_unknown_feature_defaults_false(self):
        """Unknown feature names not in defaults return False."""
        self.assertFalse(is_feature_enabled(None, "nonexistent_feature"))
        config = {"features": {}}
        self.assertFalse(is_feature_enabled(config, "nonexistent_feature"))

    def test_empty_features_dict_uses_defaults(self):
        """Config with empty features dict falls back to defaults."""
        config = {"features": {}}
        self.assertTrue(is_feature_enabled(config, "security_review"))
        self.assertFalse(is_feature_enabled(config, "mcp_health"))

    def test_config_without_features_key_uses_defaults(self):
        """Config dict without 'features' key falls back to defaults."""
        config = {"max_concurrent": 4}
        self.assertTrue(is_feature_enabled(config, "quality_scoring"))


class TestDefaultFeatureFlags(unittest.TestCase):
    """Tests for DEFAULT_FEATURE_FLAGS constant."""

    EXPECTED_FLAGS = {
        "language_prompts": True,
        "hooks": False,
        "mcp_health": False,
        "forgesmith_lessons": True,
        "forgesmith_episodes": True,
        "gepa_ab_testing": False,
        "security_review": True,
        "bash_security_pretooluse": False,
        "security_review_block_on_missing_artifact": True,
        "quality_scoring": True,
        "anti_compaction_state": True,
        "vector_memory": False,
        "auto_model_routing": False,
        "knowledge_graph": False,
        "autoresearch": True,
        "rlm_decompose": False,
        "config_versioning": False,
        "session_persistence": False,
        "project_templates": False,
    }

    def test_contains_all_expected_flags(self):
        """DEFAULT_FEATURE_FLAGS has exactly the expected flags."""
        self.assertEqual(set(DEFAULT_FEATURE_FLAGS.keys()),
                         set(self.EXPECTED_FLAGS.keys()))

    def test_flag_values_match(self):
        """Each flag has the expected default value."""
        for flag, expected in self.EXPECTED_FLAGS.items():
            self.assertEqual(DEFAULT_FEATURE_FLAGS[flag], expected,
                             f"Flag '{flag}' expected {expected}, "
                             f"got {DEFAULT_FEATURE_FLAGS[flag]}")


class TestLoadDispatchConfigDeepMerge(unittest.TestCase):
    """Tests for deep merge of features in load_dispatch_config()."""

    def test_partial_features_preserves_other_defaults(self):
        """Specifying only one feature flag preserves all other defaults."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({"features": {"hooks": True}}, f)
            f.flush()
            try:
                config = load_dispatch_config(f.name)
                features = config["features"]
                # hooks overridden to True
                self.assertTrue(features["hooks"])
                # All other flags preserved from defaults
                self.assertTrue(features["forgesmith_lessons"])
                self.assertTrue(features["security_review"])
                self.assertFalse(features["mcp_health"])
                self.assertFalse(features["gepa_ab_testing"])
            finally:
                os.unlink(f.name)

    def test_no_features_key_uses_all_defaults(self):
        """Config file without features key gets full default features."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({"max_concurrent": 2}, f)
            f.flush()
            try:
                config = load_dispatch_config(f.name)
                self.assertEqual(config["features"],
                                 dict(DEFAULT_FEATURE_FLAGS))
            finally:
                os.unlink(f.name)

    def test_full_features_override(self):
        """Specifying all features replaces all values."""
        all_false = {k: False for k in DEFAULT_FEATURE_FLAGS}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({"features": all_false}, f)
            f.flush()
            try:
                config = load_dispatch_config(f.name)
                for flag in DEFAULT_FEATURE_FLAGS:
                    self.assertFalse(config["features"][flag],
                                     f"Flag '{flag}' should be False")
            finally:
                os.unlink(f.name)

    def test_missing_file_returns_defaults(self):
        """Non-existent config file returns all defaults including features."""
        config = load_dispatch_config("/tmp/nonexistent_config_12345.json")
        self.assertIn("features", config)
        self.assertEqual(config["features"], dict(DEFAULT_FEATURE_FLAGS))


class TestExampleConfigMatchesDefaults(unittest.TestCase):
    """Verify dispatch_config.example.json features match DEFAULT_FEATURE_FLAGS."""

    def test_example_features_match_code_defaults(self):
        """dispatch_config.example.json features section matches DEFAULT_FEATURE_FLAGS."""
        example_path = (Path(__file__).resolve().parent.parent /
                        "dispatch_config.example.json")
        if not example_path.exists():
            self.skipTest("dispatch_config.example.json not found")

        with open(example_path, encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("features", data,
                       "dispatch_config.example.json must have a 'features' key")

        example_features = data["features"]
        for flag, default_val in DEFAULT_FEATURE_FLAGS.items():
            self.assertIn(flag, example_features,
                          f"Example config missing flag '{flag}'")
            self.assertEqual(example_features[flag], default_val,
                             f"Flag '{flag}' in example ({example_features[flag]}) "
                             f"differs from code default ({default_val})")


if __name__ == "__main__":
    unittest.main()
