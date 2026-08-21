import json
import sys
import tempfile
import unittest
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gmap_tracker import config
from gmap_tracker.config import ValidationError

FONTS = ["DejaVuSansMono.ttf", "small_6x8"]

VALID = {
    "timezone": "Europe/London",
    "schedule": {"start": "07:00", "end": "16:00", "weekdays": [0, 1, 2, 3, 4]},
    "poll_interval_minutes": 3,
    "eta_offset_minutes": 10,
    "route": {
        "origin": {"latitude": 53.474157, "longitude": -2.263105},
        "destination": {"latitude": 53.417157, "longitude": -2.315093},
        "intermediates": [{"via": True, "location": {"latitude": 53.44, "longitude": -2.29}}],
    },
    "route_modifiers": {"avoid_tolls": True},
    "language_code": "en-GB",
    "units": "IMPERIAL",
    "display": {"font": "small_6x8", "font_size": 8},
}


def parse(**overrides):
    return config.parse(config.deep_merge(VALID, overrides), FONTS)


class TestMerge(unittest.TestCase):
    def test_override_wins_and_nested_keys_survive(self):
        merged = config.deep_merge(
            {"a": 1, "schedule": {"start": "07:00", "end": "16:00"}},
            {"schedule": {"end": "18:00"}},
        )
        self.assertEqual(merged, {"a": 1, "schedule": {"start": "07:00", "end": "18:00"}})

    def test_source_not_mutated(self):
        base = {"schedule": {"start": "07:00"}}
        config.deep_merge(base, {"schedule": {"start": "09:00"}})
        self.assertEqual(base, {"schedule": {"start": "07:00"}})


class TestValidation(unittest.TestCase):
    def test_valid_config_parses(self):
        cfg = parse()
        self.assertEqual(cfg.schedule.start, dtime(7, 0))
        self.assertTrue(cfg.is_configured)
        self.assertEqual(len(cfg.route.intermediates), 1)

    def test_font_traversal_rejected(self):
        for attempt in ("../../../etc/shadow", "/etc/shadow", "small_6x8/../../../etc/passwd"):
            with self.subTest(attempt=attempt), self.assertRaises(ValidationError) as ctx:
                parse(display={"font": attempt})
            self.assertEqual(ctx.exception.field, "display.font")

    def test_unknown_font_rejected(self):
        with self.assertRaises(ValidationError):
            parse(display={"font": "not_a_font"})

    def test_non_finite_coordinates_rejected(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                parse(route={"origin": {"latitude": bad, "longitude": 0.0}})

    def test_out_of_range_coordinates_rejected(self):
        with self.assertRaises(ValidationError):
            parse(route={"origin": {"latitude": 91.0, "longitude": 0.0}})
        with self.assertRaises(ValidationError):
            parse(route={"origin": {"latitude": 0.0, "longitude": 181.0}})

    def test_identical_origin_and_destination_rejected(self):
        point = {"latitude": 1.0, "longitude": 2.0}
        with self.assertRaises(ValidationError):
            parse(route={"origin": point, "destination": point})

    def test_too_many_intermediates_rejected(self):
        many = [{"location": {"latitude": 1.0, "longitude": 2.0}}] * (config.MAX_INTERMEDIATES + 1)
        with self.assertRaises(ValidationError):
            parse(route={"intermediates": many})

    def test_interval_bounds(self):
        for bad in (0, 0.5, 61):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                parse(poll_interval_minutes=bad)
        self.assertEqual(parse(poll_interval_minutes=1).poll_interval_minutes, 1)

    def test_offset_bounds(self):
        with self.assertRaises(ValidationError):
            parse(eta_offset_minutes=-1)
        with self.assertRaises(ValidationError):
            parse(eta_offset_minutes=121)

    def test_units_constrained_to_enum(self):
        with self.assertRaises(ValidationError):
            parse(units="MILES")
        self.assertEqual(parse(units="METRIC").units, "METRIC")

    def test_unknown_timezone_rejected(self):
        with self.assertRaises(ValidationError):
            parse(timezone="Mars/Olympus_Mons")

    def test_weekdays_validated(self):
        with self.assertRaises(ValidationError):
            parse(schedule={"weekdays": []})
        with self.assertRaises(ValidationError):
            parse(schedule={"weekdays": [7]})
        with self.assertRaises(ValidationError):
            parse(schedule={"weekdays": [True]})

    def test_bad_time_rejected(self):
        with self.assertRaises(ValidationError):
            parse(schedule={"start": "25:00"})
        with self.assertRaises(ValidationError):
            parse(schedule={"start": "not a time"})

    def test_font_size_bounds(self):
        with self.assertRaises(ValidationError):
            parse(display={"font_size": 5})
        with self.assertRaises(ValidationError):
            parse(display={"font_size": 25})

    def test_roundtrip_through_to_dict(self):
        cfg = parse()
        self.assertEqual(config.parse(config.to_dict(cfg), FONTS), cfg)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.defaults = self.dir / "defaults.json"
        self.override = self.dir / "config.json"
        self.defaults.write_text(json.dumps(VALID))

    def test_override_layers_over_defaults(self):
        self.override.write_text(json.dumps({"units": "METRIC"}))
        cfg, degraded = config.load(self.defaults, self.override, FONTS)
        self.assertIsNone(degraded)
        self.assertEqual(cfg.units, "METRIC")
        self.assertEqual(cfg.language_code, "en-GB")

    def test_atomic_write_leaves_no_temp_file(self):
        config.save_override({"units": "METRIC"}, self.override)
        self.assertEqual(json.loads(self.override.read_text()), {"units": "METRIC"})
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_interrupted_write_leaves_original_intact(self):
        config.save_override({"units": "METRIC"}, self.override)
        original = self.override.read_bytes()
        tmp = self.override.with_suffix(".json.tmp")
        tmp.write_text('{"units": "IMP')  # crash between write and replace
        self.assertEqual(self.override.read_bytes(), original)
        cfg, degraded = config.load(self.defaults, self.override, FONTS)
        self.assertIsNone(degraded)
        self.assertEqual(cfg.units, "METRIC")

    def test_corrupt_override_falls_back_to_defaults(self):
        self.override.write_text("{ this is not json")
        cfg, degraded = config.load(self.defaults, self.override, FONTS)
        self.assertIsNotNone(degraded)
        self.assertEqual(cfg.units, "IMPERIAL")

    def test_invalid_override_falls_back_to_defaults(self):
        self.override.write_text(json.dumps({"units": "FURLONGS"}))
        cfg, degraded = config.load(self.defaults, self.override, FONTS)
        self.assertIn("units", degraded)
        self.assertEqual(cfg.units, "IMPERIAL")

    def test_corrupt_defaults_and_override_degrade_to_fallback(self):
        self.defaults.write_text("nope")
        self.override.write_text("also nope")
        cfg, degraded = config.load(self.defaults, self.override, FONTS)
        self.assertIsNotNone(degraded)
        self.assertFalse(cfg.is_configured)

    def test_missing_files_are_not_an_error(self):
        cfg, degraded = config.load(self.dir / "no.json", self.dir / "none.json", FONTS)
        self.assertIsNone(degraded)
        self.assertFalse(cfg.is_configured)

    def test_reset_removes_override(self):
        config.save_override({"units": "METRIC"}, self.override)
        config.reset_override(self.override)
        self.assertFalse(self.override.exists())
        config.reset_override(self.override)


if __name__ == "__main__":
    unittest.main()
