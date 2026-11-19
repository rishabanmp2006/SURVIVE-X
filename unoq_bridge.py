"""Non-blocking profile 3 host over Arduino Router Bridge RPC.

The MCU endpoint described in STAGE6.md is still scaffolding. This module can be
exercised with an injected mock without installing the Arduino target SDK.
"""
import json
import queue
import threading
import time
import uuid

from wokwi.display_adapter import ActionReceiver, IncrementalEncoder

PART_BYTES = 80
MAX_RECORD_BYTES = 639
MAX_TRANSACTION_RECORDS = 130


class BridgeLink:
    def __init__(self, bridge_factory=None, clock=time.monotonic):
        self.clock = clock
        self.bridge_factory = bridge_factory or self._official_bridge
        self.events = queue.Queue(maxsize=128)
        self.work = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.encoder = None
        self.actions = ActionReceiver()
        self.dirty = True
        self.active = False
        self.connected = False
        self.sending = False
        self.last_send = 0.0
        self.last_contact = 0.0
        self.error = 'Bridge starting'
        self.thread.start()

    @staticmethod
    def _official_bridge():
        from arduino.router_bridge import Bridge
        return Bridge()

    def _event(self, kind, session, frame_id, action, seq):
        # RPC handler performs no nested Bridge calls and never blocks.
        if (kind not in ('ack', 'resync', 'action', 'boot') or not isinstance(session,str)
                or len(session)>23 or type(frame_id) is not int or not 0 <= frame_id <= 0xffffffff
                or type(seq) is not int or not 0 < seq <= 0xffffffff
                or not isinstance(action,str) or len(action)>16):
            return False
        try:
            self.events.put_nowait(('event', kind, session, frame_id, action, seq))
            return True
        except queue.Full:
            # A dropped event makes the displayed baseline uncertain.
            self.stop.set()
            return False

    def _worker(self):
        bridge = None
        try:
            bridge = self.bridge_factory()
            bridge.provide('sx_event', self._event)
            while not self.stop.is_set():
                if not bridge.connect(timeout=0.5):
                    self.stop.wait(0.5)
                    continue
                self.events.put(('connected',))
                while not self.stop.is_set():
                    try:
                        records = self.work.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    try:
                        for record in records:
                            raw = record.encode('ascii')
                            if len(raw)>MAX_RECORD_BYTES:
                                raise ValueError('record exceeds receiver buffer')
                            pieces = [raw[i:i+PART_BYTES].decode('ascii') for i in range(0,len(raw),PART_BYTES)]
                            for index,piece in enumerate(pieces):
                                if bridge.call('sx_part', index, len(pieces), piece, timeout=0.5) is not True:
                                    raise RuntimeError('MCU rejected fragment')
                        self.events.put(('sent',))
                    except Exception as exc:
                        self.events.put(('failed',str(exc)[:160]))
                        self.stop.wait(0.5)
                        break
        except Exception as exc:
            try: self.events.put_nowait(('failed',str(exc)[:160]))
            except queue.Full: pass
        finally:
            if bridge is not None:
                try: bridge.disconnect()
                except Exception: pass

    def _new_session(self):
        self.encoder = IncrementalEncoder('sx-' + uuid.uuid4().hex[:16])
        self.actions = ActionReceiver()
        self.active = True
        self.dirty = True
        self.last_contact = self.last_send = self.clock()
        self._send([self.encoder.hello()])

    def _send(self, records):
        if self.sending or not records or len(records)>MAX_TRANSACTION_RECORDS:
            return False
        try:
            self.work.put_nowait(records)
        except queue.Full:
            return False
        self.sending = True
        self.last_send = self.clock()
        return True

    def update(self, app, dirty=False):
        """Called by the app loop; all RPC calls execute on the worker thread."""
        self.dirty = self.dirty or dirty
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            kind = event[0]
            if kind == 'connected':
                self.connected = True
                self.active = False
                self.sending = False
                self._new_session()
                self.error = ''
            elif kind == 'sent':
                self.sending = False
            elif kind == 'failed':
                self.connected = False
                self.active = self.sending = False
                self.error = event[1]
            elif kind == 'event' and self.encoder:
                _, event_kind, session, frame_id, action, seq = event
                if event_kind == 'ack' and session == self.encoder.session:
                    self.encoder.acknowledge(frame_id)
                    self.last_contact = self.clock()
                elif event_kind in ('resync','boot'):
                    if event_kind == 'resync' and action == 'WAITING FOR FRAME':
                        continue
                    # Do not interrupt an in-flight transaction. Reconnect after
                    # it finishes so old fragments cannot contaminate a session.
                    self.active = False
                    self.dirty = True
                elif event_kind == 'action':
                    try:
                        line=json.dumps({'v':1,'session':'adapter','seq':seq,'kind':'action',
                                         'body':{'action':action}})
                        self.actions.receive(app,line)
                        self.dirty = True
                    except (ValueError, KeyError):
                        pass
        if self.stop.is_set():
            self.active = False
            if not self.error: self.error = 'Bridge event queue overflow or stopped'
            return
        if self.connected and not self.active and not self.sending and self.encoder:
            self._new_session()
        if not self.active or self.sending or not self.encoder:
            return
        now = self.clock()
        if ((self.encoder.pending and now-self.last_send>4) or
                (self.encoder.baseline and now-self.last_contact>4)):
            self._new_session()
        elif self.dirty and not self.encoder.pending:
            packets = self.encoder.frame(app,int(now*1000)&0xffffffff)
            if packets:
                self._send(packets)
            self.dirty = False
        elif self.encoder.baseline and not self.encoder.pending and now-self.last_send>=0.4:
            self._send([self.encoder.ping(int(now*1000)&0xffffffff)])

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
