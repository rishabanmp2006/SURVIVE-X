"""Build flash replay and optionally emit one serial v1 replay (standard library)."""
import argparse
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
from app import HeadlessDisplayAdapter
from navigation import CompassReading, GPSReading, NavigationState, Waypoint
from places import PlacesDatabase

DB = ROOT / 'places_bengaluru.db'
QUERIES = ('Bengaluru', 'Bannerughatta')
# Clock, GPS sample time, heading sample time, fix, latitude, magnetic heading.
SAMPLES = [(0, 0, 0, True, 12.87, 0), (2000, 2000, 2000, True, 12.87, 90),
           (4000, 4000, 4000, False, 12.87, 90), (6000, 6000, 6000, True, 12.87, 90),
           (17000, 6000, 6000, True, 12.87, 90), (18000, 18000, 6000, True, 12.87, 90),
           (20000, 20000, 20000, True, 91, 45), (22000, 22000, 22000, True, 12.87, 45)]
FONT = 'gfx-classic-6x8-ascii'
WHITE, CYAN, AMBER = 65535, 2047, 64864


def compact(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def label(x, y, value, size=2, color=WHITE):
    assert value.isascii() and x + len(value)*6*size <= 320 and y + 8*size <= 240
    return ['t', x, y, size, color, value]


def display_value(name, value, unit):
    return f'{name} unavailable' if value is None else f'{name} {value:.0f} {unit}'


def scenes():
    db = PlacesDatabase(str(DB), read_only=True)
    try:
        places = [next(p for p in db.search_by_name(q) if p.name == q) for q in QUERIES]
    finally:
        db.close()
    frames, telemetry = [], []
    for p in places:
        target = Waypoint(p.name, p.lat, p.lon, source=p.source, place_id=p.id)
        for now, gt, ct, fix, lat, heading in SAMPLES:
            # Positive offset preserves true sample age at session time zero.
            gps = GPSReading(lat, 77.58, timestamp=1+gt/1000, accuracy_m=3, has_fix=fix)
            compass = CompassReading(heading, timestamp=1+ct/1000)
            state = NavigationState.from_readings(gps, compass, 0, 1+now/1000)
            sol = state.to_destination(target)
            ops = [label(8, 8, 'SURVIVE-X  /  GNSS REPLAY', 1, CYAN),
                   label(8, 30, target.display_name),
                   label(8, 60, 'GPS '+state.position_status.value, 1),
                   label(8, 84, display_value('Distance', sol.distance_m, 'm'), 1),
                   label(8, 105, display_value('Bearing', sol.bearing_deg, 'deg'), 1),
                   label(8, 126, display_value('Heading', state.true_heading, 'deg'), 1),
                   label(8, 156, sol.arrow_label.replace('_',' '), 1, AMBER),
                   label(8, 195, 'Left/right: sample   Select: places', 1),
                   label(8, 218, '(c) OpenStreetMap contributors', 1)]
            display = HeadlessDisplayAdapter()
            display.draw_nav_arrow(253, 113, 44, sol.turn_angle_deg, sol.status.name, sol.arrow_label)
            for op in display.draw_log:
                if op['type'] == 'polygon' and op['width'] == 0:
                    r, g, b = op['color']
                    ops.append(['p', (r >> 3) << 11 | (g >> 2) << 5 | b >> 3, *op['points']])
            frames.append(ops)
            telemetry.append(dict(t_ms=now, gps=dict(sample_ms=gt, lat=lat, lon=gps.lon, fix=fix,
                accuracy_m=3, altitude_m=None, hdop=None, satellites=None, mode='SIMULATED'),
                compass=dict(sample_ms=ct, magnetic_deg=heading, valid=True, calibrated=True, source='SIMULATED'),
                destination=dict(id=p.id,name=p.name,lat=p.lat,lon=p.lon,source=p.source,test_data=p.is_test_data,
                                 provenance={'osm_type':p.osm_type,'osm_id':p.osm_id}),
                nav=dict(status=sol.status.name, position_status=state.position_status.value,
                         mode='SIMULATED',destination_id=p.id,destination_name=p.name,source=p.source,
                         test_data=p.is_test_data,distance_m=sol.distance_m,bearing_deg=sol.bearing_deg,
                         true_heading_deg=state.true_heading,turn_angle_deg=sol.turn_angle_deg,
                         arrow=sol.arrow_label,sample_age_ms=now-gt)))
    for selected in range(len(places)):
        ops = [label(8, 10, 'OFFLINE PLACES'), label(8, 42, 'Exact name search results', 1, CYAN)]
        for i, p in enumerate(places):
            ops.append(label(8, 74 + i*32, ('> ' if i == selected else '  ') + p.display_name))
        ops += [label(8, 180, 'Up/down: choose   Select: navigate', 1),
                label(8, 200, 'Left/Back: cancel   Right: open', 1),
                label(8, 220, '(c) OpenStreetMap contributors', 1)]
        frames.append(ops)
    return frames, telemetry


def records(ops):
    return [compact(op) for op in ops]


def checksum(lines):
    return zlib.crc32(('\n'.join(lines)+'\n').encode())


def header(frames, telemetry):
    parts = ['// Generated by replay.py. All coordinates and navigation originate on the host.',
             '#pragma once', f'constexpr unsigned Samples = {len(SAMPLES)}, Places = {len(QUERIES)};',
              'constexpr uint64_t placeIds[] = {' + ', '.join(
                 str(row['destination']['id']) + 'ULL' for row in telemetry[::len(SAMPLES)]) + '};',
             'struct Frame { const char *ops; unsigned chunks; uint32_t crc; };',
             'const Frame frames[] = {']
    for ops in frames:
        lines = records(ops)
        assert all(len(line.encode()) <= 160 for line in lines)
        parts.append('  {'+json.dumps('\n'.join(lines)+'\n')+f', {len(lines)}, {checksum(lines)}u}},')
    return '\n'.join(parts+['};',''])


def stream(frames, telemetry):
    seq = 0
    def packet(t, kind, body):
        nonlocal seq
        seq += 1
        return compact(dict(v=1,session='demo',seq=seq,t_ms=t,kind=kind,body=body))
    yield packet(0, 'hello', dict(width=320,height=240,color='RGB565',font=FONT,
                 max_payload=160,watchdog_ms=1000,primitives=['t','p'],
                 font_source_sha256='bd6d60ecb9cc350a83f209d7c62de2fe5f6ce3402d5598cde2988b5b44e83e02',
                 database_sha256=hashlib.sha256(DB.read_bytes()).hexdigest(),mode='SIMULATED'))
    for step, sample in enumerate(SAMPLES):
        # Change to second destination during recovery.
        index = step if step < len(SAMPLES)-1 else len(SAMPLES)+step
        row = telemetry[index]
        t = sample[0]
        for kind in ('gps','compass','destination','nav'):
            yield packet(t,kind,row[kind])
        yield packet(t,'menu',dict(screen='NAVIGATION',selected=row['destination']['id'],
                                   page=0,busy=False,outcome='EXACT',coverage='IN_COVERAGE'))
        lines = records(frames[index])
        yield packet(t,'frame_begin',dict(frame_id=step+1,base_frame_id=None,chunks=len(lines)))
        for i, line in enumerate(lines):
            yield packet(t,'draw',dict(frame_id=step+1,index=i,payload=line))
        yield packet(t,'frame_end',dict(frame_id=step+1,chunks=len(lines),checksum=checksum(lines)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stream', action='store_true', help='print v1 JSONL instead of generating replay.h')
    parser.add_argument('--chat-replay', action='store_true', help='emit deterministic profile 2 AI chat JSONL')
    args = parser.parse_args()
    if args.chat_replay:
        import tempfile
        from app import SURVIVEXApp, Screen
        from display_adapter import ProtocolEncoder
        with tempfile.TemporaryDirectory() as tmp:
            app = SURVIVEXApp(storage_path=str(Path(tmp)/'waypoints.json'))
            try:
                app.current_screen = Screen.ASSISTANT_INFO
                app.ai.messages = [('You', 'What is a compass?'),
                    ('AI', 'A compass uses a magnetic needle to indicate direction.')]
                encoder = ProtocolEncoder('chat-replay')
                print(encoder.hello())
                print('\n'.join(encoder.frame(app)))
            finally:
                app.close()
        sys.exit(0)
    frames, telemetry = scenes()
    if args.stream:
        print('\n'.join(stream(frames, telemetry)))
    else:
        (HERE/'replay.h').write_text(header(frames, telemetry))
        print(f'{len(frames)} frames; database SHA256 {hashlib.sha256(DB.read_bytes()).hexdigest()}')
