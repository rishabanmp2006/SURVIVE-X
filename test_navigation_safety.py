"""Navigation regressions using isolated waypoint and place stores."""
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import app
import navigation as nav
import places


class NavigationSafetyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.wp = self.root / "waypoints.json"
        self.db = self.root / "places.db"
        self.db.write_bytes(Path(places.DB_FILE_DEFAULT).read_bytes())
        self.ui = self.open_app()
        self.display = app.HeadlessDisplayAdapter()

    def open_app(self):
        ui = app.SURVIVEXApp(str(self.wp), str(self.db))
        self.addCleanup(ui.places_db.close)
        return ui

    def render(self, screen):
        self.ui.current_screen = screen
        self.ui.render(self.display)
        return "\n".join(row["text"] for row in self.display.draw_log if row["type"] == "text")

    def select(self, screen):
        self.ui.current_screen = screen
        self.ui.handle_action(app.Action.SELECT)

    def test_fix_loss_stale_invalid_recovery_across_screens(self):
        ui = self.ui
        ui.places_query = "Morton"
        ui.execute_places_name_search()
        ui.selected_place = ui.places_results[0]
        target = ui.selected_place
        ui.place_details_btn_idx = 0
        self.select(app.Screen.PLACE_DETAILS)
        normal = self.render(app.Screen.NAVIGATION)
        self.assertGreater(self.display.arrow_log[0]["angle_deg"] ** 2, 0)
        ui.sim_has_fix = False
        for status in [nav.NavStatus.NO_FIX, nav.NavStatus.STALE_GPS, nav.NavStatus.INVALID_COORDS]:
            with self.subTest(status=status):
                state = ui.navigation_state()
                self.assertEqual(state.position_status, status)
                vector = state.to_destination(target)
                self.assertEqual((vector.distance_m, vector.bearing_deg, vector.turn_angle_deg), (None,) * 3)
                self.assertIn("Bearing: N/A", self.render(app.Screen.NAVIGATION))
                self.assertIsNone(self.display.arrow_log[0]["angle_deg"])
                self.assertNotIn("Turn ", self.render(app.Screen.HOME))
                self.assertIn("N/A / N/A", self.render(app.Screen.PLACE_DETAILS))
                self.assertIn("(N/A)", self.render(app.Screen.WAYPOINTS))
                ui.execute_places_nearby_search()
                self.assertFalse(ui.places_results)
                ui.execute_places_name_search()
                self.assertEqual([p.id for p in ui.places_results], [target.id])
                self.assertIsNone(ui.places_results[0].distance_m)
                self.assertIsNone(ui.places_results[0].bearing_deg)
            ui.sim_has_fix = True
            if status == nav.NavStatus.NO_FIX:
                ui.handle_action(app.Action.SIM_TOGGLE_STALE)
            else:
                ui.sim_lat = math.nan
        ui.sim_lat = 46.7865
        ui.handle_action(app.Action.SIM_TOGGLE_STALE)
        self.assertEqual(ui.navigation_state().position_status, nav.NavStatus.OK)
        self.assertIn("Bearing: ", normal)
        self.assertNotIn("Bearing: N/A", self.render(app.Screen.NAVIGATION))
        ui.execute_places_nearby_search()
        self.assertTrue(ui.places_results)
        self.assertTrue(all(p.distance_m is not None for p in ui.places_results))

    def test_results_already_visible_at_loss_and_recovery(self):
        ui = self.ui
        ui.execute_places_nearby_search()
        self.assertTrue(ui.places_results)
        ui.sim_has_fix = False
        self.assertIn("POSITION UNAVAILABLE", self.render(app.Screen.PLACES_RESULTS))
        self.assertFalse(ui.places_results)
        ui.sim_has_fix = True
        self.render(app.Screen.PLACES_RESULTS)
        self.assertTrue(ui.places_results)
        ui.places_query = "muir"
        ui.execute_places_name_search()
        ui.sim_accuracy_m = -1
        self.render(app.Screen.PLACES_RESULTS)
        self.assertTrue(ui.places_results)
        self.assertTrue(all(p.distance_m is None for p in ui.places_results))

    def test_manual_start_uses_first_entered_position(self):
        ui = self.ui
        ui.cycle_sensor_mode()
        self.assertFalse(ui.navigation_state().has_position)
        self.assertIsNone(ui.start_position)
        ui.set_manual_coordinates(12.9716, 77.5946)
        ui.set_manual_coordinates(13, 78)
        ui.waypoints_idx = 1
        self.select(app.Screen.WAYPOINTS)
        wp = ui.waypoint_mgr.get_active_waypoint()
        self.assertEqual((wp.lat, wp.lon), (12.9716, 77.5946))
        self.assertEqual(wp.source, nav.SensorMode.MANUAL.value)

    def test_no_gnss_fix_cannot_save_or_borrow_sim_heading(self):
        self.ui.sensor_mode = nav.SensorMode.LIVE_GNSS
        before = self.wp.read_bytes()
        self.select(app.Screen.WAYPOINTS)
        self.assertEqual(self.wp.read_bytes(), before)
        self.assertIn("Cannot save", self.ui.notification_msg)
        self.assertIsNone(self.ui.navigation_state().true_heading)
        self.assertNotIn("Turn ", self.render(app.Screen.HOME))

    def test_bad_samples_and_boundaries(self):
        base = nav.GPSReading(0, 0, timestamp=100)
        compass = nav.CompassReading(0, timestamp=100)
        for field, values in {
            "timestamp": [math.nan, math.inf, -1, 101, 89, None],
            "accuracy_m": [math.nan, math.inf, -0.1, 31],
            "hdop": [math.nan, math.inf, -1, 0, 6],
            "lat": [math.nan, math.inf, -91, "0"],
            "lon": [181, None], "altitude_m": [math.inf],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertFalse(replace(base, **{field: value}).validate(100)[0])
        for bad in [replace(compass, timestamp=t) for t in [math.nan, math.inf, 101, 94]] + [
            replace(compass, is_calibrated=False), replace(compass, magnetic_heading_deg=math.nan)
        ]:
            self.assertIsNone(nav.NavigationState.from_readings(base, bad, 0, 100).true_heading)
        self.ui.set_manual_coordinates(90, 180)
        self.ui.handle_action(app.Action.SIM_MOVE_NORTH)
        self.ui.handle_action(app.Action.SIM_MOVE_EAST)
        self.assertEqual(self.ui.manual_lat, 90)
        self.assertAlmostEqual(self.ui.manual_lon, -179.999)
        self.assertTrue(self.ui.navigation_state().has_position)
        self.render(app.Screen.HOME)

    def test_missing_heading_keeps_absolute_vector(self):
        self.ui.sim_compass_available = False
        self.assertIn("HEADING UNAVAILABLE", self.render(app.Screen.HOME))
        text = self.render(app.Screen.NAVIGATION)
        self.assertIn("HEADING UNAVAILABLE", text)
        self.assertNotIn("Bearing: N/A", text)
        self.assertIsNone(self.display.arrow_log[0]["angle_deg"])
        sol = self.ui.navigation_state().to_destination(self.ui.waypoint_mgr.get_active_waypoint())
        self.assertGreater(sol.distance_m, 0)

    def test_duplicate_identity_provenance_and_active_restart(self):
        ui = self.ui
        ui.places_query = "Wilderness Ranger Station"
        ui.execute_places_name_search()
        rows = ui.places_results
        self.assertEqual(len(rows), 2)
        self.assertIn("TEST DATA", self.render(app.Screen.PLACES_RESULTS))
        for place in rows:
            ui.selected_place = place
            self.assertIn("TEST DATA", self.render(app.Screen.PLACE_DETAILS))
            ui.place_details_btn_idx = 0
            self.select(app.Screen.PLACE_DETAILS)
            self.assertIn("DEST: TEST DATA", self.render(app.Screen.NAVIGATION))
            ui.place_details_btn_idx = 1
            self.select(app.Screen.PLACE_DETAILS)
        self.ui = self.open_app()
        manager = self.ui.waypoint_mgr
        saved = [wp for wp in manager.list_waypoints() if wp.place_id is not None]
        self.assertEqual({(wp.place_id, wp.lat, wp.lon) for wp in saved},
                         {(p.id, p.lat, p.lon) for p in rows})
        self.assertEqual(len(saved), 2)
        self.assertEqual(manager.get_active_waypoint().place_id, rows[-1].id)
        self.assertIn("DEST: TEST DATA", self.render(app.Screen.NAVIGATION))
        manager.select_active_waypoint(saved[0].name)
        self.assertEqual(self.open_app().waypoint_mgr.get_active_waypoint().place_id, rows[0].id)

    def test_arrival_requires_accuracy_and_real_destination(self):
        gps = nav.GPSReading(0, 0, timestamp=100)
        for accuracy, status in [(None, nav.NavStatus.POSITION_UNCERTAIN),
                                 (20, nav.NavStatus.POSITION_UNCERTAIN), (3, nav.NavStatus.ARRIVED)]:
            state = nav.NavigationState.from_readings(replace(gps, accuracy_m=accuracy), None, None, 100)
            self.assertEqual(state.to_destination(nav.Waypoint("start", 0, 0)).status, status)
            self.assertIsNone(state.to_destination(None).distance_m)
            self.assertIsNone(state.to_destination(nav.Waypoint("bad", 91, 0)).bearing_deg)
        manager = self.ui.waypoint_mgr
        for wp in manager.list_waypoints():
            manager.delete_waypoint(wp.name)
        self.assertIn("NO DESTINATION", self.render(app.Screen.NAVIGATION))
        self.assertFalse(self.open_app().waypoint_mgr.list_waypoints())

    def test_legacy_and_malformed_stores_preserved(self):
        production = Path(__file__).with_name("waypoints.json").read_bytes()
        self.wp.write_bytes(production)
        ui = self.open_app()
        self.assertEqual(len(ui.waypoint_mgr.list_waypoints()), len(json.loads(production)["waypoints"]))
        self.assertEqual(self.wp.read_bytes(), production)
        for raw in [b"{", b"{}", b'{"waypoints":[{"name":"bad","lat":100,"lon":1}]}']:
            self.wp.write_bytes(raw)
            ui = self.open_app()
            ui.current_screen = app.Screen.WAYPOINTS
            ui.handle_action(app.Action.SELECT)
            self.assertTrue(ui.waypoint_mgr.load_error)
            self.assertFalse(ui.waypoint_mgr.list_waypoints())
            self.assertEqual(self.wp.read_bytes(), raw)
        self.assertEqual(self.db.read_bytes(), Path(places.DB_FILE_DEFAULT).read_bytes())

    def test_legacy_fixture_identity_is_recovered_without_rewriting_on_load(self):
        place = self.ui.places_db.search_by_name("Morton")[0]
        record = {"name": place.name, "lat": place.lat, "lon": place.lon,
                  "description": "Manual Destination"}
        original = json.dumps({"active_waypoint": place.name, "waypoints": [record]}).encode()
        self.wp.write_bytes(original)
        self.ui = self.open_app()
        self.assertIn("TEST DATA", self.render(app.Screen.NAVIGATION))
        self.assertEqual(self.ui.waypoint_mgr.get_active_waypoint().place_id, place.id)
        self.assertEqual(self.wp.read_bytes(), original)

    def test_arrival_radius_includes_position_error(self):
        target = nav.Waypoint("near", 0, 0.00008)
        gps = nav.GPSReading(0, 0, timestamp=100, accuracy_m=3)
        sol = nav.compute_nav_solution(gps, None, target, current_time=100)
        self.assertLess(sol.distance_m, 10)
        self.assertNotEqual(sol.status, nav.NavStatus.ARRIVED)
        sol = nav.compute_nav_solution(replace(gps, accuracy_m=1), None, target, current_time=100)
        self.assertEqual(sol.status, nav.NavStatus.ARRIVED)

    def test_concurrent_or_same_name_save_preserves_previous_points(self):
        manager = self.ui.waypoint_mgr
        stale = nav.WaypointManager(self.wp)
        first = manager.add_waypoint("Camp", 1, 2)
        second = manager.add_waypoint("Camp", 3, 4)
        self.assertNotEqual(first.name, second.name)
        with self.assertRaises(ValueError):
            stale.add_waypoint("Third", 5, 6)
        restored = nav.WaypointManager(self.wp)
        self.assertEqual(restored.get_waypoint(first.name).lat, 1)
        self.assertEqual(restored.get_waypoint(second.name).lat, 3)
        self.assertIsNone(restored.get_waypoint("Third"))


if __name__ == "__main__":
    unittest.main()
