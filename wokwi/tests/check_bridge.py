"""Exercise acknowledged profile 3 transfers through a POSIX pseudo-terminal."""
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time

master, slave = pty.openpty()
env = dict(os.environ, SDL_VIDEODRIVER='dummy', PYGAME_HIDE_SUPPORT_PROMPT='1')
with tempfile.TemporaryDirectory() as tmp:
    proc = subprocess.Popen([sys.executable, 'wokwi/serial_bridge.py', '--port', os.ttyname(slave),
                             '--waypoints', tmp+'/wp.json', '--scale', '1'],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    data = b''
    current = None
    session = None
    action_seq = 0
    seen = []
    start = None
    latency = None
    try:
        deadline = time.monotonic()+15
        while time.monotonic()<deadline and latency is None:
            if not select.select([master],[],[],0.05)[0]:
                continue
            data += os.read(master,65536)
            while b'\n' in data:
                line, _, data = data.partition(b'\n')
                try:
                    packet = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                kind, body = packet['kind'], packet['body']
                if kind=='hello': session=packet['session']
                elif kind=='frame_begin': current=body; seen.append((body['screen'],body['mode']))
                elif kind=='frame_end':
                    ack={'v':1,'session':'adapter','seq':100+len(seen),'kind':'ack',
                         'body':{'session':session,'frame_id':body['frame_id']}}
                    os.write(master,(json.dumps(ack)+'\n').encode())
                    if current['screen']=='HOME':
                        for action in ('UP','SELECT'):
                            action_seq+=1
                            event={'v':1,'session':'adapter','seq':action_seq,'kind':'action','body':{'action':action}}
                            os.write(master,(json.dumps(event)+'\n').encode())
                    elif current['screen']=='AI_CHAT' and current['mode']=='FULL':
                        action_seq+=1; start=time.monotonic()
                        event={'v':1,'session':'adapter','seq':action_seq,'kind':'action','body':{'action':'RIGHT'}}
                        os.write(master,(json.dumps(event)+'\n').encode())
                    elif current['screen']=='AI_CHAT' and current['mode']=='DELTA' and start:
                        latency=(time.monotonic()-start)*1000
        assert latency is not None, seen
        print('PASS pseudo-terminal FULL/ACK/DELTA; action-to-last-byte %.1f ms' % latency)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        os.close(master)
        os.close(slave)

# Drop the first FULL acknowledgment, then simulate a receiver reboot.
master, slave = pty.openpty()
with tempfile.TemporaryDirectory() as tmp:
    proc = subprocess.Popen([sys.executable, 'wokwi/serial_bridge.py', '--port', os.ttyname(slave),
                             '--waypoints', tmp+'/wp.json', '--scale', '1'],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    data=b''; sessions=[]; stage=0
    try:
        deadline=time.monotonic()+12
        while time.monotonic()<deadline and stage<3:
            if not select.select([master],[],[],0.05)[0]: continue
            data+=os.read(master,65536)
            while b'\n' in data:
                line,_,data=data.partition(b'\n')
                try: packet=json.loads(line)
                except (ValueError, UnicodeError): continue
                if packet['kind']=='hello':
                    sessions.append(packet['session'])
                    if stage==0: stage=1
                    elif stage==1 and len(sessions)>=2: stage=2
                    elif stage==2 and len(sessions)>=3: stage=3
                elif packet['kind']=='frame_end' and stage==2:
                    ack={'v':1,'session':'adapter','seq':1,'kind':'ack',
                         'body':{'session':sessions[-1],'frame_id':packet['body']['frame_id']}}
                    boot={'v':1,'session':'adapter','seq':1,'kind':'ack',
                          'body':{'session':'flash','frame_id':1}}
                    os.write(master,(json.dumps(ack)+'\n'+json.dumps(boot)+'\n').encode())
        assert stage==3 and len(set(sessions[:3]))==3, (stage,sessions)
        print('PASS pseudo-terminal lost ACK and receiver restart establish fresh sessions')
    finally:
        proc.terminate(); proc.wait(timeout=5)
        os.close(master); os.close(slave)
