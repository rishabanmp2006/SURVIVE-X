"""Profile 2 display encoder for the Linux-owned SURVIVE-X application."""
import json
import re
import unicodedata
import zlib

from app import Action, Colors, DisplayAdapter, Screen

MAX_CHUNKS = 128
MAX_PAYLOAD = 160


def compact(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def rgb565(color):
    r, g, b = color
    return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)


def display_text(text):
    text = str(text).translate(str.maketrans({'•': '-', '°': 'o', '–': '-', '—': '-', '…': '...', '▶': '>', '◀': '<'}))
    text = re.sub(r'(?<!\w)\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\w)', r'\1', text)
    text = re.sub(r'(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)', r'\1', text)
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    text = unicodedata.normalize('NFKD', text)
    return ''.join(c if 32 <= ord(c) <= 126 else '?' for c in text if not unicodedata.combining(c))


class TFTDisplay(DisplayAdapter):
    """GFX classic font has a measured six-pixel advance and eight-pixel height."""
    def __init__(self):
        self.ops = []
        self.background = Colors.BG

    def measure_text(self, text, size='med'):
        return 6 * len(display_text(text))

    def wrap_text(self, text, width, size='med'):
        # Preserve line breaks while applying fallback before measuring.
        return super().wrap_text('\n'.join(display_text(line) for line in str(text).split('\n')), width, size)

    def clear(self, color=Colors.BG):
        self.ops = []
        self.background = color
        if rgb565(color):
            self.ops.append(['r', 0, 0, 320, 240, rgb565(color), None])

    def draw_rect(self, x, y, w, h, color, border_color=None, border_width=1, border_radius=0):
        self.ops.append(['r', x, y, w, h, rgb565(color), rgb565(border_color) if border_color else None])

    def draw_line(self, x1, y1, x2, y2, color, width=1):
        self.ops.append(['l', max(0, min(x1, 319)), max(0, min(y1, 239)),
                         max(0, min(x2, 319)), max(0, min(y2, 239)), rgb565(color)])

    def draw_text(self, x, y, text, size='med', color=None, style='NORMAL'):
        text = display_text(text)
        text = text[:max(0, (320-x)//6)]
        if text and y <= 232:
            self.ops.append(['t', x, y, 1, rgb565(color or Colors.TEXT_WHITE), text])

    def draw_circle(self, cx, cy, radius, color, width=0):
        # The v1 navigation replay remains the authoritative arrow renderer.
        # Circles are represented by short line segments in profile 2.
        import math
        points = [(round(cx + radius*math.cos(i*math.pi/8)),
                   round(cy + radius*math.sin(i*math.pi/8))) for i in range(16)]
        for a, b in zip(points, points[1:] + points[:1]):
            self.draw_line(*a, *b, color)

    def draw_polygon(self, points, color, width=0):
        if len(points) == 7 and not width:
            self.ops.append(['p', rgb565(color), *[list(p) for p in points]])
        else:
            for a, b in zip(points, points[1:] + points[:1]):
                self.draw_line(*a, *b, color)

    def present(self):
        pass


class ProtocolEncoder:
    def __init__(self, session='survivex'):
        if not session or len(session.encode()) > 23:
            raise ValueError('invalid session')
        self.session = session
        self.seq = 0
        self.frame_id = 0

    def packet(self, kind, body, now=0):
        self.seq += 1
        return compact({'v': 1, 'session': self.session, 'seq': self.seq,
                        't_ms': now, 'kind': kind, 'body': body})

    def hello(self, now=0):
        return self.packet('hello', {'width': 320, 'height': 240, 'color': 'RGB565',
            'font': 'gfx-classic-6x8-ascii', 'max_payload': 160, 'watchdog_ms': 1000,
            'profile': 2}, now)

    def frame(self, app, now=0):
        display = TFTDisplay()
        app.render(display)
        lines = [compact(op) for op in display.ops]
        if not lines or len(lines) > MAX_CHUNKS or any(len(line.encode()) > MAX_PAYLOAD for line in lines):
            raise ValueError('frame exceeds receiver limits')
        screen = ('AI_CHAT' if app.current_screen in (Screen.ASSISTANT_INFO, Screen.AI_KEYBOARD)
                  else 'HOME' if app.current_screen == Screen.HOME
                  else 'NAVIGATION' if app.current_screen == Screen.NAVIGATION else 'PLACES')
        self.frame_id += 1
        fid = self.frame_id
        yield self.packet('frame_begin', {'frame_id': fid, 'base_frame_id': None,
                          'chunks': len(lines), 'screen': screen}, now)
        for index, line in enumerate(lines):
            yield self.packet('draw', {'frame_id': fid, 'index': index, 'payload': line}, now)
        yield self.packet('frame_end', {'frame_id': fid, 'chunks': len(lines),
                          'checksum': zlib.crc32((screen+'\n'+'\n'.join(lines)+'\n').encode())}, now)


def dispatch_action(app, line):
    """Apply a validated receiver action envelope to the same application state."""
    if len(line.encode()) > 640:
        raise ValueError('action too long')
    msg = json.loads(line)
    if msg.get('v') != 1 or msg.get('kind') != 'action' or msg.get('session') != 'adapter':
        raise ValueError('invalid action envelope')
    action = msg.get('body', {}).get('action')
    if action not in ('UP', 'DOWN', 'LEFT', 'RIGHT', 'SELECT', 'BACK'):
        raise ValueError('invalid action')
    app.handle_action(Action[action])


class ActionReceiver:
    def __init__(self):
        self.last_seq = 0

    def receive(self, app, line):
        msg = json.loads(line)
        seq = msg.get('seq')
        if type(seq) is not int or seq <= self.last_seq:
            raise ValueError('stale or invalid action sequence')
        dispatch_action(app, line)
        self.last_seq = seq


def bounds(op):
    kind = op[0]
    if kind == 'r':
        return op[1], op[2], op[1]+op[3], op[2]+op[4]
    if kind == 't':
        return op[1], op[2], op[1]+6*op[3]*len(op[5]), op[2]+8*op[3]
    if kind == 'l':
        return min(op[1], op[3]), min(op[2], op[4]), max(op[1], op[3])+1, max(op[2], op[4])+1
    if kind == 'p':
        return (min(p[0] for p in op[2:]), min(p[1] for p in op[2:]),
                max(p[0] for p in op[2:])+1, max(p[1] for p in op[2:])+1)
    raise ValueError('unsupported primitive')


def intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def region_delta(old, new):
    """Reconstruct changed rectangles from the complete new drawing stack.

    When an intersecting primitive would need clipping other than a plain filled
    rectangle, return None so the caller can send a full frame instead.
    """
    from difflib import SequenceMatcher
    old_lines = [compact(op) for op in old]
    new_lines = [compact(op) for op in new]
    regions = []
    for tag, i, j, k, end in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag != 'equal':
            regions.extend(bounds(op) for op in old[i:j] + new[k:end])
    merged = []
    for region in regions:
        index = 0
        while index < len(merged):
            if intersects(region, merged[index]):
                other = merged.pop(index)
                region = (min(region[0],other[0]), min(region[1],other[1]),
                          max(region[2],other[2]), max(region[3],other[3]))
                index = 0
            else:
                index += 1
        merged.append(region)
    result = []
    for region in merged:
        for op in new:
            rect = bounds(op)
            if not intersects(rect, region):
                continue
            inside = (rect[0]>=region[0] and rect[1]>=region[1] and
                      rect[2]<=region[2] and rect[3]<=region[3])
            if inside:
                result.append(op)
            elif op[0]=='r' and op[6] is None:
                x1=max(rect[0],region[0]); y1=max(rect[1],region[1])
                x2=min(rect[2],region[2]); y2=min(rect[3],region[3])
                result.append(['r',x1,y1,x2-x1,y2-y1,op[5],None])
            else:
                return None
    if len(result)>MAX_CHUNKS:
        return None
    return result


def pack_ops(ops):
    """Group ordered primitives into bounded profile 3 draw payloads."""
    groups = []
    group = []
    size = 5  # ["b"] plus a comma per item
    for op in ops:
        line = compact(op)
        line_size = len(line.encode())
        if line_size > MAX_PAYLOAD:
            raise ValueError('payload exceeds receiver limits')
        if group and size + 1 + line_size > MAX_PAYLOAD:
            groups.append('["b",' + ','.join(group) + ']' if len(group) > 1 else group[0])
            group = []
            size = 5
        group.append(line)
        size += 1 + line_size
    if group:
        groups.append('["b",' + ','.join(group) + ']' if len(group) > 1 else group[0])
    return groups



class IncrementalEncoder(ProtocolEncoder):
    """The caller must acknowledge each returned frame before using it as a base."""
    def __init__(self, session='survivex'):
        super().__init__(session)
        self.baseline = None
        self.pending = None
        self.acknowledged = 0

    def hello(self, now=0):
        return self.packet('hello', {'width':320, 'height':240, 'color':'RGB565',
            'font':'gfx-classic-6x8-ascii', 'max_payload':160, 'watchdog_ms':1000,
            'profile':3}, now)

    def acknowledge(self, frame_id):
        if self.pending and self.pending[0] == frame_id:
            self.acknowledged, self.baseline = frame_id, self.pending[1:]
            self.pending = None
            return True
        return False

    def ping(self, now=0):
        if not self.baseline:
            raise ValueError('no acknowledged frame')
        return self.packet('ping', {'frame_id':self.acknowledged}, now)

    def frame(self, app, now=0):
        if self.pending:
            raise ValueError('frame awaiting acknowledgment')
        display = TFTDisplay()
        app.render(display)
        screen = ('AI_CHAT' if app.current_screen in (Screen.ASSISTANT_INFO, Screen.AI_KEYBOARD)
                  else 'HOME' if app.current_screen == Screen.HOME
                  else 'NAVIGATION' if app.current_screen == Screen.NAVIGATION else 'PLACES')
        full = display.ops
        if not full or len(full)>MAX_CHUNKS or any(len(compact(op).encode())>MAX_PAYLOAD for op in full):
            raise ValueError('frame exceeds receiver limits')
        delta = None
        if self.baseline and self.baseline[0] == screen:
            delta = region_delta(self.baseline[1], full)
        if delta == []:
            return None
        # Compare exact serialized byte counts without consuming sequence numbers.
        def wire_size(ops, mode, base):
            fid = self.frame_id + 1
            lines = pack_ops(ops)
            bodies = [('frame_begin', {'frame_id':fid, 'base_frame_id':base or None,
                       'chunks':len(lines), 'screen':screen, 'mode':mode})]
            bodies.extend(('draw', {'frame_id':fid, 'index':i, 'payload':line})
                          for i, line in enumerate(lines))
            bodies.append(('frame_end', {'frame_id':fid, 'chunks':len(lines), 'checksum':
                zlib.crc32((screen+'\n'+mode+'\n'+str(base)+'\n'+'\n'.join(lines)+'\n').encode())}))
            return sum(len(compact({'v':1,'session':self.session,'seq':self.seq+i+1,
                       't_ms':now,'kind':kind,'body':body}).encode())+1
                       for i,(kind,body) in enumerate(bodies))
        use_delta = bool(delta) and wire_size(delta,'DELTA',self.acknowledged) < wire_size(full,'FULL',0)
        ops = delta if use_delta else full
        self.frame_id += 1
        fid = self.frame_id
        base = self.acknowledged if use_delta else 0
        mode = 'DELTA' if use_delta else 'FULL'
        lines = pack_ops(ops)
        crc = zlib.crc32((screen+'\n'+mode+'\n'+str(base)+'\n'+'\n'.join(lines)+'\n').encode())
        packets = [self.packet('frame_begin', {'frame_id':fid, 'base_frame_id':base if use_delta else None,
                    'chunks':len(lines), 'screen':screen, 'mode':mode}, now)]
        packets.extend(self.packet('draw', {'frame_id':fid,'index':i,'payload':line}, now)
                       for i,line in enumerate(lines))
        packets.append(self.packet('frame_end', {'frame_id':fid,'chunks':len(lines),'checksum':crc}, now))
        self.pending = (fid, screen, full)
        return packets
