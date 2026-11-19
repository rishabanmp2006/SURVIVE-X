import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

import navigation as nav
from app import Action, Screen, SURVIVEXApp
from hardware_sensors import LiveSensors, NMEAReader
from unoq_bridge import BridgeLink, PART_BYTES


def sentence(content):
    checksum=0
    for ch in content: checksum ^= ord(ch)
    return f'${content}*{checksum:02X}\r\n'.encode()


class MockRouter:
    def __init__(self):
        self.available=True
        self.handler=None
        self.parts=[]
        self.frames=[]
        self.baseline=0
        self.session=''
        self.sequence=0
        self.screen=''
        self.chunks=[]
        self.seq=0
        self.drop=False
        self.connected_event=threading.Event()
    def provide(self,name,callback):
        assert name=='sx_event'
        self.handler=callback
    def connect(self,timeout):
        self.connected_event.set()
        return self.available
    def disconnect(self): pass
    def emit(self,kind,session='',fid=0,action=''):
        self.seq+=1
        self.handler(kind,session,fid,action,self.seq)
    def call(self,name,index,count,piece,timeout):
        if not self.available: raise ConnectionError('link down')
        assert name=='sx_part' and len(piece.encode())<=PART_BYTES and 1<=count<=8
        if index==0: self.parts=[]
        assert index==len(self.parts)
        self.parts.append(piece)
        if index+1<count: return True
        message=json.loads(''.join(self.parts))
        if message['kind']=='hello':
            self.session=message['session'];self.sequence=message['seq'];self.baseline=0
            self.emit('resync',action='WAITING FOR FRAME')
        else:
            assert message['session']==self.session and message['seq']==self.sequence+1
            self.sequence=message['seq']
            body=message['body']
            if message['kind']=='frame_begin':
                if body['mode']=='DELTA': assert body['base_frame_id']==self.baseline
                self.current=body;self.chunks=[]
            elif message['kind']=='draw':
                assert body['index']==len(self.chunks)
                self.chunks.append(body['payload'])
            elif message['kind']=='frame_end':
                base=self.current['base_frame_id'] or 0
                data=self.current['screen']+'\n'+self.current['mode']+'\n'+str(base)+'\n'+'\n'.join(self.chunks)+'\n'
                assert body['checksum']==zlib.crc32(data.encode())
                self.baseline=body['frame_id'];self.frames.append(self.current['mode'])
                if not self.drop: self.emit('ack',self.session,self.baseline)
            elif message['kind']=='ping':
                assert body['frame_id']==self.baseline
                self.emit('ack',self.session,self.baseline)
        return True


def wait_for(link, app, condition, timeout=4):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        link.update(app)
        if condition(): return
        time.sleep(.005)
    raise AssertionError((link.error,link.active,link.sending,link.encoder.pending if link.encoder else None))


