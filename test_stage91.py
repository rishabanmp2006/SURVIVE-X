import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import journey
from hardware_sensors import LiveSensors
from test_stage71 import gga


class Reliability(unittest.TestCase):
    def test_hot_journal_recovery_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'evidence.db'
            r=journey.Recorder(path); r.close()
            code='''import sqlite3,os,sys
c=sqlite3.connect(sys.argv[1]);c.execute('pragma cache_size=2');c.execute('begin immediate');c.execute("update journeys set head=?",('X'*64,));c.execute('create table big(x)');c.executemany('insert into big values(?)',[('x'*3000,)]*1000);os._exit(0)'''
            subprocess.run([sys.executable,'-c',code,str(path)],check=True)
            journal=Path(str(path)+'-journal');self.assertTrue(journal.exists())
            before={p:p.read_bytes() for p in (path,journal)}
            out=Path(tmp)/'out.json';journey.export(path,r.id,out)
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            self.assertTrue(out.exists())

    def test_reacquisition_and_midnight(self):
        now=[0];s=LiveSensors(monotonic=lambda:now[0])
        self.assertEqual(s.feed_nmea(gga(t='235959')),1)
        self.assertEqual(s.feed_nmea(gga(t='235959')),0)
        self.assertEqual(s.feed_nmea(gga(t='000000')),1)
        now[0]+=50000
        self.assertEqual(s.feed_nmea(gga(t='140000')),0)
        self.assertEqual(s.gnss_state,'reacquiring')
        self.assertFalse(s.gps().has_fix)
        self.assertEqual(s.feed_nmea(gga(t='140000')),0)
        self.assertEqual(s.feed_nmea(gga(t='140001')),1)
        s.disconnect_gnss();self.assertEqual(s.gnss_state,'disconnected')
        self.assertEqual(s.feed_nmea(gga(t='123400')),0)
        self.assertEqual(s.feed_nmea(gga(t='123401')),1)
        self.assertEqual(s.feed_nmea(gga(t='250000')),0)

    def test_queue_priority_and_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=threading.Event();original=journey.shutil.disk_usage
            def slow(p): gate.wait(3);return original(p)
            with patch('journey.shutil.disk_usage',side_effect=slow):
                r=journey.Recorder(Path(tmp)/'j')
                for _ in range(400):r.emit('checkpoint','observation',source='SIMULATED',valid=False)
                r.emit('destination','device',name='important',coordinates=[1,2],source='test')
                self.assertTrue(r.degraded)
                gate.set();r.close()
            self.assertGreater(r.losses.get('checkpoint',0),0)
            with journey.database(r.path) as db:
                rows=[__import__('json').loads(x[0]) for x in db.execute('select record from events order by seq')]
            self.assertTrue(any(e['kind']=='destination' for e in rows))
            self.assertTrue(any(e['kind']=='logging_error' and e['data'].get('counts') for e in rows))

    def test_wal_recovery_and_corrupt_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'wal.db'
            r=journey.Recorder(path);r.close()
            code='''import sqlite3,os,sys
c=sqlite3.connect(sys.argv[1]);c.execute('pragma journal_mode=wal');c.execute('create table wal_fixture(x)');c.execute('insert into wal_fixture values(42)');c.commit();os._exit(0)'''
            subprocess.run([sys.executable,'-c',code,str(path)],check=True)
            wal=Path(str(path)+'-wal');self.assertTrue(wal.exists())
            evidence={p:p.read_bytes() for p in (path,wal)}
            with journey.recovery_copy(path) as copy, sqlite3.connect(copy) as db:
                self.assertEqual(db.execute('select x from wal_fixture').fetchone()[0],42)
            self.assertEqual(evidence,{p:p.read_bytes() for p in evidence})
            path.write_bytes(b'bad database')
            with self.assertRaises((ValueError,sqlite3.Error)):
                with journey.recovery_copy(path): pass

    def test_bounded_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=threading.Event();original=journey.shutil.disk_usage
            def slow(p): gate.wait(5);return original(p)
            with patch('journey.shutil.disk_usage',side_effect=slow):
                r=journey.Recorder(Path(tmp)/'j')
                time.sleep(.1);r.emit('screen','user',screen='NAVIGATION')
                started=time.monotonic();r.close()
                self.assertLess(time.monotonic()-started,3)
                self.assertIn('Shutdown timed out',r.error)
                self.assertGreaterEqual(r.losses.get('screen',0),1)
                gate.set();r.worker.join(2)
