"""Bounded host-side sensor ingestion; hardware-specific readers supply samples."""
import math
import threading
import time

import navigation as nav


class LiveSensors:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self._gps = None
        self._compass = None
        self._line = bytearray()
        self._overflow = False

    def disconnect_gnss(self):
        with self.lock:
            self._gps = None
            self._line.clear()
            self._overflow = False

    def disconnect_compass(self):
        with self.lock:
            self._compass = None

    def gps(self):
        with self.lock:
            return self._gps or nav.GPSReading(0, 0, timestamp=0, has_fix=False,
                                                sensor_mode=nav.SensorMode.LIVE_GNSS)

    def compass(self):
        with self.lock:
            return self._compass

    def compass_sample(self, magnetic_heading, calibrated, timestamp=None):
        # Calibration, axis orientation and tilt compensation belong to the
        # selected sensor driver. Never assume that a raw vector is a heading.
        stamp = self.clock() if timestamp is None else timestamp
        if (type(calibrated) is not bool or not nav.finite_number(magnetic_heading)
                or not 0 <= magnetic_heading < 360 or not nav.finite_number(stamp)):
            self.disconnect_compass()
            return False
        with self.lock:
            self._compass = nav.CompassReading(magnetic_heading, stamp, calibrated)
        return True

    def feed_nmea(self, data):
        """Accept bytes from a configured NMEA source. Only checksummed GGA fixes update state."""
        if not isinstance(data, bytes):
            raise TypeError('NMEA input must be bytes')
        with self.lock:
            return self._feed_nmea_locked(data)

    def _feed_nmea_locked(self, data):
        accepted = 0
        for byte in data:
            if byte == 10:
                if not self._overflow:
                    try:
                        if self._parse_gga(bytes(self._line).rstrip(b'\r')):
                            accepted += 1
                    except (ValueError, UnicodeError, IndexError, OverflowError):
                        pass
                self._line.clear()
                self._overflow = False
            elif not self._overflow:
                if len(self._line) >= 127:
                    self._line.clear()
                    self._overflow = True
                else:
                    self._line.append(byte)
        return accepted

    @staticmethod
    def _coordinate(value, hemisphere, degrees):
        if hemisphere not in (('N','S') if degrees==2 else ('E','W')):
            raise ValueError('invalid hemisphere')
        if len(value) < degrees+2:
            raise ValueError('invalid coordinate')
        degree = int(value[:degrees])
        minutes = float(value[degrees:])
        limit = 90 if degrees==2 else 180
        if not math.isfinite(minutes) or not 0 <= minutes < 60 or degree > limit or (degree == limit and minutes):
            raise ValueError('invalid coordinate')
        result = degree + minutes/60
        return -result if hemisphere in ('S','W') else result

    def _parse_gga(self, line):
        text = line.decode('ascii')
        if not text.startswith('$') or text.count('*') != 1:
            raise ValueError('invalid NMEA frame')
        content, checksum = text[1:].split('*')
        if len(checksum) != 2:
            raise ValueError('invalid checksum')
        value = 0
        for ch in content:
            value ^= ord(ch)
        if value != int(checksum, 16):
            raise ValueError('checksum mismatch')
        fields = content.split(',')
        if len(fields[0]) != 5 or fields[0][2:] != 'GGA':
            return False
        quality = int(fields[6])
        if quality < 0 or quality > 8:
            raise ValueError('invalid fix quality')
        if not quality:
            with self.lock:
                self._gps = None
            return True
        lat = self._coordinate(fields[2],fields[3],2)
        lon = self._coordinate(fields[4],fields[5],3)
        satellites = int(fields[7]) if fields[7] else None
        hdop = float(fields[8]) if fields[8] else None
        altitude = float(fields[9]) if fields[9] and fields[10]=='M' else None
        if satellites is not None and not 0 <= satellites <= 99:
            raise ValueError('invalid satellite count')
        reading = nav.GPSReading(lat,lon,altitude,self.clock(),True,None,hdop,satellites,
                                 nav.SensorMode.LIVE_GNSS)
        with self.lock:
            self._gps = reading
        return True


class NMEAReader:
    """Read a user-selected POSIX GNSS serial device without blocking the UI."""
    def __init__(self, sensors, path, baud=9600):
        import termios
        speeds = {4800: termios.B4800, 9600: termios.B9600,
                  19200: termios.B19200, 38400: termios.B38400,
                  115200: termios.B115200}
        if baud not in speeds:
            raise ValueError('unsupported NMEA baud')
        self.sensors, self.path, self.speed = sensors, path, speeds[baud]
        self.stopped = threading.Event()
        self.error = ''
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=2)
        self.sensors.disconnect_gnss()

    def _run(self):
        import os
        import select
        import termios
        while not self.stopped.is_set():
            fd = None
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
                attrs = termios.tcgetattr(fd)
                attrs[0] = attrs[1] = attrs[3] = 0
                attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                attrs[4] = attrs[5] = self.speed
                attrs[6][termios.VMIN] = 0
                attrs[6][termios.VTIME] = 0
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                self.error = ''
                while not self.stopped.is_set():
                    if select.select([fd], [], [], 0.25)[0]:
                        chunk = os.read(fd, 512)
                        if not chunk:
                            raise OSError('GNSS device disconnected')
                        self.sensors.feed_nmea(chunk)
            except OSError as exc:
                self.error = str(exc)[:160]
            finally:
                if fd is not None:
                    os.close(fd)
                self.sensors.disconnect_gnss()
            self.stopped.wait(1)
