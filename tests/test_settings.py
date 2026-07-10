# tests/test_settings.py

import copy
import json

from audible_downloader.settings import DEFAULT_SETTINGS, deep_update, load_settings


class TestDeepUpdate:
    def test_nested_merge_preserves_sibling_keys(self):
        source = {"a": {"x": 1, "y": 2}, "b": 3}
        deep_update(source, {"a": {"y": 20}})
        assert source == {"a": {"x": 1, "y": 20}, "b": 3}

    def test_adds_keys_missing_from_source(self):
        source = {"a": 1}
        deep_update(source, {"b": {"c": 2}})
        assert source == {"a": 1, "b": {"c": 2}}

    def test_scalar_override_replaces_value(self):
        source = {"a": {"x": 1}, "b": 3}
        deep_update(source, {"b": 30})
        assert source["b"] == 30

    def test_returns_the_source_dict(self):
        source = {"a": 1}
        assert deep_update(source, {"a": 2}) is source


class TestLoadSettings:
    def test_missing_file_returns_defaults(self, settings_file):
        assert load_settings() == DEFAULT_SETTINGS

    def test_returns_independent_deep_copies_without_file(self, settings_file):
        """H2 regression (no-file return path): the returned dict must share
        nothing with DEFAULT_SETTINGS."""
        first = load_settings()
        second = load_settings()
        assert first == second
        assert first is not second
        assert first is not DEFAULT_SETTINGS
        # The nested dicts are the part the old shallow copy got wrong.
        assert first["job"] is not DEFAULT_SETTINGS["job"]
        assert first["tasks"] is not second["tasks"]

    def test_returns_independent_deep_copies_with_file(self, settings_file):
        """H2 regression (file-exists return path): same guarantee when a
        settings.json is actually loaded and merged over the defaults."""
        settings_file.write_text(json.dumps({"username": "bob"}))
        loaded = load_settings()
        assert loaded is not DEFAULT_SETTINGS
        assert loaded["job"] is not DEFAULT_SETTINGS["job"]
        assert loaded["tasks"] is not DEFAULT_SETTINGS["tasks"]
        assert loaded["job"]["download"] is not DEFAULT_SETTINGS["job"]["download"]

    def test_mutating_loaded_settings_never_corrupts_defaults(self, settings_file):
        """H2 regression: simulate what the settings API does — merge request
        data into a loaded settings dict, on both return paths."""
        baseline = copy.deepcopy(DEFAULT_SETTINGS)

        # Path 1: no settings.json yet (fresh install).
        loaded = load_settings()
        deep_update(loaded, {"naming": {"template": "corrupted"}, "job": {"download": {"total_processing_cores": 99}}})
        loaded["conversion"]["quality"] = "corrupted"
        assert DEFAULT_SETTINGS == baseline

        # Path 2: settings.json exists (every later save goes through here).
        settings_file.write_text(json.dumps({"username": "bob"}))
        loaded = load_settings()
        deep_update(loaded, {"naming": {"template": "corrupted"}, "job": {"download": {"total_processing_cores": 99}}})
        loaded["conversion"]["quality"] = "corrupted"
        assert DEFAULT_SETTINGS == baseline

    def test_defaults_survive_a_partial_settings_file(self, settings_file):
        """Users with old settings.json files must still get defaults for new keys."""
        settings_file.write_text(json.dumps({"username": "bob", "naming": {"template": "{title}"}}))
        settings = load_settings()
        # Values from the file win...
        assert settings["username"] == "bob"
        assert settings["naming"]["template"] == "{title}"
        # ...while everything the file doesn't mention keeps its default.
        assert settings["conversion"]["quality"] == "High"
        assert settings["tasks"]["timezone"] == "UTC"
        assert settings["job"]["download"]["max_parallel_downloads"] == 2

    def test_corrupt_json_falls_back_to_defaults(self, settings_file):
        settings_file.write_text("{this is not json")
        assert load_settings() == DEFAULT_SETTINGS
