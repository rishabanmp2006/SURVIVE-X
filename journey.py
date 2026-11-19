"""Local journey recorder and owner-operated export utility (standard library only)."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid

APP_ID = 0x53584A31
ZERO = '0' * 64
DEMO = 'SYNTHETIC TEST DATA'
DEVICE = 'DEVICE'
LIMIT = 64 * 1024 * 1024
RESERVE = 16 * 1024 * 1024
MAX_EVENTS = 20000
MAX_BYTES = 12 * 1024 * 1024


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(previous, record):
    return hashlib.sha256((previous + record).encode()).hexdigest()


@contextlib.contextmanager
def database(path, origin=None, delete=False):
    """An exclusive writer lock also keeps deletion away from live sessions."""
    path = Path(path).resolve()
    if origin is not None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    write = origin is not None or delete
    guard = os.fdopen(os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600), 'rb') if write else contextlib.nullcontext()
    with guard as lock:
        if write:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mode = 'rw' if origin is not None or delete else 'ro'
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, timeout=1)) as db:
            app_id = db.execute('pragma application_id').fetchone()[0]
            if origin is not None and not app_id and path.stat().st_size == 0:
                db.executescript(f"""BEGIN;
                    PRAGMA application_id={APP_ID};
                    PRAGMA user_version=1;
                    CREATE TABLE config(origin TEXT NOT NULL);
                    CREATE TABLE journeys(id TEXT PRIMARY KEY, n INTEGER, head TEXT);
                    CREATE TABLE events(id TEXT, seq INTEGER, record TEXT, hash TEXT,
                                        PRIMARY KEY(id,seq)) WITHOUT ROWID;
                """)
                db.execute('insert into config values(?)', (origin,))
                db.commit()
            if db.execute('pragma application_id').fetchone()[0] != APP_ID or db.execute('pragma user_version').fetchone()[0] != 1:
                raise ValueError('Unrecognized journey database')
            if db.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise ValueError('Database integrity check failed')
            if origin and db.execute('select origin from config').fetchone()[0] != origin:
                raise ValueError('Synthetic and device stores must be separate')
            yield db


class Recorder:
    def __init__(self, path, interval=30, ai=False, *, origin=DEVICE, clock=time.time, session=None, resumed=False):
        if not 1 <= interval <= 3600:
            raise ValueError('Checkpoint interval must be 1–3600 seconds')
        self.path, self.interval, self.ai = Path(path), interval, ai
        self.origin, self.clock = origin, clock
        self.id = session or uuid.uuid4().hex
        self.error = ''
        self.ready = False
        self.closed = False
        self.pending = queue.Queue(256)
        self.finished = threading.Event()
        self.sensor = self.destination = self.mode = None
        self.last_sample = -math.inf
        self.worker = threading.Thread(target=self._write, name='journey-writer', daemon=True)
        if not resumed:
            self.emit('startup', 'device', scope='application')
        self.emit('journey_start', 'device', interval=interval, ai_content=ai)
        self.worker.start()

    def emit(self, kind, actor, **data):
        if self.closed or self.error:
            return
        if kind in ('ai_question', 'ai_response') and not self.ai:
            return
        try:
            self.pending.put_nowait((self.clock(), kind, actor, data))
        except queue.Full:
            self.error = 'Recorder queue full'

    def observe(self, state, destination, mono=None):
        mono = time.monotonic() if mono is None else mono
        gps = state.gps
        mode = gps.sensor_mode.value
        if mode != self.mode:
            self.emit('mode', 'device', source=mode)
            self.mode = mode
        sensor = (mode, state.position_status.value, state.true_heading is not None)
        changed = sensor != self.sensor
        if changed:
            self.emit('sensor', 'observation', source=mode, valid=state.has_position,
                      status=state.position_status.value, heading_available=sensor[2])
            self.sensor = sensor
        dest = (destination.display_name, destination.lat, destination.lon, destination.source) if destination else ()
        if dest != self.destination:
            self.emit('destination', 'device', name=dest[0] if dest else None,
                      coordinates=list(dest[1:3]) if dest else None, source=dest[3] if dest else None)
            self.destination = dest
        if changed or mono - self.last_sample >= self.interval:
            self.last_sample = mono
            if state.has_position:
                self.emit('position', 'observation', source=mode, lat=gps.lat, lon=gps.lon,
                          sample_time=gps.timestamp, receiver_utc=gps.receiver_utc,
                          quality=gps.fix_quality, accuracy_m=gps.accuracy_m, hdop=gps.hdop,
                          satellites=gps.satellites)
            else:
                self.emit('checkpoint', 'observation', source=mode, valid=False)

    def _write(self):
        try:
            with database(self.path, self.origin) as db:
                db.execute('pragma synchronous=FULL')
                db.execute('pragma max_page_count=%d' % (LIMIT // db.execute('pragma page_size').fetchone()[0]))
                previous, seq, size = ZERO, 0, 0
                with db:
                    db.execute('insert into journeys values(?,0,?)', (self.id, ZERO))
                self.ready = True
                while not self.finished.is_set() or not self.pending.empty():
                    try:
                        batch = [self.pending.get(timeout=.15)]
                    except queue.Empty:
                        continue
                    while len(batch) < 64 and not self.pending.empty():
                        batch.append(self.pending.get_nowait())
                    if shutil.disk_usage(self.path.parent).free < RESERVE:
                        self.error = 'Low disk space'
                        batch = []  # Use the reserve only for the terminal error marker.
                    if self.error:
                        batch.append((self.clock(), 'logging_error', 'device', {'reason': self.error}))
                    with db:
                        for observed, kind, actor, data in batch:
                            seq += 1
                            record = encode(dict(id=self.id, origin=self.origin, seq=seq, observed=observed,
                                                 kind=kind, actor=actor, data=data))
                            size += len(record)
                            if seq > MAX_EVENTS or size > MAX_BYTES:
                                raise OSError('Journey storage limit reached')
                            previous = digest(previous, record)
                            db.execute('insert into events values(?,?,?,?)', (self.id, seq, record, previous))
                        db.execute('update journeys set n=?,head=? where id=?', (seq, previous, self.id))
                    if self.error:
                        break
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.error = str(exc)
        finally:
            if self.error:
                print('Journey recording stopped:', self.error, file=sys.stderr, flush=True)

    def stop(self):
        if not self.closed:
            self.emit('journey_end', 'device')
            self.closed = True
            self.finished.set()

    def close(self):
        self.emit('shutdown', 'device', scope='application')
        self.stop()
        self.worker.join()


def export(path, session, output):
    with database(path) as db:
        db.execute('begin')
        meta = db.execute('select n,head from journeys where id=?', (session,)).fetchone()
        if not meta:
            raise ValueError('Unknown journey')
        rows = db.execute('select record,hash from events where id=? order by seq', (session,)).fetchall()
        origin = db.execute('select origin from config').fetchone()[0]
    doc = dict(version=1, id=session, origin=origin, count=meta[0], head=meta[1], events=rows)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(doc, stream, separators=(',', ':'))


def add_options(parser):
    parser.add_argument('--record-journey', metavar='NEW_OR_EXISTING_JOURNEY_DB',
                        help='Enable local recording of positions, destinations, actions and searches')
    parser.add_argument('--record-ai', action='store_true', help='Also save private AI question and reply text')
    parser.add_argument('--checkpoint', type=int, default=30, help='Position checkpoint seconds (1–3600)')


def configured(args):
    if args.record_journey:
        return Recorder(args.record_journey, args.checkpoint, args.record_ai)
    return None


def demo(path):
    import navigation as nav
    now = [1760000000]
    rec = Recorder(path, origin=DEMO, clock=lambda: now[0], session='synthetic-trek-01')
    for i in range(8):
        now[0] += 120 if i == 7 else 30
        gps = nav.GPSReading(12.95+i*.0008, 77.58+i*.0006, timestamp=now[0], has_fix=i not in (3,4),
                             accuracy_m=6, hdop=1.1, satellites=8, sensor_mode=nav.SensorMode.SIMULATED,
                             receiver_utc=time.strftime('%H%M%S', time.gmtime(now[0])), fix_quality=8)
        state = nav.NavigationState.from_readings(gps, None, None, now[0])
        rec.observe(state, nav.Waypoint('Ridge shelter' if i<2 else 'Trail hut', 12.97, 77.59, source=DEMO), now[0])
        if i == 1:
            rec.emit('search', 'user', query='water', category='water')
    # End the fixture at the last durable observation with no shutdown/end event.
    rec.finished.set()
    rec.worker.join()
    if rec.error:
        raise ValueError(rec.error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for cmd in ('demo', 'list', 'export', 'delete'):
        p = sub.add_parser(cmd)
        p.add_argument('path', type=Path)
        if cmd == 'demo':
            p.add_argument('--store', type=Path, help='Keep the synthetic SQLite store for later recovery')
        if cmd in ('export','delete'):
            p.add_argument('session')
        if cmd == 'export':
            p.add_argument('output', type=Path)
        if cmd == 'delete':
            p.add_argument('--confirm', required=True, choices=['DELETE'])
    args = parser.parse_args()
    if args.command == 'demo':
        with tempfile.TemporaryDirectory() as tmp:
            path = args.store or Path(tmp)/'synthetic.sqlite'
            demo(path)
            export(path, 'synthetic-trek-01', args.path)
    elif args.command == 'export':
        export(args.path, args.session, args.output)
    elif args.command == 'list':
        with database(args.path) as db:
            for sid, count in db.execute('select id,n from journeys order by rowid'):
                print(sid, count)
    else:
        with database(args.path, delete=True) as db, db:
            if not db.execute('delete from journeys where id=?', (args.session,)).rowcount:
                raise ValueError('Unknown journey')
            db.execute('delete from events where id=?', (args.session,))


if __name__ == '__main__':
    try:
        main()
    except (sqlite3.Error, OSError, ValueError) as exc:
        sys.exit(str(exc))
