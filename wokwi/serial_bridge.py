"""Drive a profile 2 TFT receiver through a 115200-baud POSIX serial device."""
import argparse
import json
import uuid
import os
from pathlib import Path
import select
import sys
import termios
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from display_adapter import ActionReceiver, IncrementalEncoder


def open_serial(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        os.close(fd)
        raise
    return fd


def run(port, db, waypoints, scale):
    fd = open_serial(port)
    device = None
    display = None
    try:
        device = app.SURVIVEXApp(storage_path=waypoints, db_path=db, async_search=True)
        display = app.PygameDisplayAdapter(scale=scale)
        inputs = app.PygameInputAdapter(app_ref=device)
        def new_session():
            encoder = IncrementalEncoder('sx-' + uuid.uuid4().hex[:16])
            return encoder, ('\n' + encoder.hello() + '\n').encode()

        encoder, outgoing = new_session()
        actions = ActionReceiver()
        incoming = bytearray()
        dirty = True
        last_send = last_contact = time.monotonic()
        while device.running:
            event = inputs.read_action(timeout_sec=0)
            while event != app.Action.NONE:
                device.handle_action(event, inputs.last_char)
                dirty = True
                event = inputs.read_action(timeout_sec=0)
            dirty = device.tick() or dirty
            now = time.monotonic()
            # A lost ACK leaves the receiver's base uncertain. A new session and
            # FULL transaction safely establish a new shared baseline.
            if not outgoing and ((encoder.pending and now-last_send > 4) or
                                 (encoder.baseline and now-last_contact > 4)):
                encoder, outgoing = new_session()
                actions = ActionReceiver()
                dirty = True
                last_send = last_contact = now
            if not outgoing and not encoder.pending and dirty:
                capture = encoder.frame(device, int(now * 1000) & 0xffffffff)
                if capture:
                    outgoing = ('\n'.join(capture) + '\n').encode()
                    last_send = now
                dirty = False
                device.render(display)
            if not outgoing and not encoder.pending and encoder.baseline and now-last_send >= 0.4:
                outgoing = (encoder.ping(int(now * 1000) & 0xffffffff) + '\n').encode()
                last_send = now
            ready_read, ready_write, _ = select.select([fd], [fd] if outgoing else [], [], 0.01)
            if ready_write:
                try:
                    count = os.write(fd, outgoing[:512])
                    outgoing = outgoing[count:]
                except BlockingIOError:
                    pass
            if ready_read:
                try:
                    chunk = os.read(fd, 512)
                except BlockingIOError:
                    chunk = b''
                incoming.extend(chunk)
                if len(incoming) > 4096:
                    incoming.clear()
                while b'\n' in incoming:
                    line, _, rest = incoming.partition(b'\n')
                    incoming = bytearray(rest)
                    try:
                        msg = json.loads(line.decode('ascii'))
                        if msg.get('v') != 1 or msg.get('session') != 'adapter':
                            continue
                        kind = msg.get('kind')
                        body = msg.get('body', {})
                        if kind == 'ack' and body.get('session') == encoder.session:
                            encoder.acknowledge(body.get('frame_id'))
                            last_contact = time.monotonic()
                        elif kind == 'ack' and body.get('session') == 'flash':
                            encoder, outgoing = new_session()
                            actions = ActionReceiver()
                            dirty = True
                            last_send = last_contact = time.monotonic()
                        elif kind == 'resync' and body.get('reason') != 'WAITING FOR FRAME':
                            encoder, outgoing = new_session()
                            actions = ActionReceiver()
                            dirty = True
                            last_send = last_contact = time.monotonic()
                        elif kind == 'action':
                            actions.receive(device, line.decode('ascii'))
                            dirty = True
                    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
                        pass
    finally:
        if device:
            device.close()
        if display:
            display.close()
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='POSIX serial device connected to the display receiver')
    parser.add_argument('--waypoints', required=True, help='Explicit waypoint file path')
    parser.add_argument('--db', help='Read-only places database')
    parser.add_argument('--scale', type=int, default=3, choices=(1, 2, 3, 4))
    args = parser.parse_args()
    run(args.port, args.db, args.waypoints, args.scale)
