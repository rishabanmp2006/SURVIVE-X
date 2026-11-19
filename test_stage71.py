"""Audit regressions; all state and executables are synthetic and temporary."""
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import navigation as nav
from app import Action, HeadlessDisplayAdapter, Screen, SURVIVEXApp
from ai_worker import ChatWorker
from chat import run_inference
from hardware_sensors import LiveSensors
from test_stage6 import sentence
from wokwi.display_adapter import display_text


def gga(t='123519', lat='4807.038', quality='1', sats='08', hdop='0.9'):
    return sentence(f'GPGGA,{t},{lat},N,01131.000,E,{quality},{sats},{hdop},545.4,M,46.9,M,,')


class AuditNavigation(unittest.TestCase):
    def test_bad_qualities_and_signed_coordinate(self):
        for quality in ('6', '7', '8', '9'):
            s = LiveSensors()
            self.assertEqual(s.feed_nmea(gga(quality=quality, sats='00')), 0)
            self.assertFalse(s.gps().has_fix, quality)
            self.assertEqual(LiveSensors().feed_nmea(gga(quality=quality)), 0)
        for lat in ('-4807.038', '+4807.038', '4807', '4807.123456789'):
            s = LiveSensors(); self.assertEqual(s.feed_nmea(gga(lat=lat)), 0)
            self.assertFalse(s.gps().has_fix)
        for sats, hdop in (('', '1'), ('3','1'), ('8',''), ('8','6')):
            self.assertEqual(LiveSensors().feed_nmea(gga(sats=sats, hdop=hdop)), 0)

    def test_frozen_timestamp_cannot_refresh(self):
        now = [100.0]; s = LiveSensors(clock=lambda: now[0])
        self.assertEqual(s.feed_nmea(gga()), 1)
        now[0] += 11
        self.assertEqual(s.feed_nmea(gga()), 0)
        self.assertFalse(s.gps().validate(current_time=now[0])[0])
        self.assertEqual(s.feed_nmea(gga(t='123520')), 1)
        self.assertTrue(s.gps().validate(current_time=now[0])[0])

    def test_live_start_and_missing_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensors = LiveSensors(); ui = SURVIVEXApp(storage_path=str(Path(tmp)/'wp'), live_sensors=sensors)
            self.addCleanup(ui.close)
            ui.sensor_mode = nav.SensorMode.LIVE_GNSS
            ui.current_screen = Screen.WAYPOINTS; ui.waypoints_idx = 1
            ui.handle_action(Action.SELECT)
            self.assertEqual(ui.notification_msg, 'START UNAVAILABLE')
            self.assertIsNone(ui.start_position)
            sensors.feed_nmea(gga()); ui.tick()
            start = ui.start_position
            self.assertEqual(start.source, nav.SensorMode.LIVE_GNSS.value)
            self.assertIsNotNone(start.created_at)
            sensors.feed_nmea(gga(t='123520', lat='4808.038')); ui.tick()
            ui.handle_action(Action.SELECT)
            self.assertEqual(ui.current_screen, Screen.NAVIGATION)
            self.assertAlmostEqual(ui.waypoint_mgr.get_active_waypoint().lat, start.lat)
            sensors.feed_nmea(gga(t='123521', lat='4809.038')); ui.tick()
            self.assertEqual(ui.start_position, start)
            self.assertAlmostEqual(ui.waypoint_mgr.get_active_waypoint().lat, start.lat)

    def test_unknown_accuracy_and_manual_heading(self):
        now = time.time(); gps = nav.GPSReading(1, 1, timestamp=now, has_fix=True, accuracy_m=None)
        target = nav.Waypoint('near', 1.000001, 1)
        sol = nav.compute_nav_solution(gps, nav.CompassReading(0, now, True), target, declination_deg=0)
        self.assertEqual(sol.status, nav.NavStatus.POSITION_UNCERTAIN)
        self.assertIsNotNone(sol.distance_m); self.assertIsNotNone(sol.bearing_deg)
        self.assertIsNone(sol.turn_angle_deg)
        with tempfile.TemporaryDirectory() as tmp:
            ui = SURVIVEXApp(storage_path=str(Path(tmp)/'wp')); self.addCleanup(ui.close)
            ui.set_manual_coordinates(1, 1)
            self.assertIsNone(ui.navigation_state().true_heading)
            self.assertIsNone(ui.get_current_compass())

    def test_compass_disconnect_during_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            sensors = LiveSensors(); ui = SURVIVEXApp(storage_path=str(Path(tmp)/'wp'), live_sensors=sensors, live_declination=0)
            self.addCleanup(ui.close)
            ui.sensor_mode = nav.SensorMode.LIVE_GNSS
            sensors._gps = nav.GPSReading(1, 1, timestamp=time.time(), has_fix=True, accuracy_m=3, sensor_mode=nav.SensorMode.LIVE_GNSS)
            sensors.compass_sample(20, True); ui.current_screen = Screen.NAVIGATION
            calls = [0]; original = sensors.compass
            def disappearing():
                calls[0] += 1
                result = original(); sensors.disconnect_compass(); return result
            with patch.object(sensors, 'compass', side_effect=disappearing):
                ui.render(HeadlessDisplayAdapter())
            self.assertEqual(calls[0], 1)
            self.assertIsNone(ui.navigation_state().true_heading)


class AuditAI(unittest.TestCase):
    def test_constraints_truncation_and_correction(self):
        worker = ChatWorker(server='/usr/bin/true', model=str(Path(__file__).resolve()))
        process = MagicMock(); process.poll.return_value = None
        captured = []
        def urlopen(request, timeout):
            path = request if isinstance(request, str) else request.full_url
            if path.endswith('/health'): value = {'status': 'ok'}
            elif path.endswith('/apply-template'):
                captured.append(json.loads(request.data)['messages'][1]['content']); value = {'prompt': 'formatted'}
            elif path.endswith('/tokenize'): value = {'tokens': [1]}
            else: value = {'choices': [{'finish_reason': 'length', 'message': {'content': 'Partial'}}]}
            result = MagicMock(); result.__enter__.return_value = io.BytesIO(json.dumps(value).encode()); return result
        sock = MagicMock(); sock.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 1234)
        with patch('ai_worker.socket.socket', return_value=sock), patch('ai_worker.subprocess.Popen', return_value=process), patch('ai_worker.urllib.request.urlopen', side_effect=urlopen):
            worker._run('no phone'); worker._run('no radio'); worker._run('what now?')
        self.assertIn('No phone', captured[-1]); self.assertIn('No radio', captured[-1])
        self.assertIn('INCOMPLETE', worker.messages[-1][1])
        self.assertTrue(worker.submit('/forget no phone'))
        self.assertEqual(worker.constraints, ['No radio'])
        worker.clear(); self.assertEqual(worker.constraints, [])

    def test_standalone_failure_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp)/'fake'; executable.write_text('#!/bin/sh\nexit 7\n'); executable.chmod(0o700)
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                run_inference('x', 'unused', str(executable), timeout_sec=1)
            executable.write_text('#!/bin/sh\nexit 0\n')
            with self.assertRaisesRegex(RuntimeError, 'Empty or malformed'):
                run_inference('x', 'unused', str(executable), timeout_sec=1)
            executable.write_text('#!/bin/sh\nsleep 30\n')
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                run_inference('x', 'unused', str(executable), timeout_sec=.2)
            self.assertLess(time.monotonic()-start, 4)

    def test_literal_math(self):
        self.assertEqual(display_text('**Warning**: 2**3 and `x`'), 'Warning: 2**3 and x')


if __name__ == '__main__': unittest.main()
