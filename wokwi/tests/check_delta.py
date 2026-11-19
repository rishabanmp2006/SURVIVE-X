"""Profile 3 transactions, rejection and application-state diff checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from app import Action, Screen, SURVIVEXApp
from wokwi.display_adapter import IncrementalEncoder, ProtocolEncoder, TFTDisplay, region_delta


def size(packets): return sum(len(p.encode())+1 for p in packets)

def mode(packets): return json.loads(packets[0])['body']['mode']


def raster(ops, pixels=None):
    # A deterministic pixel model of the keyboard and text primitives.
    pixels = list(pixels) if pixels is not None else [0]*(320*240)
    def pixel(x, y, color):
        if 0<=x<320 and 0<=y<240: pixels[y*320+x]=color
    for op in ops:
        if op[0]=='r':
            _,x,y,w,h,color,border=op
            for yy in range(y,y+h):
                for xx in range(x,x+w):
                    pixel(xx,yy,border if border is not None and (xx in (x,x+w-1) or yy in (y,y+h-1)) else color)
        elif op[0]=='t':
            _,x,y,scale,color,text=op
            for index,ch in enumerate(text):
                for yy in range(7*scale):
                    for xx in range(5*scale):
                        if (ord(ch)+xx//scale+yy//scale)%3: pixel(x+index*6*scale+xx,y+yy,color)
        elif op[0]=='l':
            _,x1,y1,x2,y2,color=op
            steps=max(abs(x2-x1),abs(y2-y1))
            for i in range(steps+1):
                pixel(round(x1+(x2-x1)*i/max(1,steps)),round(y1+(y2-y1)*i/max(1,steps)),color)
        else:
            raise AssertionError('unexpected primitive')
    return pixels


with tempfile.TemporaryDirectory() as tmp:
    ui=SURVIVEXApp(storage_path=str(Path(tmp)/'wp.json'))
    ui.current_screen=Screen.AI_KEYBOARD
    enc=IncrementalEncoder('delta-test')
    hello=enc.hello()
    full=enc.frame(ui)
    assert mode(full)=='FULL' and enc.baseline is None
    assert not enc.acknowledge(999)
    assert enc.acknowledge(1)
    previous=TFTDisplay(); ui.render(previous)
    ui.handle_action(Action.RIGHT)
    current=TFTDisplay(); ui.render(current)
    changes=region_delta(previous.ops,current.ops)
    assert changes is not None
    assert raster(changes,raster(previous.ops))==raster(current.ops)
    delta=enc.frame(ui)
    assert mode(delta)=='DELTA'
    assert size(delta)<size(full)
    assert json.loads(delta[0])['body']['base_frame_id']==1
    assert enc.baseline[0]=='AI_CHAT'
    try: enc.frame(ui); assert False
    except ValueError: pass
    assert enc.acknowledge(2)
    previous=current
    ui.handle_action(Action.SELECT)
    current=TFTDisplay(); ui.render(current)
    changes=region_delta(previous.ops,current.ops)
    assert changes is not None
    assert raster(changes,raster(previous.ops))==raster(current.ops)
    char_delta=enc.frame(ui)
    print('keyboard FULL',size(full),'highlight',size(delta),'character',size(char_delta),mode(char_delta))
    assert enc.acknowledge(3)
    ui.current_screen=Screen.ASSISTANT_INFO
    transition=enc.frame(ui)
    assert mode(transition)=='FULL'
    enc.acknowledge(4)
    ui.ai.status='GENERATING'
    status=enc.frame(ui)
    print('AI status',size(status),mode(status))
    enc.acknowledge(5)
    ui.ai.messages=[('You','hello'),('AI','A long response. '*40)]
    response=enc.frame(ui)
    print('AI response',size(response),mode(response))
    enc.acknowledge(6)
    ui.current_screen=Screen.NAVIGATION
    nav=enc.frame(ui);enc.acknowledge(7)
    ui.sim_has_fix=False
    loss=enc.frame(ui)
    print('navigation FULL',size(nav),'GPS loss',size(loss),mode(loss))
    enc.acknowledge(8)
    ui.sim_has_fix=True
    recovery=enc.frame(ui)
    print('GPS recovery',size(recovery),mode(recovery))
    binary=Path(tmp)/'receiver'
    includes=os.environ.get('ARDUINOJSON_SRC',str(Path.home()/'Documents/Arduino/libraries/ArduinoJson/src'))
    subprocess.run(['c++','-std=c++17','-I',str(ROOT/'wokwi/tests'),'-I',includes,
                    str(ROOT/'wokwi/tests/receiver.cpp'),'-o',str(binary)],check=True)
    def run(lines):
        return subprocess.run([binary],input='\n'.join(lines)+'\n',text=True,capture_output=True,check=True).stdout.splitlines()
    out=run([hello]+full[:10]+['EXTINPUT']+full[10:]+delta+char_delta+transition+status+response+nav+loss+recovery)
    assert all(row.split()[1]=='0' for row in out),out
    assert out[6].split()[2]=='1' and out[7].split()[2]=='0' and out[8].split()[2]=='1', out
    assert run([hello]+full+['FAILAFTER']+delta+['CHECKRESYNC'])[-1].split()[:2]==['1','1']
    assert run([hello,'FAILPAINT']+full+['CHECKRESYNC'])[-1].split()[:2]==['0','1']
    broken=delta.copy();end=json.loads(broken[-1]);end['body']['checksum']^=1;broken[-1]=json.dumps(end,separators=(',',':'))
    assert run([hello]+full+broken)[-1].split()[1]=='1'
    # A checksum-valid batch containing an invalid primitive is still rejected.
    import zlib
    malformed=delta.copy()
    draw=json.loads(malformed[1]); draw['body']['payload']='["b",["t",999,0,1,1,"x"]]'
    malformed[1]=json.dumps(draw,separators=(',',':'))
    begin=json.loads(malformed[0])['body']; end=json.loads(malformed[-1])
    payloads=[json.loads(line)['body']['payload'] for line in malformed[1:-1]]
    data=begin['screen']+'\nDELTA\n'+str(begin['base_frame_id'])+'\n'+'\n'.join(payloads)+'\n'
    end['body']['checksum']=zlib.crc32(data.encode())
    malformed[-1]=json.dumps(end,separators=(',',':'))
    assert run([hello]+full+malformed)[-1].split()[1]=='1' 
    duplicate=delta.copy(); duplicate.insert(2,duplicate[1])
    assert run([hello]+full+duplicate)[-1].split()[1]=='0'
    reordered=delta.copy(); reordered[1],reordered[2]=reordered[2],reordered[1]
    assert run([hello]+full+reordered)[-1].split()[1]=='1'
    # A reboot has no negotiated session or baseline.
    assert run([hello]+delta)[-1].split()[1]=='1' 
    assert run([hello]+delta)[-1].split()[1]=='1'
    assert run([hello]+full+delta[:1]+delta[2:])[-1].split()[1]=='1'
    # After a rejected delta, a new session and full frame recover.
    replacement=IncrementalEncoder('recovery');h2=replacement.hello(); f2=replacement.frame(ui)
    assert run([hello]+full+broken+[h2]+f2)[-1].split()[1]=='0'
    assert run([hello]+full+['TIMEOUT']+delta)[-1].split()[1]=='1'
    ui.close()
print('PASS profile 3 full, delta, checksum, missing base/chunk, timeout and recovery')
