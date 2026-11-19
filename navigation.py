#!/usr/bin/env python3
"""
SURVIVE-X — Stage 1: Portable Offline Navigation Engine
Target Hardware: Arduino UNO Q (2GB RAM, Quad Cortex-A53, Linux ARM64)
Host Environment: macOS Darwin arm64 (Apple Silicon M3)

Provides deterministic geodetic navigation primitives completely independent of LLMs:
1. WGS84 latitude/longitude validation.
2. Haversine spherical distance calculation (in metres).
3. Initial true bearing (forward azimuth) calculation normalized to 0–360 degrees.
4. Relative angle and directional arrow computation (-180° to +180° turn angle).
5. Magnetic to true heading conversion using externally supplied magnetic declination.
6. GPS fix validity, horizontal accuracy (HDOP/meters), and staleness auditing.
7. Atomic JSON-persisted waypoint management (add, rename, list, select, delete).
8. Sensor simulation for development on non-sensor host hardware.
9. Terminal demonstration dashboard with clear terrain hazard warnings.

SAFETY NOTICE:
Straight-line guidance is NOT a safe walking route.
It does not account for terrain elevation, ravines, rivers, cliffs, or physical barriers.
"""

import argparse
import fcntl
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

# Earth mean radius in meters according to IUGG / WGS84 spherical approximation
EARTH_MEAN_RADIUS_METRES = 6371008.8

# Critical safety warning required on all navigation readouts
SAFETY_WARNING = (
    "CAUTION: Straight-line guidance is NOT a safe walking route. "
    "Observe terrain, cliffs, rivers, and physical barriers."
)


# ==============================================================================
# 1. Geodetic & Coordinate Math Primitives
# ==============================================================================

def validate_wgs84(lat: float, lon: float) -> Tuple[bool, str]:
    """
    Validates that latitude and longitude conform to WGS84 geographic limits:
    Latitude:  [-90.0, +90.0]
    Longitude: [-180.0, +180.0]
    Rejects NaN, infinite values, and non-numeric types.
    """
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
        return False, "Coordinates must be numeric."
    if not -90 <= lat <= 90:
        return False, "Latitude must be finite and within [-90, +90]."
    if not -180 <= lon <= 180:
        return False, "Longitude must be finite and within [-180, +180]."

    return True, "OK"


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculates great-circle distance between two WGS84 coordinates in metres
    using the Haversine formula.
    """
    valid1, err1 = validate_wgs84(lat1, lon1)
    if not valid1:
        raise ValueError(f"Origin coordinate invalid: {err1}")
    valid2, err2 = validate_wgs84(lat2, lon2)
    if not valid2:
        raise ValueError(f"Destination coordinate invalid: {err2}")

    # Identical points check (avoid numerical noise)
    if lat1 == lat2 and lon1 == lon2:
        return 0.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(dlam / 2.0) ** 2)
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_MEAN_RADIUS_METRES * c


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculates the initial true bearing (forward azimuth) from coordinate 1 to 2
    along the great circle path.
    Normalized to [0.0, 360.0) degrees.
    Returns 0.0 if origin and destination coordinates are identical.
    """
    valid1, err1 = validate_wgs84(lat1, lon1)
    if not valid1:
        raise ValueError(f"Origin coordinate invalid: {err1}")
    valid2, err2 = validate_wgs84(lat2, lon2)
    if not valid2:
        raise ValueError(f"Destination coordinate invalid: {err2}")

    if lat1 == lat2 and lon1 == lon2:
        return 0.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)

    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360.0) % 360.0


def magnetic_to_true_heading(magnetic_heading_deg: float, declination_deg: Optional[float]) -> float:
    """
    Converts magnetic compass heading to true geographic heading using externally supplied declination.
    Sign convention:
      - East declination is POSITIVE (+) (e.g., Seattle ~ +14.5°)
      - West declination is NEGATIVE (-) (e.g., Maine ~ -14.0°)
    Formula: True = (Magnetic + Declination) mod 360.

    CRITICAL RULE:
    Does NOT invent or default a declination value. If declination_deg is None,
    raises ValueError to prevent erroneous navigation guidance.
    """
    if declination_deg is None:
        raise ValueError(
            "Magnetic declination must be explicitly provided. "
            "Declination cannot be invented or defaulted in an offline safety system."
        )

    try:
        mag = float(magnetic_heading_deg)
        dec = float(declination_deg)
    except (TypeError, ValueError):
        raise ValueError("Heading and declination must be real numbers.")

    if math.isnan(mag) or math.isnan(dec) or math.isinf(mag) or math.isinf(dec):
        raise ValueError("Heading and declination cannot be NaN or Inf.")

    norm_mag = mag % 360.0
    true_heading = (norm_mag + dec) % 360.0
    return true_heading