class HardwareTests(unittest.TestCase):
    def test_recorded_gnss_compass_loss_recovery(self):
        now=[time.time()]; sensors=LiveSensors(clock=lambda:now[0])
        with tempfile.TemporaryDirectory() as tmp:
            ui=SURVIVEXApp(storage_path=str(Path(tmp)/'wp'),live_sensors=sensors,live_declination=None)
            ui.sensor_mode=nav.SensorMode.LIVE_GNSS
            self.assertFalse(ui.navigation_state().has_position)
            line=sentence('GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,')
            self.assertEqual(sensors.feed_nmea(line[:12]),0)
            self.assertEqual(sensors.feed_nmea(line[12:]),1)
            self.assertTrue(ui.navigation_state().has_position)
            self.assertAlmostEqual(sensors.gps().lat,48.1173)
            self.assertEqual(sensors.gps().hdop,.9)
            self.assertTrue(sensors.compass_sample(90,True))
            self.assertIsNone(ui.navigation_state().true_heading)
            ui.live_declination=5
            self.assertEqual(ui.navigation_state().true_heading,95)
            sensors.feed_nmea(line[:-4]+b'00\r\n')
            self.assertTrue(ui.navigation_state().has_position)
            now[0]+=6
            with patch('navigation.time.time',return_value=now[0]):
                self.assertIsNone(ui.navigation_state().true_heading)
            now[0]+=6
            with patch('navigation.time.time',return_value=now[0]):
                self.assertFalse(ui.navigation_state().has_position)
            sensors.feed_nmea(sentence('GPGGA,123520,,,,,0,00,,,,,,,'))
            self.assertFalse(sensors.gps().has_fix)
            sensors.feed_nmea(line)
            self.assertTrue(sensors.gps().has_fix)
            sensors.disconnect_gnss(); sensors.disconnect_compass()
            self.assertFalse(sensors.gps().has_fix);self.assertIsNone(sensors.compass())
            self.assertTrue(sensors.compass_sample(40,False))
            self.assertFalse(sensors.compass().is_calibrated)
            self.assertTrue(sensors.compass_sample(40,True))
            self.assertTrue(sensors.compass().is_calibrated)
            ui.close()

    def test_gnss_pseudoterminal_acquisition(self):
        import os
        import pty
        master,slave=pty.openpty()
        sensors=LiveSensors()
        reader=NMEAReader(sensors,os.ttyname(slave),9600)
        try:
            reader.start()
            line=sentence('GNGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,')
            end=time.monotonic()+3
            while time.monotonic()<end and not sensors.gps().has_fix:
                os.write(master,line)
                time.sleep(.05)
            self.assertTrue(sensors.gps().has_fix, reader.error)
            os.close(master);master=None
            end=time.monotonic()+3
            while time.monotonic()<end and sensors.gps().has_fix:
                time.sleep(.05)
            self.assertFalse(sensors.gps().has_fix)
        finally:
            reader.close()
            if master is not None: os.close(master)
            os.close(slave)

    def test_bridge_sync_delta_actions_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=SURVIVEXApp(storage_path=str(Path(tmp)/'wp'))
            router=MockRouter(); link=BridgeLink(lambda:router)
            try:
                wait_for(link,ui,lambda:bool(router.frames) and link.encoder.baseline is not None)
                self.assertEqual(router.frames[-1],'FULL')
                router.emit('action',action='DOWN')
                wait_for(link,ui,lambda:'DELTA' in router.frames)
                self.assertEqual(ui.home_menu_idx,1)
                old_session=link.encoder.session
                router.emit('boot')
                wait_for(link,ui,lambda:link.encoder.session!=old_session and link.encoder.baseline is not None)
                self.assertEqual(router.frames[-1],'FULL')
                old_session=link.encoder.session
                router.available=False
                ui.handle_action(Action.DOWN);link.update(ui,True)
                wait_for(link,ui,lambda:bool(link.error))
                t=time.monotonic();ui.tick();self.assertLess(time.monotonic()-t,.1)
                router.available=True
                wait_for(link,ui,lambda:link.encoder.session!=old_session and link.encoder.baseline is not None)
                self.assertEqual(router.frames[-1],'FULL')
            finally: link.close();ui.close()

    def test_lost_ack_forces_new_full_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=SURVIVEXApp(storage_path=str(Path(tmp)/'wp'))
            router=MockRouter(); router.drop=True
            now=[0.0]; link=BridgeLink(lambda:router,clock=lambda:now[0])
            try:
                wait_for(link,ui,lambda:bool(router.frames) and not link.sending)
                self.assertIsNone(link.encoder.baseline)
                old_session=link.encoder.session
                router.drop=False;now[0]=5.0
                wait_for(link,ui,lambda:link.encoder.session!=old_session and link.encoder.baseline is not None)
                self.assertEqual(router.frames[-1],'FULL')
            finally:link.close();ui.close()

    def test_ai_unavailable_does_not_block_live_navigation(self):
        from ai_worker import ChatWorker
        with tempfile.TemporaryDirectory() as tmp:
            sensors=LiveSensors()
            sensors.feed_nmea(sentence('GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,'))
            ui=SURVIVEXApp(storage_path=str(Path(tmp)/'wp'),live_sensors=sensors)
            ui.sensor_mode=nav.SensorMode.LIVE_GNSS
            ui.ai.close();ui.ai=ChatWorker('/missing','/missing')
            ui.ai.submit('test');ui.ai.thread.join(1)
            self.assertEqual(ui.ai.status,'ERROR')
            self.assertTrue(ui.navigation_state().has_position)
            ui.current_screen=Screen.NAVIGATION
            ui.tick()
            self.assertEqual(ui.current_screen,Screen.NAVIGATION)
            ui.close()

    def test_bridge_unavailable_does_not_block(self):
        router=MockRouter(); router.available=False
        with tempfile.TemporaryDirectory() as tmp:
            ui=SURVIVEXApp(storage_path=str(Path(tmp)/'wp'))
            link=BridgeLink(lambda:router)
            try:
                self.assertTrue(router.connected_event.wait(1))
                start=time.monotonic()
                for _ in range(20): ui.tick();link.update(ui)
                self.assertLess(time.monotonic()-start,.2)
                self.assertFalse(link.active)
            finally:link.close();ui.close()

if __name__=='__main__': unittest.main()
