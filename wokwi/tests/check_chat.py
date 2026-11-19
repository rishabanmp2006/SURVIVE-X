"""Exercise profile 2 frames through the real native receiver and app actions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app import Action, Screen, SURVIVEXApp
from wokwi.display_adapter import ActionReceiver, ProtocolEncoder

class FakeAI:
    def __init__(self): self.status='READY'; self.error=''; self.messages=[]
    def submit(self, prompt): self.status='GENERATING'; return True
    def cancel(self): self.status='CANCELLED'
    def clear(self): self.messages=[]; self.status='READY'
    def close(self): pass

with tempfile.TemporaryDirectory() as tmp, patch('app.ChatWorker', FakeAI):
    app = SURVIVEXApp(storage_path=str(Path(tmp)/'waypoints.json'))
    encoder = ProtocolEncoder('chat-test')
    action = ActionReceiver()
    seq = 0
    def press(name):
        global seq
        seq += 1
        action.receive(app, json.dumps({'v':1,'session':'adapter','seq':seq,'kind':'action','body':{'action':name}}))
    app.home_menu_idx=5
    press('SELECT')
    assert app.current_screen==Screen.ASSISTANT_INFO
    app.ai.messages=[('You','Where is north?'), ('AI','**North** is a direction. '+('Long response words ' * 20))]
    packets=[encoder.hello()]
    packets.extend(encoder.frame(app))
    packets += ['LABEL:OFFLINE AI  READY', 'EXTINPUT']
    press('UP'); assert app.ai_scroll==1
    app.ai.status='GENERATING'
    packets.extend(encoder.frame(app))
    packets += ['LABEL:OFFLINE AI  GENERATING']
    press('LEFT'); press('LEFT'); press('SELECT')
    assert app.ai.status=='CANCELLED'
    packets.extend(encoder.frame(app)); packets += ['LABEL:OFFLINE AI  CANCELLED']
    press('RIGHT'); press('SELECT'); assert not app.ai.messages
    app.ai.status='ERROR'; app.ai.error='model failed'
    packets.extend(encoder.frame(app)); packets += ['LABEL:OFFLINE AI  ERROR','LABEL:Error: model failed']
    # GPS loss during chat must leave the rendered error screen intact.
    packets.append(encoder.packet('gps', {'fix': False}))
    packets.append('LABEL:OFFLINE AI  ERROR')
    packets.append('TIMEOUT')
    packets.extend(encoder.frame(app))
    packets.append('LABEL:OFFLINE AI  ERROR')
    app.ai.status='READY'; app.ai_focus=0; press('SELECT')
    assert app.current_screen==Screen.AI_KEYBOARD
    press('SELECT'); assert app.ai_input=='a'
    packets.extend(encoder.frame(app)); packets += ['LABEL:AI INPUT KEYBOARD']
    press('BACK'); press('RIGHT'); press('SELECT'); assert app.ai.status=='GENERATING'
    press('BACK'); assert app.current_screen==Screen.HOME
    app.home_menu_idx=0; press('SELECT'); assert app.current_screen==Screen.NAVIGATION
    app.close()
    binary=Path(tmp)/'receiver'
    includes=os.environ.get('ARDUINOJSON_SRC', str(Path.home()/'Documents/Arduino/libraries/ArduinoJson/src'))
    subprocess.run(['c++','-std=c++17','-I',str(ROOT/'wokwi/tests'),'-I',includes,
                    str(ROOT/'wokwi/tests/receiver.cpp'),'-o',str(binary)],check=True)
    result=subprocess.run([binary],input='\n'.join(packets)+'\n',text=True,capture_output=True)
    if result.returncode: raise AssertionError(result.stderr)
    # Corrupt an otherwise complete frame and confirm it is rejected.
    packets_bad = [encoder.hello()]
    app = SURVIVEXApp(storage_path=str(Path(tmp)/'other.json'))
    app.current_screen = Screen.ASSISTANT_INFO
    frame = list(encoder.frame(app))
    end = json.loads(frame[-1]); end['body']['checksum'] ^= 1
    frame[-1] = json.dumps(end, separators=(',', ':'))
    bad = subprocess.run([binary],input='\n'.join(packets_bad+frame)+'\n',text=True,capture_output=True,check=True)
    assert bad.stdout.splitlines()[-1].split()[1]=='1'
    # A missing chunk and an oversized chunk both fail, then the next full
    # reset frame restores the AI screen.
    for fault in ('missing', 'oversize'):
        encoder = ProtocolEncoder('fault-test')
        hello = encoder.hello()
        first = list(encoder.frame(app))
        if fault == 'missing':
            del first[1]
        else:
            draw = json.loads(first[1]); draw['body']['payload'] = 'x' * 161
            first[1] = json.dumps(draw, separators=(',', ':'))
        second = list(encoder.frame(app))
        trace = [hello] + first + second + ['LABEL:OFFLINE AI  READY']
        result = subprocess.run([binary],input='\n'.join(trace)+'\n',text=True,capture_output=True)
        assert result.returncode==0, (fault, result.stderr)
        assert result.stdout.splitlines()[-1].split()[1]=='0'
    app.close()
    print('PASS profile 2: UI actions, chat status, keyboard, scrolling, cancel, clear, receiver and navigation')
