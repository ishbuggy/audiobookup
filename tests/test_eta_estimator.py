# tests/test_eta_estimator.py

import json

from audible_downloader import eta_estimator


def _write_cache(rates):
    """Seed the estimator's on-disk cache with the given rate history."""
    with open(eta_estimator.ETA_CACHE_FILE, "w") as f:
        json.dump({"conversion_rates": rates}, f)


class TestGetAverageRate:
    """get_average_rate() is the single source of truth for the sec/min rate the
    large-bulk warning (v0.20 Phase 6 / FR13) multiplies by total runtime."""

    def teardown_method(self):
        import os

        # Keep tests independent: the cache file lives in the temp CONFIG_DIR.
        if os.path.exists(eta_estimator.ETA_CACHE_FILE):
            os.remove(eta_estimator.ETA_CACHE_FILE)

    def test_no_history_returns_conservative_default(self):
        # With no cache file at all, fall back to the 10 sec/min default guess.
        rate = eta_estimator.get_average_rate()
        assert rate == 10.0

    def test_empty_history_returns_default(self):
        _write_cache([])
        assert eta_estimator.get_average_rate() == 10.0

    def test_returns_average_of_recorded_rates(self):
        _write_cache([4.0, 6.0, 8.0])
        assert eta_estimator.get_average_rate() == 6.0

    def test_matches_estimate_conversion_time(self):
        # The warning multiplies the rate by runtime; that product must line up
        # with the estimator's own per-book estimate for the same runtime.
        _write_cache([5.0, 7.0])
        runtime_min = 120
        assert eta_estimator.estimate_conversion_time(runtime_min) == int(
            runtime_min * eta_estimator.get_average_rate()
        )
