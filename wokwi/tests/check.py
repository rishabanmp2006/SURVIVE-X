"""Run from any directory. ARDUINOJSON_SRC can override the installed header path."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
import replay

frames, telemetry = replay.scenes()
assert replay.header(frames, telemetry) == (TESTS.parent/'replay.h').read_text()
assert telemetry[0]['nav']['bearing_deg'] == telemetry[1]['nav']['bearing_deg']
assert telemetry[1]['nav']['turn_angle_deg'] != telemetry[0]['nav']['turn_angle_deg']
assert telemetry[5]['nav']['bearing_deg'] is not None and telemetry[5]['nav']['turn_angle_deg'] is None
assert telemetry[5]['nav']['distance_m'] is not None
for i in (2, 4, 6):
    assert telemetry[i]['nav']['distance_m'] is None and telemetry[i]['nav']['bearing_deg'] is None
packets = [json.loads(s) for s in replay.stream(frames, telemetry)]
assert max(len(replay.compact(p)) for p in packets) < 640
assert packets == [json.loads(s) for s in replay.stream(*replay.scenes())]
with tempfile.TemporaryDirectory() as tmp:
    binary = Path(tmp)/'receiver'
    includes = os.environ.get('ARDUINOJSON_SRC', Path.home()/'Documents/Arduino/libraries/ArduinoJson/src')
    subprocess.run(['c++','-std=c++17','-I',str(TESTS),'-I',str(includes),str(TESTS/'receiver.cpp'),
                    '-o',str(binary)],check=True)

    def run(lines):
        return subprocess.run([binary],input='\n'.join(lines)+'\n',capture_output=True,
                              text=True,check=True).stdout.splitlines()

    encode = replay.compact
    expected = [f'{i+1} 0 {flag}' for i,flag in enumerate([1,1,0,1,0,0,0,1])]
    good = list(map(encode, packets))
    assert run(good+['TIMEOUT']) == expected+['8 1 0']
    for fault in ('missing','checksum','version','out_of_bounds'):
        broken=[]
        for p in json.loads(json.dumps(packets)):
            b=p['body']
            if b.get('frame_id')==2:
                if p['kind']=='draw' and b['index']==0:
                    if fault=='missing': continue
                    if fault=='version': p['v']=2
                    if fault=='out_of_bounds': b['payload']='["t",320,0,1,1,"X"]'
                if p['kind']=='frame_end' and fault=='checksum': b['checksum']+=1
            broken.append(encode(p))
        assert run(broken)==[expected[0],'1 1 0',*expected[2:]], fault
    # Open Places half way through frame 1. Complete two frames behind the menu,
    # then cancel while the latest committed frame is still fresh.
    interleaved=[]
    for p in packets:
        interleaved.append(encode(p))
        if p['kind']=='draw' and p['body']['frame_id']==1 and p['body']['index']==0:
            interleaved += ['OPEN', 'DOWN']
        if p['kind']=='frame_end' and p['body']['frame_id'] in (1,2):
            interleaved += ['MENU?']
        if p['kind']=='frame_end' and p['body']['frame_id']==2:
            interleaved += ['BACK']
    assert run(interleaved)==['1 0 0','2 0 0',*expected[2:]]
    # Pick the second destination during frame 1. Frames for the first one
    # still get ACKs, but only the last (matching) recovery frame can paint.
    pinned=[]
    for p in packets:
        pinned.append(encode(p))
        if p['kind']=='draw' and p['body']['frame_id']==1 and p['body']['index']==0:
            pinned += ['OPEN', 'DOWN', 'PICK']
    assert run(pinned)==[f'{i} 0 {int(i==8)}' for i in range(1,9)]
    duplicate=good.copy()
    duplicate.insert(20,good[19])
    assert run(duplicate)==expected
print('PASS: flash and JSONL replay, 6 controls, menu, bounds, watchdog, corrupt/drop/version/duplicate handling and recovery')
