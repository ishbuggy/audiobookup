# tests/test_settings.py

import copy
import json

from audible_downloader.settings import (
    DEFAULT_SETTINGS,
    deep_update,
    load_settings,
    resolve_output_format,
)


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


class TestResolveOutputFormat:
    """resolve_output_format is the single decider of the output format,
    honoring the new enum and falling back to the legacy no_reencode flag."""

    def test_explicit_formats_win(self):
        for fmt in ("original", "m4b", "mp3"):
            assert resolve_output_format({"conversion": {"output_format": fmt}}) == fmt

    def test_unknown_format_falls_back_to_legacy_flag(self):
        # An out-of-range output_format is ignored; the legacy flag decides.
        assert resolve_output_format({"conversion": {"output_format": "flac", "no_reencode": True}}) == "original"
        assert resolve_output_format({"conversion": {"output_format": "flac", "no_reencode": False}}) == "m4b"

    def test_missing_format_uses_legacy_flag(self):
        assert resolve_output_format({"conversion": {"no_reencode": True}}) == "original"
        assert resolve_output_format({"conversion": {"no_reencode": False}}) == "m4b"

    def test_empty_settings_default_to_m4b(self):
        assert resolve_output_format({}) == "m4b"
        assert resolve_output_format({"conversion": {}}) == "m4b"

    def test_explicit_format_overrides_stale_legacy_flag(self):
        # output_format is authoritative even if no_reencode disagrees.
        assert resolve_output_format({"conversion": {"output_format": "m4b", "no_reencode": True}}) == "m4b"
        assert resolve_output_format({"conversion": {"output_format": "original", "no_reencode": False}}) == "original"


class TestOutputFormatNormalization:
    """load_settings back-fills output_format from the legacy no_reencode flag
    for old settings.json files that predate the enum."""

    def test_legacy_no_reencode_true_becomes_original(self, settings_file):
        settings_file.write_text(json.dumps({"conversion": {"no_reencode": True}}))
        settings = load_settings()
        assert settings["conversion"]["output_format"] == "original"
        # The legacy flag itself is preserved as-loaded.
        assert settings["conversion"]["no_reencode"] is True

    def test_legacy_without_either_key_defaults_to_m4b(self, settings_file):
        # An old file that mentions conversion but neither key keeps the default.
        settings_file.write_text(json.dumps({"conversion": {"quality": "Low"}}))
        settings = load_settings()
        assert settings["conversion"]["output_format"] == "m4b"

    def test_legacy_no_reencode_false_defaults_to_m4b(self, settings_file):
        settings_file.write_text(json.dumps({"conversion": {"no_reencode": False}}))
        settings = load_settings()
        assert settings["conversion"]["output_format"] == "m4b"

    def test_explicit_output_format_in_file_is_not_overridden(self, settings_file):
        # A file that already carries output_format is trusted even if the
        # legacy flag would suggest otherwise.
        settings_file.write_text(json.dumps({"conversion": {"output_format": "mp3", "no_reencode": True}}))
        settings = load_settings()
        assert settings["conversion"]["output_format"] == "mp3"

    def test_no_conversion_section_keeps_default(self, settings_file):
        settings_file.write_text(json.dumps({"username": "bob"}))
        settings = load_settings()
        assert settings["conversion"]["output_format"] == "m4b"


class TestNewNestedKeysRoundTrip:
    """The new nested conversion keys deep-merge cleanly over old files and
    the defaults fill in anything a partial file omits."""

    def test_partial_mp3_block_keeps_sibling_defaults(self, settings_file):
        settings_file.write_text(json.dumps({"conversion": {"mp3": {"vbr_quality": 5}}}))
        settings = load_settings()
        mp3 = settings["conversion"]["mp3"]
        # The value from the file wins...
        assert mp3["vbr_quality"] == 5
        # ...while the untouched siblings keep their defaults.
        assert mp3["target"] == "quality"
        assert mp3["bitrate_kbps"] == 128
        assert mp3["encoder_quality"] == "High"

    def test_partial_chapters_block_keeps_sibling_defaults(self, settings_file):
        settings_file.write_text(json.dumps({"conversion": {"chapters": {"strip_unabridged": True}}}))
        settings = load_settings()
        chapters = settings["conversion"]["chapters"]
        assert chapters["strip_unabridged"] is True
        assert chapters["combine_nested_titles"] is False
        assert chapters["chapter_title_template"] == "{ch_title}"

    def test_new_top_level_conversion_keys_have_defaults(self, settings_file):
        settings_file.write_text(json.dumps({"username": "bob"}))
        conv = load_settings()["conversion"]
        assert conv["download_quality"] == "best"
        assert conv["save_cover_alongside"] is False
        assert conv["save_metadata_json"] is False
        assert conv["create_cue_sheet"] is False
        assert conv["retain_aax"] is False
        # An old settings.json predating the annotations sidecar must default off.
        assert conv["save_annotations"] is False

    def test_new_naming_keys_have_defaults(self, settings_file):
        settings_file.write_text(json.dumps({"username": "bob"}))
        naming = load_settings()["naming"]
        assert naming["folder_template"] == ""
        assert naming["file_template"] == ""

    def test_full_nested_roundtrip_survives_reload(self, settings_file):
        """Simulate a save: a full merged dict written back and reloaded is stable."""
        merged = load_settings()
        merged["conversion"]["output_format"] = "mp3"
        merged["conversion"]["mp3"]["target"] = "bitrate"
        merged["conversion"]["mp3"]["bitrate_kbps"] = 192
        merged["conversion"]["chapters"]["merge_credit_chapters"] = True
        settings_file.write_text(json.dumps(merged))
        reloaded = load_settings()
        assert reloaded["conversion"]["output_format"] == "mp3"
        assert reloaded["conversion"]["mp3"]["target"] == "bitrate"
        assert reloaded["conversion"]["mp3"]["bitrate_kbps"] == 192
        assert reloaded["conversion"]["chapters"]["merge_credit_chapters"] is True