def relative_turn_angle_deg(destination_bearing_deg: float, device_heading_deg: float) -> float:
    """
    Calculates the shortest turn angle in degrees from device heading to destination bearing.
    Range: [-180.0, +180.0)
      - Negative value (-180° to 0°): Turn LEFT
      - Positive value (0° to +180°): Turn RIGHT
      - 0.0°: Directly ahead
    """
    diff = (destination_bearing_deg - device_heading_deg) % 360.0
    if diff >= 180.0:
        diff -= 360.0
    return diff


def turn_angle_to_arrow(turn_deg: float) -> Tuple[str, str]:
    """
    Maps relative turn angle [-180, 180) to an 8-cardinal directional arrow symbol and text label.
    """
    # Normalize to [0, 360) where 0 is dead ahead
    ahead_rel = (turn_deg + 360.0) % 360.0
    sector = int((ahead_rel + 22.5) // 45) % 8
    arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
    labels = [
        "AHEAD",
        "RIGHT-AHEAD",
        "RIGHT",
        "RIGHT-BEHIND",
        "BEHIND",
        "LEFT-BEHIND",
        "LEFT",
        "LEFT-AHEAD",
    ]
    return arrows[sector], labels[sector]


# ==============================================================================
# 2. Sensor Readings & Status Enums
# ==============================================================================

class NavStatus(str, Enum):
    OK = "OK"
    NO_DESTINATION = "NO_DESTINATION"
    POSITION_UNCERTAIN = "POSITION_UNCERTAIN"
    ARRIVED = "ARRIVED"
    NO_FIX = "NO_GPS_FIX"
    STALE_GPS = "STALE_GPS"
    POOR_ACCURACY = "POOR_ACCURACY"
    INVALID_COORDS = "INVALID_COORDINATES"
    MISSING_HEADING = "MISSING_HEADING"


class SensorMode(str, Enum):
    SIMULATED = "SIMULATED"
    MANUAL = "MANUAL COORDINATES"
    LIVE_GNSS = "LIVE GNSS"


def finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return -sys.float_info.max <= value <= sys.float_info.max


def fresh_sample(timestamp, now, max_age) -> bool:
    return (finite_number(timestamp) and timestamp > 0 and finite_number(now)
            and 0 <= now - timestamp <= max_age)


@dataclass
class GPSReading:
    lat: float
    lon: float
    altitude_m: Optional[float] = None
    timestamp: float = 0.0
    has_fix: bool = True
    accuracy_m: Optional[float] = None
    hdop: Optional[float] = None
    satellites: Optional[int] = None
    sensor_mode: SensorMode = SensorMode.SIMULATED

    def validate(
        self,
        current_time: Optional[float] = None,
        max_staleness_sec: float = 10.0,
        max_accuracy_m: float = 30.0,
        max_hdop: float = 5.0,
    ) -> Tuple[bool, NavStatus, str]:
        """Validates fix status, coordinates, age, and geometric accuracy."""
        if not isinstance(self.sensor_mode, SensorMode):
            return False, NavStatus.NO_FIX, "Unknown position source."
        if self.has_fix is not True:
            return False, NavStatus.NO_FIX, "GPS receiver has no satellite fix."

        valid_coord, err = validate_wgs84(self.lat, self.lon)
        if not valid_coord:
            return False, NavStatus.INVALID_COORDS, f"Invalid GPS position: {err}"

        now = time.time() if current_time is None else current_time
        if not fresh_sample(self.timestamp, now, max_staleness_sec):
            return False, NavStatus.STALE_GPS, "GPS timestamp is invalid, stale or in the future."
        if self.accuracy_m is not None and not (
            finite_number(self.accuracy_m) and 0 <= self.accuracy_m <= max_accuracy_m
        ):
            return False, NavStatus.POOR_ACCURACY, "Horizontal accuracy invalid or insufficient."
        if self.hdop is not None and not (finite_number(self.hdop) and 0 < self.hdop <= max_hdop):
            return False, NavStatus.POOR_ACCURACY, "HDOP invalid or insufficient."
        if self.altitude_m is not None and not finite_number(self.altitude_m):
            return False, NavStatus.INVALID_COORDS, "Invalid altitude."

        return True, NavStatus.OK, "GPS fix valid and fresh."


@dataclass
class CompassReading:
    magnetic_heading_deg: float
    timestamp: float = 0.0
    is_calibrated: bool = True

    def validate(self, current_time: Optional[float] = None, max_staleness_sec: float = 5.0) -> Tuple[bool, str]:
        if self.is_calibrated is not True or not finite_number(self.magnetic_heading_deg):
            return False, "Compass is uncalibrated or heading is invalid."
        now = time.time() if current_time is None else current_time
        if not fresh_sample(self.timestamp, now, max_staleness_sec):
            return False, "Compass timestamp is invalid, stale or in the future."
        return True, "OK"


@dataclass
class Waypoint:
    name: str
    lat: float
    lon: float
    altitude_m: Optional[float] = None
    created_at: float = 0.0
    description: str = ""
    source: str = ""
    place_id: Optional[int] = None
    provenance: Optional[dict] = None

    @property
    def is_test_data(self) -> bool:
        return (self.source in ("SYNTHETIC_TEST_FIXTURE", SensorMode.SIMULATED.value)
                or "SYNTHETIC_TEST_FIXTURE" in self.description)

    @property
    def display_name(self) -> str:
        if self.is_test_data:
            identity = f"#{self.place_id} " if self.place_id is not None else ""
            return f"TEST DATA {identity}{self.name}"
        if not self.source:
            return f"UNVERIFIED {self.name}"
        return self.name


@dataclass
class NavSolution:
    status: NavStatus
    status_message: str
    distance_m: Optional[float]
    bearing_deg: Optional[float]
    true_heading_deg: Optional[float]
    turn_angle_deg: Optional[float]
    arrow: str
    arrow_label: str
    safety_warning: str = SAFETY_WARNING


# ==============================================================================
# 3. Waypoint Manager (Atomic JSON Persistence)
# ==============================================================================

class WaypointManager:
    """
    Manages named waypoints stored in a minimal, portable JSON file.
    Uses atomic filesystem writes (temporary file + os.replace) to prevent
    data corruption on embedded flash storage during unexpected power loss.
    """

    def __init__(self, storage_path: str = "waypoints.json"):
        self.storage_path = Path(storage_path)
        self.waypoints: dict[str, Waypoint] = {}
        self.active_waypoint_name: Optional[str] = None
        self.load_error = None
        self._stored_bytes = None
        self.load()

    def add_waypoint(
        self,
        name: str,
        lat: float,
        lon: float,
        altitude_m: Optional[float] = None,
        description: str = "",
        source: str = "",
        place_id: Optional[int] = None,
        activate: bool = False,
        provenance: Optional[dict] = None,
    ) -> Waypoint:
        name_clean = name.strip()
        if not name_clean:
            raise ValueError("Waypoint name cannot be empty.")
        valid, err = validate_wgs84(lat, lon)
        if not valid:
            raise ValueError(f"Invalid waypoint coordinates: {err}")

        if altitude_m is not None and not finite_number(altitude_m):
            raise ValueError("Waypoint altitude must be finite.")
        if place_id is not None:
            for existing in self.waypoints.values():
                if (existing.place_id, existing.source, existing.lat, existing.lon) == (place_id, source, lat, lon):
                    if activate:
                        self.select_active_waypoint(existing.name)
                    return existing
        base = name_clean
        suffix = 2
        while name_clean in self.waypoints:
            name_clean = f"{base} ({suffix})"
            suffix += 1

        wp = Waypoint(
            name=name_clean,
            lat=float(lat),
            lon=float(lon),
            altitude_m=float(altitude_m) if altitude_m is not None else None,
            created_at=time.time(),
            description=description.strip(), source=source, place_id=place_id, provenance=provenance,
        )
        self.waypoints[name_clean] = wp
        if activate or self.active_waypoint_name is None:
            self.active_waypoint_name = name_clean
        self.save()
        return wp

    def rename_waypoint(self, old_name: str, new_name: str) -> bool:
        old_clean = old_name.strip()
        new_clean = new_name.strip()
        if old_clean not in self.waypoints:
            return False
        if not new_clean:
            raise ValueError("New waypoint name cannot be empty.")
        if new_clean in self.waypoints and new_clean != old_clean:
            raise ValueError(f"Waypoint '{new_clean}' already exists.")

        wp = self.waypoints.pop(old_clean)
        wp.name = new_clean
        self.waypoints[new_clean] = wp
        if self.active_waypoint_name == old_clean:
            self.active_waypoint_name = new_clean
        self.save()
        return True

    def delete_waypoint(self, name: str) -> bool:
        name_clean = name.strip()
        if name_clean in self.waypoints:
            del self.waypoints[name_clean]
            if self.active_waypoint_name == name_clean:
                self.active_waypoint_name = next(iter(self.waypoints.keys()), None)
            self.save()
            return True
        return False

    def list_waypoints(self) -> list[Waypoint]:
        return list(self.waypoints.values())

    def get_waypoint(self, name: str) -> Optional[Waypoint]:
        return self.waypoints.get(name.strip())

    def select_active_waypoint(self, name: str) -> Optional[Waypoint]:
        name_clean = name.strip()
        if name_clean in self.waypoints:
            if self.active_waypoint_name != name_clean:
                self.active_waypoint_name = name_clean
                self.save()
            return self.waypoints[name_clean]
        return None

    def get_active_waypoint(self) -> Optional[Waypoint]:
        if self.active_waypoint_name:
            return self.waypoints.get(self.active_waypoint_name)
        return None

    def save(self) -> None:
        """Atomically persists waypoints to JSON file."""
        data = {
            "active_waypoint": self.active_waypoint_name,
            "waypoints": [asdict(wp) for wp in self.waypoints.values()],
        }
        payload = json.dumps(data, indent=2, allow_nan=False).encode()
        # Serialize cooperating writers across atomic renames, without a sidecar.
        parent_fd = None
        try:
            parent_fd = os.open(self.storage_path.parent, os.O_RDONLY)
            fcntl.flock(parent_fd, fcntl.LOCK_EX)
            current = self.storage_path.read_bytes() if self.storage_path.exists() else None
            if self.load_error or current != self._stored_bytes:
                raise ValueError("Waypoint store unreadable or changed; save rejected.")
            with tempfile.NamedTemporaryFile(dir=self.storage_path.parent, delete=False) as tmp:
                try:
                    tmp.write(payload)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    os.replace(tmp.name, self.storage_path)
                    os.fsync(parent_fd)
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
            self._stored_bytes = payload
        except (OSError, ValueError):
            self.load()
            raise
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    def load(self) -> None:
        self.waypoints = {}
        self.active_waypoint_name = None
        self.load_error = None
        self._stored_bytes = None
        try:
            if not self.storage_path.exists():
                return
            raw = self.storage_path.read_bytes()
            data = json.loads(raw)
            rows = data["waypoints"]
            if set(data) - {"active_waypoint", "waypoints"}:
                raise ValueError("Unrecognized waypoint store format")
            if not isinstance(rows, list):
                raise ValueError("Invalid waypoint list")
            loaded = {}
            for row in rows:
                wp = Waypoint(**row)
                if (not isinstance(wp.name, str) or not wp.name.strip() or wp.name in loaded
                        or not validate_wgs84(wp.lat, wp.lon)[0]
                        or not isinstance(wp.description, str) or not isinstance(wp.source, str)
                        or not finite_number(wp.created_at)
                        or (wp.altitude_m is not None and not finite_number(wp.altitude_m))):
                    raise ValueError("Invalid or duplicate waypoint")
                loaded[wp.name] = wp
            active = data.get("active_waypoint", next(iter(loaded), None))
            if active is not None and active not in loaded:
                raise ValueError("Unknown active waypoint")
            self.active_waypoint_name = active
            self.waypoints = loaded
            self._stored_bytes = raw
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.load_error = str(exc)
            sys.stderr.write(f"Waypoint store unavailable: {exc}\n")


# ==============================================================================
# 4. Navigation Solver
# ==============================================================================

def compute_nav_solution(
    gps: GPSReading,
    compass: Optional[CompassReading],
    destination: Waypoint,
    declination_deg: Optional[float] = None,
    current_time: Optional[float] = None,
    arrival_threshold_m: float = 10.0,
) -> NavSolution:
    """
    Computes real-time navigation guidance from current GPS to destination waypoint:
    - Distance (metres)
    - True Bearing (degrees)
    - Relative Turn Angle and Arrow Indicator
    - Fix health, accuracy, and staleness validation
    """
    state = NavigationState.from_readings(gps, compass, declination_deg, current_time)
    return state.to_destination(destination, arrival_threshold_m)


@dataclass(frozen=True)
class NavigationState:
    gps: GPSReading
    position_status: NavStatus
    position_message: str
    true_heading: Optional[float]

    @classmethod
    def from_readings(cls, gps, compass, declination, current_time=None):
        now = time.time() if current_time is None else current_time
        _, status, message = gps.validate(current_time=now)
        heading = None
        if compass and compass.validate(current_time=now)[0] and finite_number(declination):
            heading = magnetic_to_true_heading(compass.magnetic_heading_deg, declination)
        return cls(gps, status, message, heading)

    @property
    def has_position(self):
        return self.position_status == NavStatus.OK

    def to_destination(self, destination: Optional[Waypoint], arrival_threshold_m=10.0) -> NavSolution:
        if not self.has_position:
            labels = {NavStatus.NO_FIX: "NO_GPS_FIX", NavStatus.STALE_GPS: "STALE_GPS_DATA"}
            return NavSolution(self.position_status, self.position_message, None, None, None, None,
                               "?", labels.get(self.position_status, self.position_status.value))
        if destination is None:
            return NavSolution(NavStatus.NO_DESTINATION, "No destination selected.", None, None,
                               None, None, "—", "NO_DESTINATION")
        if not validate_wgs84(destination.lat, destination.lon)[0]:
            return NavSolution(NavStatus.INVALID_COORDS, "Invalid destination.", None, None,
                               None, None, "—", "INVALID_TARGET")

        coords = (self.gps.lat, self.gps.lon, destination.lat, destination.lon)
        distance = haversine_distance_m(*coords)
        accuracy = self.gps.accuracy_m
        bearing = initial_bearing_deg(*coords) if distance > 0 else None
        if accuracy is not None and distance + accuracy <= arrival_threshold_m:
            return NavSolution(NavStatus.ARRIVED, "Within arrival radius including reported accuracy.",
                               distance, bearing, None, None, "★", "AT_DESTINATION")
        if bearing is None or (accuracy is not None and distance <= accuracy):
            return NavSolution(NavStatus.POSITION_UNCERTAIN, "Target direction unresolved at this accuracy.",
                               distance, None, None, None, "—", "POSITION_UNCERTAIN")
        if self.true_heading is None:
            return NavSolution(NavStatus.MISSING_HEADING, "Heading unavailable.", distance, bearing,
                               None, None, "—", "HEADING_UNAVAILABLE")
        turn = relative_turn_angle_deg(bearing, self.true_heading)
        arrow, label = turn_angle_to_arrow(turn)
        return NavSolution(NavStatus.OK, "Navigation solution computed.", distance, bearing,
                           self.true_heading, turn, arrow, label)


# ==============================================================================
# 5. Terminal Demonstration Dashboard & Simulation
# ==============================================================================

def render_terminal_dashboard(
    gps: GPSReading,
    compass: Optional[CompassReading],
    destination: Waypoint,
    solution: NavSolution,
    declination_deg: Optional[float] = None,
) -> str:
    """Renders a clean, structured terminal display suitable for small screens."""
    lines = [
        "=" * 60,
        " SURVIVE-X — Portable Offline Navigation Engine",
        "=" * 60,
        f" Position source: {gps.sensor_mode.value} | {gps.validate()[1].value}",
        (f" Current Pos: {gps.lat:+.6f}°, {gps.lon:+.6f}°" if gps.validate()[0] else " Current Pos: unavailable"),
        f" Destination: {destination.display_name}",
        "-" * 60,
        (f" Distance: {solution.distance_m:,.1f} m" if solution.distance_m is not None else " Distance: unavailable"),
        (f" Bearing: {solution.bearing_deg:05.1f}° True" if solution.bearing_deg is not None else " Bearing: unavailable"),
    ]

    if solution.true_heading_deg is not None:
        dec_str = f"{declination_deg:+.1f}°" if declination_deg is not None else "N/A"
        lines.append(f" True Head:   {solution.true_heading_deg:05.1f}° True (Mag: {compass.magnetic_heading_deg:05.1f}°, Dec: {dec_str})")
        lines.append(f" Guidance:    [{solution.arrow}] {solution.arrow_label:<14} (Turn {solution.turn_angle_deg:+05.1f}°)")
    else:
        lines.append(f" Guidance:    [{solution.arrow}] {solution.arrow_label} ({solution.status_message})")

    lines.append(f" Nav Status:  {solution.status.value}")
    lines.append("-" * 60)
    lines.append(f" ⚠️  {solution.safety_warning}")
    lines.append("=" * 60)
    return "\n".join(lines)


def run_demo(steps: int = 10, delay_sec: float = 0.4) -> None:
    """
    Simulates navigating on macOS:
    User moves from an initial location in the Pacific Northwest toward a target waypoint,
    with heading turning toward the target.
    """
    print("\n--- Starting SURVIVE-X Navigation Engine Demonstration ---\n")

    # Base coords: Mount Rainier National Park trailhead
    start_lat = 46.786500
    start_lon = -121.735000
    target_lat = 46.852886
    target_lon = -121.760374

    dest = Waypoint(
        name="Columbia Crest Summit",
        lat=target_lat,
        lon=target_lon,
        altitude_m=4392.0,
        description="Mount Rainier highest point",
    )

    # Local geomagnetic declination in WA: ~+14.5° East
    declination = 14.5
    now = time.time()

    # Step simulation
    for step in range(steps):
        # Progress 0.0 to 1.0
        frac = step / float(max(1, steps - 1))
        cur_lat = start_lat + (target_lat - start_lat) * (frac * 0.95)
        cur_lon = start_lon + (target_lon - start_lon) * (frac * 0.95)

        # True bearing to target
        t_bearing = initial_bearing_deg(cur_lat, cur_lon, target_lat, target_lon)

        # Simulate heading rotating toward bearing with slight wander
        sim_heading_true = (t_bearing - 45.0 + (step * 8.0)) % 360.0
        sim_heading_mag = (sim_heading_true - declination) % 360.0

        gps = GPSReading(
            lat=cur_lat,
            lon=cur_lon,
            altitude_m=1600.0 + frac * 2500.0,
            timestamp=time.time(),
            has_fix=True,
            accuracy_m=3.5,
            hdop=1.1,
            satellites=10,
        )

        compass = CompassReading(
            magnetic_heading_deg=sim_heading_mag,
            timestamp=time.time(),
            is_calibrated=True,
        )

        sol = compute_nav_solution(gps, compass, dest, declination_deg=declination)
        dashboard = render_terminal_dashboard(gps, compass, dest, sol, declination_deg=declination)

        # Print frame
        print(f"\n[Simulation Step {step + 1}/{steps}]")
        print(dashboard)

        if delay_sec > 0:
            time.sleep(delay_sec)

    # Test edge conditions in demo: Arrival and GPS loss
    print("\n[Edge Case 1: Arriving at Waypoint]")
    gps_arrived = GPSReading(
        lat=target_lat,
        lon=target_lon,
        altitude_m=4392.0,
        timestamp=time.time(),
        has_fix=True,
        accuracy_m=2.0,
        hdop=0.9,
        satellites=12,
    )
    sol_arrived = compute_nav_solution(gps_arrived, compass, dest, declination_deg=declination)
    print(render_terminal_dashboard(gps_arrived, compass, dest, sol_arrived, declination_deg=declination))

    print("\n[Edge Case 2: Complete GPS Loss (Tunnel / Dense Canopy)]")
    gps_nofix = GPSReading(
        lat=0.0,
        lon=0.0,
        timestamp=time.time(),
        has_fix=False,
    )
    sol_nofix = compute_nav_solution(gps_nofix, compass, dest, declination_deg=declination)
    print(render_terminal_dashboard(gps_nofix, compass, dest, sol_nofix, declination_deg=declination))

    print("\n--- Demonstration Finished Successfully ---")


# ==============================================================================
# 6. Built-in Test Suite (--test)
# ==============================================================================

def run_tests() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        return _run_tests(Path(directory))


def _run_tests(scratch: Path) -> bool:
    """
    Executes comprehensive automated test suite verifying all 10 required capabilities.
    """
    print("\n" + "=" * 65)
    print(" SURVIVE-X — Navigation Engine Automated Self-Test Suite")
    print("=" * 65)
    passed = 0
    total = 0

    def assert_eq(actual, expected, msg, tolerance=1e-5):
        nonlocal passed, total
        total += 1
        if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
            match = abs(actual - expected) <= tolerance
        else:
            match = actual == expected
        if match:
            passed += 1
            print(f"  [PASS] {msg}")
        else:
            print(f"  [FAIL] {msg} -> Expected: {expected}, Got: {actual}")
            raise AssertionError(f"Test failed: {msg}")

    # 1. Cardinal Bearings: North, East, South, West
    print("\n[Test 1: Cardinal Bearings (0, 90, 180, 270)]")
    assert_eq(initial_bearing_deg(0.0, 0.0, 1.0, 0.0), 0.0, "North cardinal bearing is 0.0°")
    assert_eq(initial_bearing_deg(0.0, 0.0, 0.0, 1.0), 90.0, "East cardinal bearing is 90.0°")
    assert_eq(initial_bearing_deg(0.0, 0.0, -1.0, 0.0), 180.0, "South cardinal bearing is 180.0°")
    assert_eq(initial_bearing_deg(0.0, 0.0, 0.0, -1.0), 270.0, "West cardinal bearing is 270.0°")

    # 2. Northeast Destination
    print("\n[Test 2: Northeast Destination]")
    ne_bearing = initial_bearing_deg(0.0, 0.0, 1.0, 1.0)
    assert_eq(round(ne_bearing, 2), 45.00, "Northeast initial bearing is ~45.0° (spherical geodesic)")

    # 3. Heading Rotation and Relative Arrow Behavior
    print("\n[Test 3: Heading Rotation & Relative Arrow Behavior]")
    # Target Bearing = 45.0°
    # Case A: Heading 45.0° -> Directly Ahead (0.0° turn -> ↑)
    t_a = relative_turn_angle_deg(45.0, 45.0)
    arr_a, lbl_a = turn_angle_to_arrow(t_a)
    assert_eq(t_a, 0.0, "Target 45° with Heading 45° yields turn 0.0°")
    assert_eq(arr_a, "↑", "Heading aligned produces '↑' arrow")
    assert_eq(lbl_a, "AHEAD", "Heading aligned produces 'AHEAD' label")

    # Case B: Heading 0.0° (North) -> Target is 45° to the right (+45.0° -> ↗)
    t_b = relative_turn_angle_deg(45.0, 0.0)
    arr_b, _ = turn_angle_to_arrow(t_b)
    assert_eq(t_b, 45.0, "Target 45° with Heading 0° yields turn +45.0°")
    assert_eq(arr_b, "↗", "Right turn produces '↗' arrow")

    # Case C: Heading 90.0° (East) -> Target is 45° to the left (-45.0° -> ↖)
    t_c = relative_turn_angle_deg(45.0, 90.0)
    arr_c, _ = turn_angle_to_arrow(t_c)
    assert_eq(t_c, -45.0, "Target 45° with Heading 90° yields turn -45.0°")
    assert_eq(arr_c, "↖", "Left turn produces '↖' arrow")

    # Case D: Heading 225.0° (Southwest) -> Target is 180° directly behind (↓)
    t_d = relative_turn_angle_deg(45.0, 225.0)
    arr_d, _ = turn_angle_to_arrow(t_d)
    assert_eq(abs(t_d), 180.0, "Target 45° with Heading 225° yields 180.0° reverse")
    assert_eq(arr_d, "↓", "Direct reversal produces '↓' arrow")

    # 4. Zero-Distance Waypoint
    print("\n[Test 4: Zero-Distance Waypoint (Arrival)]")
    d_zero = haversine_distance_m(47.6062, -122.3321, 47.6062, -122.3321)
    b_zero = initial_bearing_deg(47.6062, -122.3321, 47.6062, -122.3321)
    assert_eq(d_zero, 0.0, "Distance between identical coordinates is strictly 0.0m")
    assert_eq(b_zero, 0.0, "Bearing between identical coordinates returns safe 0.0° without crash")

    wp_zero = Waypoint("SameSpot", 47.6062, -122.3321)
    gps_zero = GPSReading(47.6062, -122.3321, timestamp=time.time(), has_fix=True, accuracy_m=3.0)
    sol_zero = compute_nav_solution(gps_zero, None, wp_zero)
    assert_eq(sol_zero.status, NavStatus.ARRIVED, "Zero-distance resolves to ARRIVED status")
    assert_eq(sol_zero.arrow, "★", "Arrived indicator symbol is '★'")

    # 5. Invalid Latitude / Longitude Validation
    print("\n[Test 5: Coordinate Validation Bounds]")
    assert_eq(validate_wgs84(0.0, 0.0)[0], True, "(0, 0) is valid")
    assert_eq(validate_wgs84(90.0, 180.0)[0], True, "(90, 180) boundary is valid")
    assert_eq(validate_wgs84(-90.0, -180.0)[0], True, "(-90, -180) boundary is valid")
    assert_eq(validate_wgs84(90.001, 0.0)[0], False, "Latitude > 90.0 rejected")
    assert_eq(validate_wgs84(-90.001, 0.0)[0], False, "Latitude < -90.0 rejected")
    assert_eq(validate_wgs84(0.0, 180.001)[0], False, "Longitude > 180.0 rejected")
    assert_eq(validate_wgs84(0.0, -180.001)[0], False, "Longitude < -180.0 rejected")
    assert_eq(validate_wgs84(float("nan"), 0.0)[0], False, "NaN latitude rejected")
    assert_eq(validate_wgs84(0.0, float("inf"))[0], False, "Inf longitude rejected")

    # 6. No GPS Fix
    print("\n[Test 6: No GPS Fix Handling]")
    gps_nofix = GPSReading(45.0, -120.0, timestamp=time.time(), has_fix=False)
    wp_test = Waypoint("Camp", 45.1, -120.1)
    sol_nofix = compute_nav_solution(gps_nofix, None, wp_test)
    assert_eq(sol_nofix.status, NavStatus.NO_FIX, "Unfixed receiver reports NO_GPS_FIX")
    assert_eq(sol_nofix.arrow, "?", "Unfixed receiver gives '?' guidance arrow")

    # 7. Stale GPS Data Check
    print("\n[Test 7: Stale GPS Data]")
    t_fixed = 100000.0
    # Reading taken 25 seconds ago (threshold is 10s)
    gps_stale = GPSReading(45.0, -120.0, timestamp=t_fixed - 25.0, has_fix=True)
    sol_stale = compute_nav_solution(gps_stale, None, wp_test, current_time=t_fixed)
    assert_eq(sol_stale.status, NavStatus.STALE_GPS, "GPS older than threshold reports STALE_GPS")

    # 8. Missing Heading Handling
    print("\n[Test 8: Missing Compass Reading]")
    gps_ok = GPSReading(45.0, -120.0, timestamp=t_fixed, has_fix=True)
    sol_no_compass = compute_nav_solution(gps_ok, None, wp_test, current_time=t_fixed)
    assert_eq(sol_no_compass.status, NavStatus.MISSING_HEADING, "None compass reports MISSING_HEADING status")
    assert_eq(sol_no_compass.arrow, "—", "Missing compass reports '—' arrow without crash")
    assert_eq(round(sol_no_compass.bearing_deg, 1) > 0.0, True, "True bearing is still correctly computed")

    # 9. Magnetic Declination Sign Convention & Refusal to Invent Values
    print("\n[Test 9: Magnetic Declination Sign Convention]")
    # East is Positive (+)
    assert_eq(magnetic_to_true_heading(350.0, +15.0), 5.0, "East declination (+15°) on 350° mag yields 5.0° True")
    # West is Negative (-)
    assert_eq(magnetic_to_true_heading(10.0, -15.0), 355.0, "West declination (-15°) on 10° mag yields 355.0° True")
    # None declination refusal
    refused = False
    try:
        magnetic_to_true_heading(180.0, None)
    except ValueError:
        refused = True
    assert_eq(refused, True, "Engine refuses to convert magnetic heading when declination is None")

    # 10. Waypoint Manager Atomic Persistence
    print("\n[Test 10: Waypoint Manager Atomic CRUD]")
    test_wp_file = scratch / "waypoints_test_scratch.json"
    if test_wp_file.exists():
        test_wp_file.unlink()

    try:
        mgr = WaypointManager(storage_path=str(test_wp_file))
        assert_eq(len(mgr.list_waypoints()), 0, "Initial store is empty")

        # Save
        mgr.add_waypoint("BaseCamp", 46.7865, -121.7350, description="Trailhead shelter")
        mgr.add_waypoint("Camp2", 46.8528, -121.7603, description="High camp")
        assert_eq(len(mgr.list_waypoints()), 2, "Added 2 waypoints")
        assert_eq(mgr.get_active_waypoint().name, "BaseCamp", "First added is default active")

        # Select
        mgr.select_active_waypoint("Camp2")
        assert_eq(mgr.get_active_waypoint().name, "Camp2", "Active waypoint switched to Camp2")

        # Rename
        mgr.rename_waypoint("Camp2", "CampMuir")
        assert_eq(mgr.get_active_waypoint().name, "CampMuir", "Active waypoint reflects rename")
        assert_eq(mgr.get_waypoint("CampMuir").lat, 46.8528, "Coordinates preserved after rename")

        # Atomic Reload from disk
        mgr2 = WaypointManager(storage_path=str(test_wp_file))
        assert_eq(len(mgr2.list_waypoints()), 2, "Reloaded 2 waypoints from disk")
        assert_eq(mgr2.get_active_waypoint().name, "CampMuir", "Reloaded active waypoint preserved")

        # Delete
        mgr2.delete_waypoint("BaseCamp")
        assert_eq(len(mgr2.list_waypoints()), 1, "Deleted 1 waypoint")
        assert_eq(mgr2.get_waypoint("BaseCamp"), None, "Deleted waypoint cannot be fetched")

    finally:
        if test_wp_file.exists():
            test_wp_file.unlink()
        tmp_clean = test_wp_file.with_name(f"{test_wp_file.name}.tmp")
        if tmp_clean.exists():
            tmp_clean.unlink()

    # 11. Known Geodesic Distance Verification
    print("\n[Test 11: Geodesic Benchmark Check]")
    # 1 degree of latitude at the equator should be ~111.195 km
    dist_1deg = haversine_distance_m(0.0, 0.0, 1.0, 0.0)
    assert_eq(round(dist_1deg, 0), 111195.0, "1° latitude distance corresponds to 111,195m")

    print("\n" + "=" * 65)
    print(f" ALL TESTS PASSED: {passed} / {total} assertions verified.")
    print("=" * 65)
    return True


# ==============================================================================
# 7. Main CLI Entry Point
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="SURVIVE-X Offline Navigation Engine")
    parser.add_argument("--test", action="store_true", help="Run automated self-test suite")
    parser.add_argument("--demo", action="store_true", help="Run live navigation terminal simulation")
    parser.add_argument("--steps", type=int, default=10, help="Number of demo simulation steps (default: 10)")
    parser.add_argument("--delay", type=float, default=0.3, help="Demo delay per step in seconds (default: 0.3)")
    parser.add_argument(
        "--nav",
        nargs=4,
        metavar=("CUR_LAT", "CUR_LON", "DEST_LAT", "DEST_LON"),
        type=float,
        help="Quick navigation computation: CUR_LAT CUR_LON DEST_LAT DEST_LON",
    )
    parser.add_argument("--heading", type=float, default=None, help="Device magnetic heading in degrees")
    parser.add_argument("--declination", type=float, default=None, help="Magnetic declination in degrees (+ East, - West)")

    args = parser.parse_args()

    if args.test:
        success = run_tests()
        sys.exit(0 if success else 1)

    if args.demo:
        run_demo(steps=args.steps, delay_sec=args.delay)
        sys.exit(0)

    if args.nav:
        c_lat, c_lon, d_lat, d_lon = args.nav
        gps = GPSReading(lat=c_lat, lon=c_lon, timestamp=time.time(), has_fix=True, sensor_mode=SensorMode.MANUAL)
        compass = (
            CompassReading(magnetic_heading_deg=args.heading, timestamp=time.time())
            if args.heading is not None
            else None
        )
        dest = Waypoint(name="Target", lat=d_lat, lon=d_lon)
        sol = compute_nav_solution(gps, compass, dest, declination_deg=args.declination)
        print(render_terminal_dashboard(gps, compass, dest, sol, declination_deg=args.declination))
        sys.exit(0)

    parser.print_help()


if __name__ == "__main__":
    main()
