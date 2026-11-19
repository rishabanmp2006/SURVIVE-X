import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app import Action, Screen, SURVIVEXApp
from hardware_sensors import LiveSensors
import journey as j
import navigation as nav
from places import PlacesDatabase
from test_stage6 import sentence


class JourneyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root/'journeys.db'
        PlacesDatabase(self.root/'places.db').close()

    def read(self, rec):
        out = self.root/(rec.id+'.json')
        j.export(self.path, rec.id, out)
        doc = json.loads(out.read_text())
        prev = j.ZERO
        events = []
        for i,(raw,h) in enumerate(doc['events'],1):
            self.assertEqual(j.digest(prev, raw), h)
            e=json.loads(raw)
            self.assertEqual(e['seq'], i)
            events.append(e)
            prev=h
        self.assertEqual(prev, doc['head'])
        self.assertEqual(len(events), doc['count'])
        return events

    def test_device_positions_times_sources_modes_privacy(self):
        rec=j.Recorder(self.path)
        sensors=LiveSensors()
        ui=SURVIVEXApp(str(self.root/'waypoints'),str(self.root/'places.db'),live_sensors=sensors,recorder=rec)
        try:
            ui.tick()
            ui.set_manual_coordinates(1,2)
            ui.tick()
            ui.sensor_mode=nav.SensorMode.LIVE_GNSS
            sensors.feed_nmea(sentence('GPGGA,181501,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,'))
            ui.tick()
            sensors.disconnect_gnss()
            ui.tick()
            rec.emit('ai_question','user',text='private question')
            ui.ai.messages=[('AI','private response')]
            ui.current_screen=Screen.WAYPOINTS
            ui.handle_action(Action.BACK)
            ui.tick()
        finally:
            ui.close()
        events=self.read(rec)
        positions=[e for e in events if e['kind']=='position']
        self.assertEqual([e['data']['source'] for e in positions],['SIMULATED','MANUAL COORDINATES','LIVE GNSS'])
        live=positions[-1]
        self.assertEqual((live['data']['receiver_utc'],live['data']['quality']),('181501',1))
        self.assertIsNone(live['data']['accuracy_m'])
        self.assertGreater(live['observed'],1e9)
        self.assertNotIn('private',str(events))
        self.assertTrue(any(e['kind']=='sensor' and not e['data']['valid'] for e in events))

    def test_abrupt_exit_restart_and_retention(self):
        code="""import os,sys,time,sqlite3
from journey import Recorder
r=Recorder(sys.argv[1],session='before')
r.emit('screen','user',screen='HOME')
while True:
    if r.error: sys.exit(1)
    try:
        with sqlite3.connect(sys.argv[1]) as db:
            if db.execute("select max(seq) from events where id='before'").fetchone()[0]==3: break
    except sqlite3.Error: pass
    time.sleep(.02)
os._exit(17)"""
        # Create the store before the subprocess opens a concurrent read connection.
        first=j.Recorder(self.path);first.close()
        run=subprocess.run([sys.executable,'-c',code,str(self.path)],timeout=5)
        self.assertEqual(run.returncode,17)
        rec=j.Recorder(self.path,ai=True)
        rec.emit('ai_response','AI unverified',text='example')
        rec.close()
        self.assertIn('example',str(self.read(rec)))
        with j.database(self.path) as db:
            rows=db.execute('select record from events where id=? order by seq',('before',)).fetchall()
            self.assertEqual(json.loads(rows[-1][0])['kind'],'screen')
            self.assertEqual(db.execute('select count(*) from journeys').fetchone()[0],3)

    def test_low_space_and_corruption(self):
        with patch('journey.RESERVE',10**20):
            rec=j.Recorder(self.path);rec.close()
        self.assertIn('Low disk space',rec.error)
        path=self.root/'damaged'
        path.write_bytes(b'unrecognized db')
        rec=j.Recorder(path);rec.close()
        self.assertTrue(rec.error)
        self.assertEqual(path.read_bytes(),b'unrecognized db')

    def test_nonblocking_and_demo_separation(self):
        import threading
        gate=threading.Event()
        original=j.shutil.disk_usage
        def slow(path):
            gate.wait(3)
            return original(path)
        with patch('journey.shutil.disk_usage',side_effect=slow):
            rec=j.Recorder(self.path)
            ui=SURVIVEXApp(str(self.root/'wp'),str(self.root/'places.db'),recorder=rec)
            try:
                start=time.monotonic()
                ui.tick();ui.handle_action(Action.DOWN)
                self.assertLess(time.monotonic()-start,.2)
                self.assertEqual(ui.home_menu_idx,1)
            finally:
                gate.set();ui.close()
        rec=j.Recorder(self.path,origin=j.DEMO);rec.close()
        self.assertIn('separate',rec.error)

    def test_stop_resume_and_delete_one_session(self):
        rec=j.Recorder(self.path)
        ui=SURVIVEXApp(str(self.root/'wp'),str(self.root/'places.db'),recorder=rec)
        try:
            ui.current_screen=Screen.STATUS
            ui.handle_action(Action.LEFT)
            rec.worker.join(timeout=2)
            ui.handle_action(Action.RIGHT)
            resumed=ui.recorder
            self.assertNotEqual(resumed.id,rec.id)
        finally:
            ui.close()
        self.assertNotIn('shutdown',[e['kind'] for e in self.read(rec)])
        self.assertNotIn('startup',[e['kind'] for e in self.read(resumed)])
        subprocess.run([sys.executable,'journey.py','delete',str(self.path),rec.id,'--confirm','DELETE'],check=True)
        with j.database(self.path) as db:
            self.assertEqual(db.execute('select distinct id from events').fetchall(),[(resumed.id,)])

    def test_sqlite_full_preserves_committed_session(self):
        rec=j.Recorder(self.path);rec.close()
        with patch('journey.LIMIT',self.path.stat().st_size):
            full=j.Recorder(self.path,ai=True)
            full.emit('ai_question','user / unverified AI chat',text='x'*60000)
            full.close()
        self.assertIn('full',full.error.lower())
        self.assertEqual(self.read(rec)[-1]['kind'],'journey_end')

    def test_ai_content_follows_session_of_submission(self):
        rec=j.Recorder(self.path,ai=True)
        ui=SURVIVEXApp(str(self.root/'wp'),str(self.root/'places.db'),recorder=rec)
        try:
            ui.ai.messages=[('AI','old answer')]
            ui.current_screen=Screen.ASSISTANT_INFO
            ui.ai_focus=1
            ui.ai_input='record this question'
            with patch.object(ui.ai,'submit',return_value=True):
                ui.handle_action(Action.SELECT)
            ui.ai.messages=[('You','record this question'),('AI','new answer')]
            ui.tick()
            ui.tick()
        finally:
            ui.close()
        events=self.read(rec)
        self.assertEqual([e['data']['text'] for e in events if e['kind'].startswith('ai_')],
                         ['record this question','new answer'])
