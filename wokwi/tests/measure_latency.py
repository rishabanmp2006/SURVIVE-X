"""Measure bridge action-to-last-byte with a 115200-baud paced PTY reader."""
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time

master,slave=pty.openpty()
env=dict(os.environ,SDL_VIDEODRIVER='dummy',PYGAME_HIDE_SUPPORT_PROMPT='1')
with tempfile.TemporaryDirectory() as tmp:
    proc=subprocess.Popen([sys.executable,'wokwi/serial_bridge.py','--port',os.ttyname(slave),
                           '--waypoints',tmp+'/wp','--scale','1'],env=env,
                          stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    data=b''; stage=0; action_seq=0; frame=None; session=None; started=0
    def send_action(action):
        global action_seq
        action_seq+=1
        packet={'v':1,'session':'adapter','seq':action_seq,'kind':'action','body':{'action':action}}
        os.write(master,(json.dumps(packet)+'\n').encode())
    try:
        deadline=time.monotonic()+20
        while time.monotonic()<deadline and stage<4:
            if not select.select([master],[],[],0.05)[0]: continue
            data+=os.read(master,115)
            time.sleep(.01)  # 115 bytes / 10 ms approximates 115200 baud 8N1.
            while b'\n' in data:
                line,_,data=data.partition(b'\n')
                try: packet=json.loads(line)
                except (ValueError,UnicodeError): continue
                if packet['kind']=='hello': session=packet['session']
                elif packet['kind']=='frame_begin': frame=packet['body']
                elif packet['kind']=='frame_end':
                    ack={'v':1,'session':'adapter','seq':100+stage,'kind':'ack',
                         'body':{'session':session,'frame_id':packet['body']['frame_id']}}
                    os.write(master,(json.dumps(ack)+'\n').encode())
                    if stage==0 and frame['screen']=='HOME':
                        send_action('UP');send_action('SELECT');stage=1
                    elif stage==1 and frame['screen']=='AI_CHAT':
                        send_action('SELECT');stage=2
                    elif stage==2 and frame['screen']=='AI_CHAT' and frame['mode']=='FULL':
                        started=time.monotonic();send_action('RIGHT');stage=3
                    elif stage==3 and frame['mode']=='DELTA':
                        print('paced joystick action to final delta byte %.1f ms' % ((time.monotonic()-started)*1000))
                        stage=4
        assert stage==4, stage
    finally:
        proc.terminate();proc.wait(timeout=5)
        os.close(master);os.close(slave)
