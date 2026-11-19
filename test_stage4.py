"""Isolated Stage 4 import, search, UI and offline integration regressions."""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import app
import import_places as importer
import navigation as nav
import places


def sample_extract():
    # Deliberately synthetic OSM-shaped data used only in temporary test databases.
    return {"osm3s": {"timestamp_osm_base": "2026-11-18T00:00:00Z"}, "elements": [
        {"type": "node", "id": 10, "lat": 12.8, "lon": 77.6,
         "tags": {"name": "Test clinic", "amenity": "clinic", "opening_hours": "unknown", "name:en": "English alias"}},
        {"type": "way", "id": 10, "center": {"lat": 12.81, "lon": 77.6},
         "tags": {"name": "Test clinic", "amenity": "clinic"}},
        {"type": "relation", "id": 20, "center": {"lat": 12.9, "lon": 77.5},
         "tags": {"name": "Test town", "place": "town"}},
        {"type": "node", "id": 30, "lat": 12.9, "lon": 77.5,
         "tags": {"amenity": "drinking_water"}},
    ]}


class Stage4Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "region.db"
        self.wp = self.root / "waypoints.json"
        self.raw = json.dumps(sample_extract()).encode()
        importer.build_database(self.raw, self.path, "2026-11-18T00:00:00Z")

    def database(self):
        db = places.PlacesDatabase(str(self.path), read_only=True)
        self.addCleanup(db.close)
        return db

    def ui(self, asynchronous=False):
        ui = app.SURVIVEXApp(str(self.wp), str(self.path), async_search=asynchronous)
        self.addCleanup(ui.close)
        ui.set_manual_coordinates(12.8, 77.6)
        return ui

    def test_import_identity_tags_geometry_and_bounds(self):
        db = self.database()
        results = db.search_by_name("Test clinic")
        self.assertEqual({p.id for p in results}, {40, 41})
        self.assertEqual({p.osm_type for p in results}, {"node", "way"})
        self.assertTrue(all(p.source == "OpenStreetMap" and not p.is_test_data for p in results))
        self.assertEqual(results[0].original_name, "Test clinic")
        self.assertEqual(results[0].metadata["tags"]["opening_hours"], "unknown")
        self.assertEqual(results[1].metadata["coordinate_method"], "bounding_box_center")
        self.assertEqual(results[0].region_id, importer.REGION)
        self.assertEqual(db.search_by_name("english ALIAS")[0].id, 40)
        self.assertEqual(db.coverage_state(12.7, 77.45), "IN COVERAGE")
        self.assertEqual(db.coverage_state(12.699, 77.45), "OUTSIDE COVERAGE")
        raw = sample_extract()
        raw["elements"].extend([raw["elements"][0],
                               {"type": "node", "id": 99, "lat": 40, "lon": 77, "tags": {"place": "town"}}])
        report = importer.build_database(json.dumps(raw).encode(), self.root / "new.db")
        self.assertEqual(sum(report["counts"].values()), 4)
        self.assertEqual(report["skipped"], 1)

    def test_atomic_update_recovery_and_waypoint_survival(self):
        ui = self.ui()
        ui.selected_place = ui.places_db.get_place_by_id(40)
        ui.current_screen = app.Screen.PLACE_DETAILS
        ui.handle_action(app.Action.SELECT)
        wp_bytes = self.wp.read_bytes()
        old_bytes = self.path.read_bytes()
        updated = sample_extract()
        updated["elements"][0]["tags"]["name"] = "Renamed Test clinic"
        importer.build_database(json.dumps(updated).encode(), self.path)
        backup = self.path.with_suffix(".previous.db")
        recovered = places.PlacesDatabase(str(backup), read_only=True)
        self.assertEqual(recovered.get_place_by_id(40).name, "Test clinic")
        self.assertEqual(recovered.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        recovered.close()
        self.assertEqual(self.wp.read_bytes(), wp_bytes)
        manager = nav.WaypointManager(self.wp)
        wp = manager.get_active_waypoint()
        self.assertEqual(wp.place_id, 40)
        self.assertEqual(wp.provenance["osm_id"], 10)
        self.assertEqual(wp.provenance["metadata"]["tags"]["opening_hours"], "unknown")
        self.assertEqual(self.database().get_place_by_id(40).name, "Renamed Test clinic")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            importer.build_database(b'{"remark":"timeout","elements":[]}', self.path)
        self.assertEqual(before, self.path.read_bytes())
        with patch.object(importer.os, "replace", side_effect=OSError("power-cut simulation")):
            with self.assertRaises(OSError):
                importer.build_database(self.raw, self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_original_store_is_protected(self):
        with self.assertRaises(ValueError):
            importer.build_database(self.raw, places.DB_FILE_DEFAULT)
        synthetic = self.root / "synthetic.db"
        synthetic.write_bytes(Path(places.DB_FILE_DEFAULT).read_bytes())
        old = synthetic.read_bytes()
        with self.assertRaises(ValueError):
            importer.build_database(self.raw, synthetic)
        self.assertEqual(synthetic.read_bytes(), old)

    def test_exact_prefix_substring_wildcards_case_and_index(self):
        db = places.PlacesDatabase(":memory:")
        self.addCleanup(db.close)
        for name in ["xAlpha", "Alpha beta", "ALPHA", "100%_literal", "100ANYliteral", "Straße", "STRASSE"]:
            db.insert_place(name, 12.8, 77.6, "clinic")
        self.assertEqual(db.search_by_name("alpha", limit=1)[0].name, "ALPHA")
        self.assertEqual([p.name for p in db.search_by_name("alpha")], ["ALPHA", "Alpha beta", "xAlpha"])
        self.assertEqual([p.name for p in db.search_by_name("%_")], ["100%_literal"])
        self.assertEqual(len(db.search_by_name("strasse")), 2)
        plan = str(db.conn.execute("EXPLAIN QUERY PLAN SELECT * FROM places WHERE name_normalized > ? AND name_normalized < ?",
                                  ("a", "a" + chr(0x10ffff))).fetchall()[0][3])
        self.assertIn("idx_places_name_norm", plan)
        self.assertFalse(db.search_by_name("unknown"))

    def test_coverage_states_and_nearby_order(self):
        db = self.database()
        self.assertEqual(db.coverage_state(), "UNKNOWN COVERAGE")
        results = db.search_nearby(12.8, 77.6, 50, category="clinic")
        self.assertEqual([p.id for p in results], [40, 41])
        self.assertTrue(all(p.category == "clinic" for p in results))
        empty = places.PlacesDatabase(":memory:")
        self.addCleanup(empty.close)
        self.assertEqual(empty.coverage_state(12.8, 77.6), "EMPTY DATABASE")
        empty.insert_place("uncovered", 12.8, 77.6, "clinic")
        self.assertEqual(empty.coverage_state(12.8, 77.6), "UNKNOWN COVERAGE")
        empty.set_coverage("region A", 0, 1, 0, 1)
        empty.set_coverage("region B", 12, 13, 77, 78)
        self.assertEqual(empty.coverage_state(12.8, 77.6), "IN COVERAGE")

    def test_joystick_only_search_and_paged_details(self):
        ui = self.ui()
        ui.current_screen = app.Screen.PLACES_SEARCH
        ui.handle_action(app.Action.SELECT)
        self.assertEqual(ui.current_screen, app.Screen.SEARCH_KEYBOARD)
        # Only five joystick actions plus BACK. No CHAR_INPUT or direct query assignment.
        for key in list("TEST") + ["SPACE"] + list("CLINIC") + ["FIND"]:
            target = app.SEARCH_KEYS.index(key)
            while ui.keyboard_index != target:
                ui.handle_action(app.Action.RIGHT)
            ui.handle_action(app.Action.SELECT)
        self.assertEqual(ui.places_query, "test clinic")
        self.assertEqual(len(ui.places_results), 2)
        ui.handle_action(app.Action.SELECT)
        self.assertEqual(ui.current_screen, app.Screen.PLACE_DETAILS)
        ui.handle_action(app.Action.RIGHT)
        display = app.HeadlessDisplayAdapter()
        ui.render(display)
        self.assertTrue(any("OSM node/10" in row.get("text", "") for row in display.draw_log))
        ui.handle_action(app.Action.DOWN)
        ui.handle_action(app.Action.SELECT)
        self.assertTrue(any(wp.provenance for wp in ui.waypoint_mgr.list_waypoints()))
        ui.handle_action(app.Action.BACK)
        self.assertEqual(ui.current_screen, app.Screen.PLACES_RESULTS)

    def test_async_query_stays_responsive_and_discards_old_position(self):
        ui = self.ui(True)
        gate = threading.Event()
        started = threading.Event()
        original = ui.search_in_worker
        def slow(path, request):
            started.set()
            gate.wait(2)
            return original(path, request)
        with patch.object(ui, "search_in_worker", slow):
            ui.places_query = "Test"
            start = time.perf_counter()
            ui.execute_places_name_search()
            self.assertLess(time.perf_counter() - start, .1)
            self.assertTrue(started.wait(1))
            ui.sensor_mode = nav.SensorMode.LIVE_GNSS
            ui.tick()
            self.assertFalse(ui.places_results)
            ui.current_screen = app.Screen.HOME
            ui.handle_action(app.Action.DOWN)
            self.assertEqual(ui.home_menu_idx, 1)
            gate.set()
            deadline = time.monotonic() + 3
            while ui.search_busy and time.monotonic() < deadline:
                ui.tick()
                time.sleep(.005)
        self.assertFalse(ui.search_busy)
        self.assertTrue(ui.places_results)
        self.assertTrue(all(p.distance_m is None for p in ui.places_results))
        self.assertEqual(ui.current_screen, app.Screen.HOME)

    def test_network_blocked_runtime(self):
        def denied(*args, **kwargs):
            raise AssertionError("Runtime attempted network access")
        with patch.object(socket, "socket", denied), patch.object(socket, "create_connection", denied):
            ui = self.ui()
            ui.places_query = "Test"
            ui.execute_places_name_search()
            self.assertTrue(ui.places_results)
            ui.execute_places_nearby_search()
            self.assertTrue(ui.places_results)
            ui.render(app.HeadlessDisplayAdapter())

    def test_long_names_fit_320_by_240(self):
        if app.pygame is None:
            self.skipTest("Pygame unavailable")
        ui = self.ui()
        display = app.PygameDisplayAdapter(1)
        self.addCleanup(display.close)
        ui.selected_place = ui.places_db.get_place_by_id(40)
        ui.selected_place.name = "W" * 160 + " A very long duplicated clinic name"
        ui.places_results = [ui.selected_place] * 30
        ui.places_search_performed = False
        seen = []
        draw = display.draw_text
        def checked(x, y, text, size="med", color=None, style="NORMAL"):
            fitted = display.fit_text(text, display.WIDTH - x, size)
            self.assertLessEqual(x + display.measure_text(fitted, size), 320)
            self.assertLess(y, 240)
            seen.append(fitted)
            draw(x, y, text, size, color, style)
        display.draw_text = checked
        for screen in app.Screen:
            ui.current_screen = screen
            ui.render(display)
        ui.current_screen = app.Screen.PLACE_DETAILS
        lines = display.wrap_text(ui.selected_place.display_name, 292, "small")
        self.assertEqual("".join(lines), ui.selected_place.name)
        self.assertGreater(len(lines), 3)
        for page in range((len(lines) + 2) // 3 + 3):
            ui.detail_page = page
            ui.render(display)
        self.assertEqual(display.surface.get_size(), (320, 240))
        self.assertTrue(seen)

    def test_deterministic_sensor_replay_uses_navigation_engine(self):
        destination = nav.Waypoint("Replay target", 12.82, 77.58)
        gps = nav.GPSReading(12.813337, 77.5789509, timestamp=1000, accuracy_m=3)
        compass = nav.CompassReading(0, timestamp=1000)
        def replay():
            samples = [
                (1000, gps, compass, destination),
                (1000, gps, nav.CompassReading(90, timestamp=1000), destination),
                (1001, nav.GPSReading(gps.lat, gps.lon, timestamp=1001, has_fix=False), compass, destination),
                (1012, gps, compass, destination),
                (1013, nav.GPSReading(gps.lat, gps.lon, timestamp=1013, accuracy_m=3),
                 nav.CompassReading(90, timestamp=1013), nav.Waypoint("Changed target", 12.83, 77.59)),
            ]
            return [nav.NavigationState.from_readings(g, c, 0, now).to_destination(target)
                    for now, g, c, target in samples]
        first, second = replay(), replay()
        self.assertEqual(first, second)
        self.assertEqual([s.status for s in first],
                         [nav.NavStatus.OK, nav.NavStatus.OK, nav.NavStatus.NO_FIX, nav.NavStatus.STALE_GPS, nav.NavStatus.OK])
        self.assertAlmostEqual(nav.relative_turn_angle_deg(first[1].turn_angle_deg, first[0].turn_angle_deg), -90)

    def test_real_dataset_offline_if_installed(self):
        path = Path(places.REAL_DB_FILE)
        if not path.exists():
            self.skipTest("Real OSM extraction pending")
        db = places.PlacesDatabase(str(path), read_only=True)
        self.addCleanup(db.close)
        with patch.object(socket, "socket", side_effect=AssertionError("Network forbidden")):
            rows = db.search_by_name("Bannerghatta", limit=100)
            self.assertTrue(rows)
            self.assertTrue(all(p.osm_type and p.osm_id and p.extracted_at for p in rows))
            self.assertTrue(all(p.source == "OpenStreetMap" for p in rows))
            nearby = db.search_nearby(12.8, 77.6, radius_km=25)
            self.assertTrue(nearby)
            self.assertEqual([p.distance_m for p in nearby], sorted(p.distance_m for p in nearby))


if __name__ == "__main__":
    unittest.main()
