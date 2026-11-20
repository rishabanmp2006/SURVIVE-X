#!/usr/bin/env python3
"""
SURVIVE-X — Stage 2: 320x240 Graphical SPI TFT Navigation Simulator
Target Hardware: Arduino UNO Q (2GB RAM, Quad Cortex-A53, 2.4-inch 320x240 SPI TFT, PS2 Joystick)
Host Environment: macOS Darwin arm64 (Apple Silicon M3)

Architecture:
- Strict decoupling: UI Application Logic is completely separated from Display and Input Adapters.
- DisplayAdapter Abstraction: Defines clean 320x240 pixel drawing primitives.
  Implementations:
    1. PygameDisplayAdapter (macOS desktop graphical simulator, scaled 2x or 3x)
    2. HeadlessDisplayAdapter (for automated CI testing and layout boundary verification)
    3. LinuxFramebufferAdapter (future target for Arduino UNO Q SPI TFT /dev/fb0)
- InputAdapter Abstraction: Translates hardware events into discrete Navigation Actions:
  UP, DOWN, LEFT, RIGHT (Joystick deflection), SELECT (Enter / Joy Button), BACK (Escape),
  and simulation shortcuts for GPS / compass testing.
- Reuses navigation.py for all geodesic mathematics, GPS validation, and atomic waypoint persistence.
- Safe offline operation: No LLM dependencies, no internet, no map downloads.
"""

import journey
import argparse
import math
import os
import tempfile
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# Re-use existing Stage 1 navigation module (zero duplication of math)
import navigation as nav

# Stage 3: Offline Places Database & Assistance Discovery
import places as plc
from ai_worker import ChatWorker

# Auto-detect local virtualenv if pygame is not in current interpreter
try:
    import pygame
except ImportError:
    pygame = None


# Canonical assistance category badges for 320x240 display
CATEGORY_BADGES = {
    "settlement": "[SETL]",
    "hospital": "[HOSP]",
    "clinic": "[CLIN]",
    "police": "[POL ]",
    "ranger_station": "[RNGR]",
    "shelter": "[SHTR]",
    "water_source": "[WATR]",
}


# ==============================================================================
# 1. Color Palette & Styling (High-Contrast Outdoor Dark Theme)
# ==============================================================================

def format_distance(metres):
    if metres is None:
        return "N/A"
    return f"{metres / 1000:.2f} km" if metres >= 1000 else f"{metres:,.0f} m"


def format_bearing(degrees):
    return "N/A" if degrees is None else f"{degrees:05.1f}° True"


class Colors:
    BG = (16, 20, 26)                # Deep tactical slate background
    PANEL = (25, 32, 42)             # Container card background
    PANEL_ALT = (32, 40, 52)         # Alternative panel background
    PANEL_BORDER = (46, 58, 74)      # Subtle panel border
    HEADER_BG = (22, 28, 36)         # Top header bar background
    FOOTER_BG = (18, 24, 32)         # Bottom footer bar background
    DIVIDER = (40, 50, 65)           # Header/footer divider line
    
    TEXT_WHITE = (242, 246, 250)     # Crisp primary text
    TEXT_MUTED = (140, 152, 170)     # Secondary / label text
    TEXT_DARK = (90, 100, 115)       # Dim footnote text
    
    CYAN = (0, 220, 255)             # Accent / title / bearing
    CYAN_DIM = (0, 140, 170)         # Subtle cyan
    GREEN = (45, 220, 115)           # OK / Lock / Active destination
    GREEN_DIM = (20, 55, 32)         # Dark green badge background
    AMBER = (255, 190, 35)           # Warning / Stale GPS / Heading missing
    AMBER_DIM = (60, 45, 12)         # Dark amber badge background
    RED = (255, 68, 68)              # Danger / No Fix / Invalid Coords
    RED_DIM = (60, 18, 18)           # Dark red badge background
    
    SEL_BG = (35, 75, 125)           # Selected menu item background
    SEL_BORDER = (0, 210, 255)       # Selected menu item highlight border


# ==============================================================================
# 2. Input & Display Abstraction Layer
# ==============================================================================

class Action(Enum):
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    SELECT = auto()
    BACK = auto()
    BACKSPACE = auto()
    TOGGLE_MODE = auto()       # Cycle Sensor Mode (SIMULATED -> MANUAL -> LIVE GNSS)
    CHAR_INPUT = auto()        # Alphanumeric character input for coordinate editor
    # Simulator shortcuts for testing GPS and Compass
    SIM_ROTATE_LEFT = auto()   # Heading -15°
    SIM_ROTATE_RIGHT = auto()  # Heading +15°
    SIM_MOVE_NORTH = auto()    # Lat +0.001° (~111m)
    SIM_MOVE_SOUTH = auto()    # Lat -0.001°
    SIM_MOVE_EAST = auto()     # Lon +0.001°
    SIM_MOVE_WEST = auto()     # Lon -0.001°
    SIM_TOGGLE_FIX = auto()    # Toggle GPS Fix
    SIM_TOGGLE_STALE = auto()  # Toggle Stale GPS test mode
    SIM_TOGGLE_COMPASS = auto()# Toggle Compass Available
    QUIT = auto()
    NONE = auto()


class DisplayAdapter(ABC):
    """
    Abstract 320x240 pixel display interface.
    Enforces a strict 320x240 pixel coordinate system matching the SPI TFT.
    """
    WIDTH = 320
    HEIGHT = 240

    def measure_text(self, text, size="med"):
        width = 5.5 if size == "small" else (9.5 if size in ("large", "huge") else 7)
        return int(len(text) * width)

    def fit_text(self, text, width, size="med"):
        text = str(text).translate(str.maketrans({"▶": ">", "◀": "<", "▲": "^", "▼": "v", "✓": "+", "⮌": "<", "•": "-"}))
        if self.measure_text(text, size) <= width:
            return text
        while text and self.measure_text(text + "...", size) > width:
            text = text[:-1]
        return text + "..."

    def wrap_text(self, text, width, size="med"):
        lines = []
        line = ""
        for char in str(text):
            if char == "\n" or (line and self.measure_text(line + char, size) > width):
                split = line.rfind(" ") + 1 if char != "\n" else len(line)
                split = split or len(line)
                lines.append(line[:split])
                line = line[split:]
            if char != "\n":
                line += char
        lines.append(line)
        return lines

    def draw_circle(self, cx, cy, radius, color, width=0):
        raise NotImplementedError

    def draw_polygon(self, points, color, width=0):
        raise NotImplementedError

    @abstractmethod
    def clear(self, color: Tuple[int, int, int] = Colors.BG) -> None:
        pass

    @abstractmethod
    def draw_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color: Tuple[int, int, int],
        border_color: Optional[Tuple[int, int, int]] = None,
        border_width: int = 1,
        border_radius: int = 0,
    ) -> None:
        pass

    @abstractmethod
    def draw_line(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        color: Tuple[int, int, int],
        width: int = 1,
    ) -> None:
        pass

    @abstractmethod
    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        size: str = "med",
        color: Optional[Tuple[int, int, int]] = None,
        style: str = "NORMAL",
    ) -> None:
        pass

    def draw_nav_arrow(self, cx, cy, radius, angle_deg, status, label):
        self.draw_circle(cx, cy, radius, (40, 50, 65), 2)
        for x, y in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            self.draw_line(cx + x * radius, cy + y * radius,
                           cx + x * (radius - 4), cy + y * (radius - 4), (80, 95, 115))
        if status == "OK" and angle_deg is not None:
            length = radius - 6
            shape = [(0, -1), (.55, .45), (.20, .25), (.20, .85),
                     (-.20, .85), (-.20, .25), (-.55, .45)]
            angle = math.radians(angle_deg)
            points = [(round(cx + length * (x * math.cos(angle) - y * math.sin(angle))),
                       round(cy + length * (x * math.sin(angle) + y * math.cos(angle)))) for x, y in shape]
            color = Colors.GREEN if abs(angle_deg) < 15 else Colors.CYAN if abs(angle_deg) < 90 else Colors.AMBER
            self.draw_polygon(points, color)
            self.draw_polygon(points, Colors.TEXT_WHITE, 1)
            self.draw_circle(cx, cy, 3, (20, 26, 34))
        elif status == "ARRIVED":
            self.draw_circle(cx, cy, radius - 6, Colors.GREEN, 3)
            self.draw_circle(cx, cy, (radius - 6) // 2, Colors.GREEN, 2)
            self.draw_circle(cx, cy, 4, Colors.TEXT_WHITE)
        else:
            color = Colors.RED if status == "NO_FIX" else Colors.AMBER
            self.draw_circle(cx, cy, radius - 6, color, 2)
            symbol = "-" if status == "MISSING_HEADING" else "?"
            self.draw_text(cx - 6, cy - 12, symbol, size="huge", color=color)

    @abstractmethod
    def present(self) -> None:
        pass


class HeadlessDisplayAdapter(DisplayAdapter):
    """
    In-memory display adapter used for automated testing.
    Verifies that all drawing operations strictly fit within the 320x240 envelope.
    """
    def __init__(self):
        self.draw_log: List[dict] = []
        self.arrow_log: List[dict] = []

    def clear(self, color: Tuple[int, int, int] = Colors.BG) -> None:
        self.draw_log.clear()
        self.arrow_log.clear()

    def draw_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color: Tuple[int, int, int],
        border_color: Optional[Tuple[int, int, int]] = None,
        border_width: int = 1,
        border_radius: int = 0,
    ) -> None:
        if x < 0 or y < 0 or (x + w) > self.WIDTH or (y + h) > self.HEIGHT:
            raise ValueError(f"Rect ({x}, {y}, {w}, {h}) exceeds 320x240 bounds.")
        self.draw_log.append({"type": "rect", "x": x, "y": y, "w": w, "h": h, "color": color})

    def draw_line(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        color: Tuple[int, int, int],
        width: int = 1,
    ) -> None:
        if not (0 <= x1 <= self.WIDTH and 0 <= x2 <= self.WIDTH and 0 <= y1 <= self.HEIGHT and 0 <= y2 <= self.HEIGHT):
            raise ValueError(f"Line from ({x1}, {y1}) to ({x2}, {y2}) exceeds 320x240 bounds.")
        self.draw_log.append({"type": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "color": color})

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        size: str = "med",
        color: Optional[Tuple[int, int, int]] = None,
        style: str = "NORMAL",
    ) -> None:
        if x < 0 or y < 0 or x >= self.WIDTH or y >= self.HEIGHT:
            raise ValueError(f"Text '{text}' start pos ({x}, {y}) out of 320x240 bounds.")
        text = self.fit_text(text, self.WIDTH - x, size)
        self.draw_log.append({"type": "text", "x": x, "y": y, "text": text, "size": size, "style": style})

    def draw_nav_arrow(
        self,
        cx: int,
        cy: int,
        radius: int,
        angle_deg: Optional[float],
        status: str,
        label: str,
    ) -> None:
        if (cx - radius) < 0 or (cx + radius) > self.WIDTH or (cy - radius) < 0 or (cy + radius) > self.HEIGHT:
            raise ValueError(f"Arrow at ({cx}, {cy}) with radius {radius} exceeds screen bounds.")
        self.arrow_log.append({"cx": cx, "cy": cy, "radius": radius, "angle_deg": angle_deg, "status": status, "label": label})
        super().draw_nav_arrow(cx, cy, radius, angle_deg, status, label)

    def draw_circle(self, cx, cy, radius, color, width=0):
        if not (radius >= 0 and radius <= cx < self.WIDTH - radius and radius <= cy < self.HEIGHT - radius):
            raise ValueError("Circle outside display")
        self.draw_log.append({"type": "circle", "cx": cx, "cy": cy, "radius": radius, "color": color, "width": width})

    def draw_polygon(self, points, color, width=0):
        if any(not (0 <= x < self.WIDTH and 0 <= y < self.HEIGHT) for x, y in points):
            raise ValueError("Polygon outside display")
        self.draw_log.append({"type": "polygon", "points": points, "color": color, "width": width})

    def present(self) -> None:
        pass


class PygameDisplayAdapter(DisplayAdapter):
    """
    Desktop Pygame renderer simulating the 320x240 SPI TFT display on macOS.
    Draws to an internal 320x240 Surface, then scales up 2x or 3x for crisp desktop viewing.
    """
    def __init__(self, scale: int = 3, title: str = "SURVIVE-X — 320×240 Tactical TFT Simulator"):
        if pygame is None:
            raise RuntimeError("Pygame is not available in the current Python environment.")
        
        self.scale = max(1, min(5, scale))
        self.title = title

        pygame.init()
        pygame.display.set_caption(self.title)

        # Logical 320x240 internal canvas
        self.surface = pygame.Surface((self.WIDTH, self.HEIGHT))
        # Window display surface (scaled)
        self.window = pygame.display.set_mode((self.WIDTH * self.scale, self.HEIGHT * self.scale))

        pygame.font.init()
        self.fonts = {
            "small": pygame.font.Font(None, 14),
            "med": pygame.font.Font(None, 18),
            "large": pygame.font.Font(None, 24),
            "huge": pygame.font.Font(None, 34),
        }
        kannada = pygame.font.match_font("notosanskannada,kannadasangammn")
        self.kannada_fonts = {}
        if kannada:
            self.kannada_fonts = {name: pygame.font.Font(kannada, pixels)
                                  for name, pixels in (("small", 11), ("med", 13), ("large", 18), ("huge", 24))}


    def font_for(self, text, size):
        if any("\u0c80" <= char <= "\u0cff" for char in text):
            return self.kannada_fonts.get(size, self.fonts[size])
        return self.fonts.get(size, self.fonts["med"])

    def measure_text(self, text, size="med"):
        return self.font_for(text, size).size(text)[0]

    def clear(self, color: Tuple[int, int, int] = Colors.BG) -> None:
        self.surface.fill(color)

    def draw_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color: Tuple[int, int, int],
        border_color: Optional[Tuple[int, int, int]] = None,
        border_width: int = 1,
        border_radius: int = 0,
    ) -> None:
        rect = pygame.Rect(x, y, w, h)
        if border_radius > 0:
            pygame.draw.rect(self.surface, color, rect, border_radius=border_radius)
            if border_color is not None:
                pygame.draw.rect(self.surface, border_color, rect, width=border_width, border_radius=border_radius)
        else:
            pygame.draw.rect(self.surface, color, rect)
            if border_color is not None:
                pygame.draw.rect(self.surface, border_color, rect, width=border_width)

    def draw_line(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        color: Tuple[int, int, int],
        width: int = 1,
    ) -> None:
        pygame.draw.line(self.surface, color, (x1, y1), (x2, y2), width)

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        size: str = "med",
        color: Optional[Tuple[int, int, int]] = None,
        style: str = "NORMAL",
    ) -> None:
        col = color if color is not None else Colors.TEXT_WHITE
        text = self.fit_text(text, self.WIDTH - x, size)
        font = self.font_for(text, size)
        rendered = font.render(text, True, col)
        self.surface.blit(rendered, (x, y))

    def draw_circle(self, cx, cy, radius, color, width=0):
        pygame.draw.circle(self.surface, color, (cx, cy), radius, width)

    def draw_polygon(self, points, color, width=0):
        pygame.draw.polygon(self.surface, color, points, width)

    def present(self) -> None:
        scaled = pygame.transform.scale(self.surface, (self.WIDTH * self.scale, self.HEIGHT * self.scale))
        self.window.blit(scaled, (0, 0))
        pygame.display.flip()

    def close(self) -> None:
        pygame.quit()


# ==============================================================================
# 3. Input Adapter Layer
# ==============================================================================

class InputAdapter(ABC):
    def __init__(self):
        self.last_char: Optional[str] = None

    @abstractmethod
    def read_action(self, timeout_sec: float = 0.05) -> Action:
        pass


class PygameInputAdapter(InputAdapter):
    """
    Translates desktop Pygame keyboard and window events into Navigation Actions.
    Simulates PS2 analog joystick deflections and push buttons.
    """
    def __init__(self, app_ref=None):
        super().__init__()
        self.app_ref = app_ref
        self.queue: List[Tuple[Action, Optional[str]]] = []

    def read_action(self, timeout_sec: float = 0.05) -> Action:
        if self.queue:
            act, ch = self.queue.pop(0)
            self.last_char = ch
            return act

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.queue.append((Action.QUIT, None))
            elif event.type == pygame.KEYDOWN:
                is_text_mode = False
                if self.app_ref:
                    curr = getattr(self.app_ref, "current_screen", None)
                    if curr in (Screen.COORD_INPUT, Screen.SEARCH_KEYBOARD, Screen.ASSISTANT_INFO, Screen.AI_KEYBOARD):
                        is_text_mode = True
                    elif curr == Screen.PLACES_SEARCH and getattr(self.app_ref, "places_search_field", 0) == 0:
                        is_text_mode = True
                
                if event.key == pygame.K_ESCAPE:
                    self.queue.append((Action.BACK, None))
                elif event.key == pygame.K_BACKSPACE:
                    self.queue.append((Action.BACKSPACE, None))
                elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self.queue.append((Action.SELECT, None))
                elif event.key == pygame.K_UP:
                    self.queue.append((Action.UP, None))
                elif event.key == pygame.K_DOWN:
                    self.queue.append((Action.DOWN, None))
                elif event.key == pygame.K_LEFT:
                    self.queue.append((Action.LEFT, None))
                elif event.key == pygame.K_RIGHT:
                    self.queue.append((Action.RIGHT, None))
                elif event.key == pygame.K_TAB:
                    self.queue.append((Action.DOWN, None))
                elif is_text_mode:
                    if event.unicode and event.unicode.isprintable() and len(event.unicode) == 1:
                        self.queue.append((Action.CHAR_INPUT, event.unicode))
                else:
                    if event.key == pygame.K_SPACE:
                        self.queue.append((Action.SELECT, None))
                    elif event.key == pygame.K_w:
                        self.queue.append((Action.SIM_MOVE_NORTH, None))
                    elif event.key == pygame.K_s:
                        self.queue.append((Action.SIM_MOVE_SOUTH, None))
                    elif event.key == pygame.K_d:
                        self.queue.append((Action.SIM_MOVE_EAST, None))
                    elif event.key == pygame.K_a:
                        self.queue.append((Action.SIM_MOVE_WEST, None))
                    elif event.key in (pygame.K_h, pygame.K_LEFTBRACKET):
                        self.queue.append((Action.SIM_ROTATE_LEFT, None))
                    elif event.key in (pygame.K_l, pygame.K_RIGHTBRACKET):
                        self.queue.append((Action.SIM_ROTATE_RIGHT, None))
                    elif event.key == pygame.K_g:
                        self.queue.append((Action.SIM_TOGGLE_FIX, None))
                    elif event.key == pygame.K_t:
                        self.queue.append((Action.SIM_TOGGLE_STALE, None))
                    elif event.key == pygame.K_c:
                        self.queue.append((Action.SIM_TOGGLE_COMPASS, None))
                    elif event.key == pygame.K_m:
                        self.queue.append((Action.TOGGLE_MODE, None))
                    elif event.key == pygame.K_q:
                        self.queue.append((Action.QUIT, None))

        if self.queue:
            act, ch = self.queue.pop(0)
            self.last_char = ch
            return act
        self.last_char = None
        return Action.NONE


class ScriptedInputAdapter(InputAdapter):
    """Feeds a scripted sequence of actions for automated testing."""
    def __init__(self, actions: List[Union[Action, Tuple[Action, str]]]):
        super().__init__()
        self.actions = list(actions)

    def read_action(self, timeout_sec: float = 0.0) -> Action:
        if self.actions:
            item = self.actions.pop(0)
            if isinstance(item, tuple):
                act, ch = item
                self.last_char = ch
                return act
            self.last_char = None
            return item
        self.last_char = None
        return Action.NONE


# ==============================================================================
# 4. Application State & Screen Model
# ==============================================================================

SEARCH_KEYS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") + ["SPACE", "DEL", "CLR", "FIND"]


class Screen(Enum):
    HOME = auto()
    NAVIGATION = auto()
    WAYPOINTS = auto()
    STATUS = auto()
    WAYPOINT_ACTIONS = auto()
    ASSISTANT_INFO = auto()
    COORD_INPUT = auto()
    PLACES_SEARCH = auto()
    PLACES_RESULTS = auto()
    PLACE_DETAILS = auto()
    SEARCH_KEYBOARD = auto()
    AI_KEYBOARD = auto()


class SURVIVEXApp:
    """
    Core interactive navigation UI engine.
    Completely decoupled from display hardware and input sources.
    Strictly enforces 320x240 screen boundary constraints.
    """

    def __init__(self, storage_path: str = "waypoints.json", db_path: Optional[str] = None, async_search: bool = False, live_sensors=None, live_declination=None, recorder=None):
        self.recorder = recorder
        self.last_ai_message = None
        self.ai_record_session = None
        self.current_screen = Screen.HOME
        self.running = True
        self.ai = ChatWorker()
        self.ai_input = ""
        self.ai_focus = 0
        self.ai_scroll = 0
        self.ai_key = 0
        self.ai_snapshot = None

        # Explicit Sensor Mode (Stage 2.5)
        self.sensor_mode = nav.SensorMode.SIMULATED
        self.live_sensors = live_sensors
        self.live_declination = live_declination

        # Simulated sensors (Mount Rainier Trailhead baseline)
        self.sim_lat = 46.786500
        self.sim_lon = -121.735000
        self.sim_alt = 1600.0
        self.sim_has_fix = True
        self.sim_stale_mode = False
        self.sim_accuracy_m = 3.2
        self.sim_hdop = 1.1
        self.sim_sats = 10
        self.sim_gps_timestamp = time.time()
        self.last_clock_sec = int(self.sim_gps_timestamp)

        # Manual coordinates (defaults to Bangalore baseline)
        self.manual_entered = False
        self.manual_lat = 12.971600
        self.manual_lon = 77.594600
        self.manual_alt: Optional[float] = None

        # Coordinate Input Editor State (Stage 2.5)
        self.coord_target_type = 0  # 0: DESTINATION, 1: CURRENT POSITION
        self.coord_lat_str = "+12.971600"
        self.coord_lon_str = "+077.594600"
        self.coord_alt_str = ""
        self.coord_name_str = "Mysore"
        self.coord_active_field = 1  # 0: Target, 1: Lat, 2: Lon, 3: Name, 4: Alt, 5: Keypad, 6: Apply, 7: Cancel
        self.coord_keypad_row = 0
        self.coord_keypad_col = 0
        self.coord_error: Optional[str] = None
        self.coord_last_text_field = 1

        # Simulated compass
        self.sim_mag_heading = 330.0
        self.sim_compass_available = True
        self.declination_deg = 14.5  # Pacific Northwest default

        # Waypoint manager (Atomic persistence)
        self.waypoint_mgr = nav.WaypointManager(storage_path=storage_path)

        demo_points = [
            ("Columbia Crest", 46.852886, -121.760374, 4392.0, "Summit"),
            ("Camp Muir", 46.835500, -121.732000, 3070.0, "High shelter"),
            ("Paradise Lodge", 46.786500, -121.735000, 1600.0, "Trailhead"),
        ]
        if not self.waypoint_mgr.storage_path.exists() and not self.waypoint_mgr.load_error:
            for name, lat, lon, altitude, description in demo_points:
                self.waypoint_mgr.add_waypoint(name, lat, lon, altitude, description,
                                               source=nav.SensorMode.SIMULATED.value)

        # Initial starting position for "Return to start" feature
        self.session_starts = {}
        self.capture_start()

        # Stage 3: Offline Places Database & Assistance Discovery
        selected_db = db_path or (plc.REAL_DB_FILE if Path(plc.REAL_DB_FILE).exists() else plc.DB_FILE_DEFAULT)
        self.places_db = plc.PlacesDatabase(selected_db, read_only=True)
        self.coverage_metadata = self.places_db.get_coverage_metadata()
        self.search_executor = ThreadPoolExecutor(max_workers=1) if async_search else None
        self.search_future = None
        self.search_pending = None
        self.search_generation = 0
        self.search_busy = False
        self.places_coverage = "UNKNOWN COVERAGE"
        self.places_outcome = ""
        self.keyboard_index = 0
        self.detail_page = 0
        # Older versions discarded the place ID when selecting a fixture.
        for wp in self.waypoint_mgr.list_waypoints():
            if wp.source:
                continue
            if (wp.name, wp.lat, wp.lon, wp.altitude_m, wp.description) in demo_points:
                wp.source = nav.SensorMode.SIMULATED.value
            elif wp.description == "Manual Destination" or "SYNTHETIC_TEST_FIXTURE" in wp.description:
                fixture = self.places_db.fixture_at(wp.lat, wp.lon)
                if fixture:
                    wp.source = fixture.source
                    if wp.name == fixture.name:
                        wp.place_id = fixture.id
        self.results_reference = None
        self.nearby_radius = None
        self.places_query = ""
        self.places_categories: List[Optional[str]] = [None] + list(plc.CANONICAL_CATEGORIES)
        self.places_selected_cat_idx = 0
        self.places_search_field = 0  # 0: Query, 1: Category, 2: Name Search, 3: Nearby Search, 4: Back
        self.places_results: List[plc.Place] = []
        self.places_results_idx = 0
        self.selected_place: Optional[plc.Place] = None
        self.place_details_btn_idx = 0
        self.places_is_outside_coverage = False
        self.places_search_performed = False

        # Menu navigation indices
        self.home_menu_idx = 0
        self.home_menu_items = [
            ("1. Live Navigation", Screen.NAVIGATION),
            ("2. Saved Locations", Screen.WAYPOINTS),
            ("3. Places & Nearby", Screen.PLACES_SEARCH),
            ("4. Enter Coordinates", Screen.COORD_INPUT),
            ("5. Sensor Diagnostics", Screen.STATUS),
            ("6. Offline AI Assistant", Screen.ASSISTANT_INFO),
        ]

        self.waypoints_idx = 0
        self.action_menu_idx = 0
        self.selected_waypoint: Optional[nav.Waypoint] = None
        self.notification_msg = "Waypoint store unreadable; saves disabled" if self.waypoint_mgr.load_error else "SURVIVE-X Tactical Simulator Ready"
        self.notification_time = time.time()

    def navigation_state(self):
        gps = self.get_current_gps()
        compass = self.get_current_compass()
        return nav.NavigationState.from_readings(gps, compass, self.declination_deg if self.sensor_mode == nav.SensorMode.SIMULATED else self.live_declination)

    @property
    def start_position(self):
        return self.session_starts.get(self.sensor_mode)

    def capture_start(self):
        state = self.navigation_state()
        if state.has_position and self.start_position is None:
            gps = state.gps
            self.session_starts[self.sensor_mode] = nav.Waypoint(
                "Start Position", gps.lat, gps.lon, gps.altitude_m, created_at=time.time(), source=self.sensor_mode.value
            )

    def cycle_sensor_mode(self) -> None:
        """Cycles between SIMULATED, MANUAL COORDINATES, and LIVE GNSS."""
        if self.sensor_mode == nav.SensorMode.SIMULATED:
            self.sensor_mode = nav.SensorMode.MANUAL
        elif self.sensor_mode == nav.SensorMode.MANUAL:
            self.sensor_mode = nav.SensorMode.LIVE_GNSS
        else:
            self.sensor_mode = nav.SensorMode.SIMULATED
        self.set_notification(f"Sensor Mode: {self.sensor_mode.value}")

    def set_manual_coordinates(self, lat: float, lon: float, alt: Optional[float] = None) -> bool:
        """Sets user manual position coordinates with validation."""
        valid, err = nav.validate_wgs84(lat, lon)
        if not valid:
            self.coord_error = err
            return False
        if alt is not None and not nav.finite_number(alt):
            self.coord_error = "Altitude must be finite."
            return False
        self.manual_entered = True
        self.manual_lat = float(lat)
        self.manual_lon = float(lon)
        self.manual_alt = float(alt) if alt is not None else None
        self.sensor_mode = nav.SensorMode.MANUAL
        self.capture_start()
        self.coord_error = None
        self.set_notification(f"Position: ({lat:+.5f}°, {lon:+.5f}°)")
        return True

    def set_manual_destination(self, lat: float, lon: float, name: str = "", alt: Optional[float] = None) -> bool:
        """Sets active destination coordinates with validation."""
        valid, err = nav.validate_wgs84(lat, lon)
        if not valid:
            self.coord_error = err
            return False
        target_name = name.strip() or f"WP_{time.strftime('%H%M%S')}"
        if alt is not None and not nav.finite_number(alt):
            self.coord_error = "Altitude must be finite."
            return False
        wp = self.waypoint_mgr.add_waypoint(
            name=target_name,
            lat=float(lat),
            lon=float(lon),
            altitude_m=float(alt) if alt is not None else None,
            description="Manual Destination", source="MANUAL", activate=True,
        )
        self.coord_error = None
        self.set_notification(f"Target set to '{wp.display_name}'")
        return True

    def validate_and_apply_coords(self) -> bool:
        """Validates strings in coordinate editor and applies them."""
        # 1. Parse Latitude
        try:
            lat = float(self.coord_lat_str.strip())
        except (ValueError, TypeError):
            self.coord_error = "Latitude must be a valid numeric coordinate."
            return False

        # 2. Parse Longitude
        try:
            lon = float(self.coord_lon_str.strip())
        except (ValueError, TypeError):
            self.coord_error = "Longitude must be a valid numeric coordinate."
            return False

        # 3. WGS84 Validation
        valid, err = nav.validate_wgs84(lat, lon)
        if not valid:
            self.coord_error = err
            return False

        # 4. Optional Altitude
        alt_val = None
        if self.coord_alt_str.strip():
            try:
                alt_val = float(self.coord_alt_str.strip())
                if not nav.finite_number(alt_val):
                    raise ValueError("Non-finite altitude")
            except ValueError:
                self.coord_error = "Altitude must be a valid numeric value or blank."
                return False

        self.coord_error = None

        if self.coord_target_type == 0:
            # Set Destination Waypoint
            name = self.coord_name_str.strip() or "Manual Target"
            self.set_manual_destination(lat, lon, name=name, alt=alt_val)
            self.current_screen = Screen.NAVIGATION
            return True
        else:
            # Set Current Position
            self.set_manual_coordinates(lat, lon, alt=alt_val)
            self.current_screen = Screen.NAVIGATION
            return True

    def execute_places_name_search(self, state=None) -> None:
        if self.recorder:
            self.recorder.emit('search', 'user', query=self.places_query[:128],
                               category=self.places_categories[self.places_selected_cat_idx])
        self.start_places_search(None, state)

    def execute_places_nearby_search(self, radius_km=50.0, state=None) -> None:
        if self.recorder:
            self.recorder.emit('search', 'user', radius_km=radius_km,
                               category=self.places_categories[self.places_selected_cat_idx])
        self.start_places_search(radius_km, state)

    @staticmethod
    def query_places(db, query, category, reference, radius):
        lat, lon = reference
        coverage = db.coverage_state(lat, lon)
        if radius is None:
            results = db.search_by_name(query, category, limit=30, ref_lat=lat, ref_lon=lon)
        elif lat is None:
            return [], coverage, "POSITION UNAVAILABLE"
        else:
            results = db.search_nearby(lat, lon, radius, category, limit=25)
        outcome = "MATCHES" if results else "NO MATCH"
        if coverage == "EMPTY DATABASE":
            outcome = coverage
        return results, coverage, outcome

    @staticmethod
    def search_in_worker(path, request):
        db = plc.PlacesDatabase(path, read_only=True)
        try:
            return SURVIVEXApp.query_places(db, *request)
        finally:
            db.close()

    def start_places_search(self, radius, state=None):
        state = state or self.navigation_state()
        reference = (state.gps.lat, state.gps.lon) if state.has_position else (None, None)
        self.results_reference = reference
        self.nearby_radius = radius
        self.search_generation += 1
        self.places_search_performed = True
        self.places_results = []
        self.places_results_idx = 0
        self.current_screen = Screen.PLACES_RESULTS
        request = (self.places_query, self.places_categories[self.places_selected_cat_idx], reference, radius)
        if self.search_executor:
            self.search_pending = (self.search_generation, request)
            self.search_busy = True
            self.poll_search()
        else:
            self.apply_search_result(self.query_places(self.places_db, *request))

    def apply_search_result(self, result):
        self.places_results, self.places_coverage, self.places_outcome = result
        self.places_is_outside_coverage = self.places_coverage == "OUTSIDE COVERAGE"
        self.search_busy = False
        label = "TEST DATA" if any(p.is_test_data for p in self.places_results) else "OSM / unverified"
        self.set_notification(f"{len(self.places_results)} results | {label}")

    def poll_search(self):
        changed = False
        if self.search_future and self.search_future.done():
            generation, future = self.search_future_generation, self.search_future
            self.search_future = None
            if generation == self.search_generation:
                try:
                    self.apply_search_result(future.result())
                except Exception as exc:
                    self.search_busy = False
                    self.places_results = []
                    self.places_outcome = "SEARCH ERROR"
                    self.set_notification(str(exc))
                changed = True
        if self.search_pending and self.search_future is None:
            generation, request = self.search_pending
            self.search_pending = None
            self.search_future_generation = generation
            self.search_future = self.search_executor.submit(self.search_in_worker, str(self.places_db.db_path), request)
        return changed

    def close(self):
        self.ai.close()
        if self.recorder:
            self.record_state()
            self.recorder.close()
        if self.search_executor:
            self.search_executor.shutdown(wait=True, cancel_futures=True)
        self.places_db.close()

    def get_current_gps(self) -> nav.GPSReading:
        if self.sensor_mode == nav.SensorMode.SIMULATED:
            return nav.GPSReading(
                lat=self.sim_lat,
                lon=self.sim_lon,
                altitude_m=self.sim_alt,
                timestamp=self.sim_gps_timestamp,
                has_fix=self.sim_has_fix,
                accuracy_m=self.sim_accuracy_m,
                hdop=self.sim_hdop,
                satellites=self.sim_sats,
                sensor_mode=nav.SensorMode.SIMULATED,
            )
        elif self.sensor_mode == nav.SensorMode.MANUAL:
            return nav.GPSReading(
                lat=self.manual_lat,
                lon=self.manual_lon,
                altitude_m=self.manual_alt,
                timestamp=time.time(),
                has_fix=self.manual_entered,
                accuracy_m=None,
                hdop=None,
                satellites=None,
                sensor_mode=nav.SensorMode.MANUAL,
            )
        else: # LIVE_GNSS
            if self.live_sensors is not None:
                return self.live_sensors.gps()
            return nav.GPSReading(
                lat=0.0,
                lon=0.0,
                altitude_m=None,
                timestamp=time.time(),
                has_fix=False,
                accuracy_m=None,
                hdop=None,
                satellites=0,
                sensor_mode=nav.SensorMode.LIVE_GNSS,
            )

    def get_current_compass(self, current_time: Optional[float] = None) -> Optional[nav.CompassReading]:
        if self.sensor_mode != nav.SensorMode.SIMULATED:
            return self.live_sensors.compass() if self.live_sensors is not None else None
        if not self.sim_compass_available:
            return None
        ts = time.time() if current_time is None else current_time
        return nav.CompassReading(
            magnetic_heading_deg=self.sim_mag_heading,
            timestamp=ts,
            is_calibrated=True,
        )

    def set_notification(self, msg: str) -> None:
        self.notification_msg = msg
        self.notification_time = time.time()

    def record_state(self):
        recorder = self.recorder
        if recorder.error or recorder.closed:
            return
        recorder.observe(self.navigation_state(), self.waypoint_mgr.get_active_waypoint())
        if recorder.ai and self.ai_record_session == recorder.id:
            messages = self.ai.messages
            if messages and messages[-1] is not self.last_ai_message:
                self.last_ai_message = messages[-1]
                role, text = messages[-1]
                if role == 'AI':
                    recorder.emit('ai_response', 'AI unverified', text=text[:8192])
                    self.ai_record_session = None

    def tick(self, current_time: Optional[float] = None) -> bool:
        """
        Periodic application tick called during UI event loop iterations.
        Maintains active sensor feeds:
        - If simulated GPS has fix and is not in simulated stale test mode, keeps timestamp fresh.
        - Checks if clock second has rolled over to trigger display refresh.
        Returns True if screen state changed.
        """
        now = time.time() if current_time is None else current_time
        snapshot = (self.ai.status, self.ai.error, len(self.ai.messages))
        ai_changed = snapshot != self.ai_snapshot
        self.ai_snapshot = snapshot
        state_changed = (self.poll_search() if self.search_executor else False) or ai_changed
        if self.current_screen == Screen.PLACES_RESULTS:
            previous_generation = self.search_generation
            self.refresh_places_results(self.navigation_state())
            state_changed = state_changed or previous_generation != self.search_generation

        if self.sensor_mode == nav.SensorMode.LIVE_GNSS and self.start_position is None:
            self.capture_start()
        if self.sim_has_fix and not self.sim_stale_mode:
            # Active sensor stream simulation: Reading stays fresh as time advances
            self.sim_gps_timestamp = now

        if self.recorder:
            self.record_state()
        curr_sec = int(now)
        if curr_sec != self.last_clock_sec:
            self.last_clock_sec = curr_sec
            state_changed = True

        return state_changed

    def handle_action(self, action: Action, char: Optional[str] = None) -> None:
        """Processes discrete joystick/keyboard input."""
        try:
            before = self.current_screen
            if self.recorder and self.ai_record_session and before == Screen.ASSISTANT_INFO:
                self.record_state()
            self._handle_action(action, char)
            if self.recorder and self.current_screen != before:
                self.recorder.emit('screen', 'user', action=action.name,
                                   previous=before.name, screen=self.current_screen.name)
            if self.recorder and action in (Action.SELECT, Action.TOGGLE_MODE):
                self.record_state()
        except (ValueError, OSError) as exc:
            self.set_notification(str(exc))
            if self.current_screen == Screen.COORD_INPUT:
                self.coord_error = str(exc)

    def _handle_action(self, action: Action, char: Optional[str]) -> None:
        if action == Action.QUIT:
            self.running = False
            return

        # Global sensor mode toggle (M key)
        if action == Action.TOGGLE_MODE:
            self.cycle_sensor_mode()
            return

        # Global sensor simulation controls
        if action == Action.SIM_ROTATE_LEFT:
            self.sim_mag_heading = (self.sim_mag_heading - 15.0) % 360.0
            self.set_notification(f"Heading rotated to {self.sim_mag_heading:.0f}° Mag (SIM)")
            return
        elif action == Action.SIM_ROTATE_RIGHT:
            self.sim_mag_heading = (self.sim_mag_heading + 15.0) % 360.0
            self.set_notification(f"Heading rotated to {self.sim_mag_heading:.0f}° Mag (SIM)")
            return
        elif action in (Action.SIM_MOVE_NORTH, Action.SIM_MOVE_SOUTH, Action.SIM_MOVE_EAST, Action.SIM_MOVE_WEST):
            state = self.navigation_state()
            if self.sensor_mode == nav.SensorMode.LIVE_GNSS or not state.has_position:
                self.set_notification("Movement unavailable")
                return
            lat, lon = state.gps.lat, state.gps.lon
            if action == Action.SIM_MOVE_NORTH:
                lat = min(90.0, lat + 0.001)
            elif action == Action.SIM_MOVE_SOUTH:
                lat = max(-90.0, lat - 0.001)
            elif action == Action.SIM_MOVE_EAST:
                lon = (lon + 0.001 + 180) % 360 - 180
            else:
                lon = (lon - 0.001 + 180) % 360 - 180
            if self.sensor_mode == nav.SensorMode.MANUAL:
                self.manual_lat, self.manual_lon = lat, lon
            else:
                self.sim_lat, self.sim_lon = lat, lon
                self.sim_gps_timestamp = time.time()
            self.set_notification(f"Position updated ({self.sensor_mode.value})")
            return
        elif action == Action.SIM_TOGGLE_FIX:
            self.sim_has_fix = not self.sim_has_fix
            if self.sim_has_fix and not self.sim_stale_mode:
                self.sim_gps_timestamp = time.time()
            self.set_notification(f"SIM fix {'enabled' if self.sim_has_fix else 'disabled'}")
            return
        elif action == Action.SIM_TOGGLE_STALE:
            self.sim_stale_mode = not self.sim_stale_mode
            if self.sim_stale_mode:
                self.sim_gps_timestamp = time.time() - 30.0
                self.set_notification("Simulated Stale GPS Fix (>30s old)")
            else:
                self.sim_gps_timestamp = time.time()
                self.set_notification("Simulated Fresh GPS Fix Restored")
            return
        elif action == Action.SIM_TOGGLE_COMPASS:
            self.sim_compass_available = not self.sim_compass_available
            self.set_notification(f"SIM compass toggled to {'CALIBRATED' if self.sim_compass_available else 'UNAVAILABLE'}")
            return

        # Screen-specific navigation logic
        if self.current_screen == Screen.HOME:
            if action == Action.UP:
                self.home_menu_idx = (self.home_menu_idx - 1) % len(self.home_menu_items)
            elif action == Action.DOWN:
                self.home_menu_idx = (self.home_menu_idx + 1) % len(self.home_menu_items)
            elif action == Action.SELECT:
                self.current_screen = self.home_menu_items[self.home_menu_idx][1]
            elif action == Action.BACK:
                self.running = False

        elif self.current_screen == Screen.NAVIGATION:
            if action in (Action.BACK, Action.SELECT):
                self.current_screen = Screen.HOME

        elif self.current_screen == Screen.WAYPOINTS:
            wps = self.waypoint_mgr.list_waypoints()
            total_items = 2 + len(wps)  # [0: Save Current, 1: Return to Start, 2..: Waypoints]
            if action == Action.UP:
                self.waypoints_idx = (self.waypoints_idx - 1) % total_items
            elif action == Action.DOWN:
                self.waypoints_idx = (self.waypoints_idx + 1) % total_items
            elif action == Action.BACK:
                self.current_screen = Screen.HOME
            elif action == Action.SELECT:
                if self.waypoints_idx == 0:
                    if self.recorder:
                        self.recorder.emit('request', 'user', command='Save current waypoint')
                    # Save current position
                    name = f"WP_{time.strftime('%H%M%S')}"
                    state = self.navigation_state()
                    if not state.has_position:
                        self.set_notification("Cannot save: position unavailable")
                        return
                    gps = state.gps
                    wp = self.waypoint_mgr.add_waypoint(name, gps.lat, gps.lon, gps.altitude_m,
                                                        "Saved via UI", source=self.sensor_mode.value)
                    self.set_notification(f"Saved {wp.display_name}")
                elif self.waypoints_idx == 1:
                    if self.recorder:
                        self.recorder.emit('request', 'user', command='Return to start')
                    start = self.start_position
                    if start is None or not self.navigation_state().has_position:
                        self.set_notification("START UNAVAILABLE")
                        return
                    wp = self.waypoint_mgr.add_waypoint("Start Point", start.lat, start.lon,
                                                        start.altitude_m, "Starting Point",
                                                        source=start.source, activate=True)
                    self.set_notification(f"Target: {wp.display_name}")
                    self.current_screen = Screen.NAVIGATION
                else:
                    wp_index = self.waypoints_idx - 2
                    if 0 <= wp_index < len(wps):
                        self.selected_waypoint = wps[wp_index]
                        self.action_menu_idx = 0
                        self.current_screen = Screen.WAYPOINT_ACTIONS

        elif self.current_screen == Screen.WAYPOINT_ACTIONS:
            # Action sub-menu for selected waypoint
            actions = ["1. Set as Destination", "2. Rename Waypoint", "3. Delete Waypoint", "4. Cancel"]
            if action == Action.UP:
                self.action_menu_idx = (self.action_menu_idx - 1) % len(actions)
            elif action == Action.DOWN:
                self.action_menu_idx = (self.action_menu_idx + 1) % len(actions)
            elif action == Action.BACK:
                self.current_screen = Screen.WAYPOINTS
            elif action == Action.SELECT:
                if self.selected_waypoint:
                    if self.action_menu_idx == 0:
                        self.waypoint_mgr.select_active_waypoint(self.selected_waypoint.name)
                        self.set_notification(f"Target: {self.selected_waypoint.display_name}")
                        self.current_screen = Screen.NAVIGATION
                    elif self.action_menu_idx == 1:
                        new_name = f"{self.selected_waypoint.name}_R"
                        self.waypoint_mgr.rename_waypoint(self.selected_waypoint.name, new_name)
                        self.set_notification(f"Renamed: {self.selected_waypoint.display_name}")
                        self.current_screen = Screen.WAYPOINTS
                    elif self.action_menu_idx == 2:
                        self.waypoint_mgr.delete_waypoint(self.selected_waypoint.name)
                        self.set_notification(f"Deleted: {self.selected_waypoint.display_name}")
                        self.current_screen = Screen.WAYPOINTS
                    elif self.action_menu_idx == 3:
                        self.current_screen = Screen.WAYPOINTS

        elif self.current_screen == Screen.COORD_INPUT:
            keypad_grid = [
                ["1", "2", "3", "+"],
                ["4", "5", "6", "-"],
                ["7", "8", "9", "."],
                ["DEL", "0", "CLR", "OK"],
            ]
            if self.coord_active_field in (1, 2, 3, 4):
                self.coord_last_text_field = self.coord_active_field

            if action == Action.CHAR_INPUT and char:
                tf = self.coord_last_text_field if self.coord_active_field == 5 else self.coord_active_field
                if tf == 1: self.coord_lat_str += char
                elif tf == 2: self.coord_lon_str += char
                elif tf == 3: self.coord_name_str += char
                elif tf == 4: self.coord_alt_str += char
                return

            if action == Action.BACKSPACE:
                tf = self.coord_last_text_field if self.coord_active_field == 5 else self.coord_active_field
                if tf == 1 and self.coord_lat_str: self.coord_lat_str = self.coord_lat_str[:-1]
                elif tf == 2 and self.coord_lon_str: self.coord_lon_str = self.coord_lon_str[:-1]
                elif tf == 3 and self.coord_name_str: self.coord_name_str = self.coord_name_str[:-1]
                elif tf == 4 and self.coord_alt_str: self.coord_alt_str = self.coord_alt_str[:-1]
                return

            if action == Action.UP:
                if self.coord_active_field == 0: self.coord_active_field = 7
                elif self.coord_active_field == 1: self.coord_active_field = 0
                elif self.coord_active_field == 2: self.coord_active_field = 1
                elif self.coord_active_field in (3, 4): self.coord_active_field = 2
                elif self.coord_active_field == 5:
                    if self.coord_keypad_row > 0: self.coord_keypad_row -= 1
                    else: self.coord_active_field = 1
                elif self.coord_active_field in (6, 7): self.coord_active_field = 3

            elif action == Action.DOWN:
                if self.coord_active_field == 0: self.coord_active_field = 1
                elif self.coord_active_field == 1: self.coord_active_field = 2
                elif self.coord_active_field == 2: self.coord_active_field = 3
                elif self.coord_active_field in (3, 4): self.coord_active_field = 6
                elif self.coord_active_field == 5:
                    if self.coord_keypad_row < 3: self.coord_keypad_row += 1
                    else: self.coord_active_field = 6
                elif self.coord_active_field in (6, 7): self.coord_active_field = 0

            elif action == Action.LEFT:
                if self.coord_active_field == 0:
                    self.coord_target_type = 1 - self.coord_target_type
                elif self.coord_active_field == 4:
                    self.coord_active_field = 3
                elif self.coord_active_field == 5:
                    if self.coord_keypad_col > 0: self.coord_keypad_col -= 1
                    else: self.coord_active_field = self.coord_last_text_field
                elif self.coord_active_field == 7:
                    self.coord_active_field = 6

            elif action == Action.RIGHT:
                if self.coord_active_field == 0:
                    self.coord_target_type = 1 - self.coord_target_type
                elif self.coord_active_field in (1, 2):
                    self.coord_active_field = 5
                elif self.coord_active_field == 3:
                    self.coord_active_field = 4
                elif self.coord_active_field == 4:
                    self.coord_active_field = 5
                elif self.coord_active_field == 5:
                    if self.coord_keypad_col < 3: self.coord_keypad_col += 1
                elif self.coord_active_field == 6:
                    self.coord_active_field = 7

            elif action == Action.SELECT:
                if self.coord_active_field == 0:
                    self.coord_target_type = 1 - self.coord_target_type
                elif self.coord_active_field == 1:
                    self.coord_active_field = 2
                elif self.coord_active_field == 2:
                    self.coord_active_field = 3
                elif self.coord_active_field == 3:
                    self.coord_active_field = 4
                elif self.coord_active_field == 4:
                    self.coord_active_field = 6
                elif self.coord_active_field == 6:
                    self.validate_and_apply_coords()
                elif self.coord_active_field == 7:
                    self.current_screen = Screen.HOME
                elif self.coord_active_field == 5:
                    btn = keypad_grid[self.coord_keypad_row][self.coord_keypad_col]
                    tf = self.coord_last_text_field
                    if btn == "OK":
                        self.validate_and_apply_coords()
                    elif btn == "CLR":
                        if tf == 1: self.coord_lat_str = ""
                        elif tf == 2: self.coord_lon_str = ""
                        elif tf == 3: self.coord_name_str = ""
                        elif tf == 4: self.coord_alt_str = ""
                    elif btn == "DEL":
                        if tf == 1 and self.coord_lat_str: self.coord_lat_str = self.coord_lat_str[:-1]
                        elif tf == 2 and self.coord_lon_str: self.coord_lon_str = self.coord_lon_str[:-1]
                        elif tf == 3 and self.coord_name_str: self.coord_name_str = self.coord_name_str[:-1]
                        elif tf == 4 and self.coord_alt_str: self.coord_alt_str = self.coord_alt_str[:-1]
                    else:
                        if tf == 1: self.coord_lat_str += btn
                        elif tf == 2: self.coord_lon_str += btn
                        elif tf == 3: self.coord_name_str += btn
                        elif tf == 4: self.coord_alt_str += btn

            elif action == Action.BACK:
                self.current_screen = Screen.HOME

            if self.coord_active_field in (1, 2, 3, 4):
                self.coord_last_text_field = self.coord_active_field

        elif self.current_screen == Screen.STATUS:
            if action == Action.LEFT and self.recorder:
                self.recorder.stop()
                self.set_notification("Recording disabled")
            elif action == Action.RIGHT and self.recorder and self.recorder.closed:
                prior = self.recorder
                if prior.worker.is_alive():
                    self.set_notification("Finishing previous journey")
                else:
                    self.last_ai_message = self.ai.messages[-1] if self.ai.messages else None
                    self.recorder = journey.Recorder(prior.path, prior.interval, prior.ai, resumed=True)
                    self.set_notification("New journey started")
            elif action == Action.SELECT:
                self.cycle_sensor_mode()
            elif action == Action.BACK:
                self.current_screen = Screen.HOME

        elif self.current_screen == Screen.ASSISTANT_INFO:
            if action == Action.BACK:
                self.current_screen = Screen.HOME
            elif action == Action.CHAR_INPUT and char and len(self.ai_input) < 512:
                self.ai_input += char
            elif action == Action.BACKSPACE:
                self.ai_input = self.ai_input[:-1]
            elif action == Action.UP:
                self.ai_scroll = min(self.ai_scroll + 1, 500)
            elif action == Action.DOWN:
                self.ai_scroll = max(self.ai_scroll - 1, 0)
            elif action in (Action.LEFT, Action.RIGHT):
                self.ai_focus = (self.ai_focus + (1 if action == Action.RIGHT else -1)) % 4
            elif action == Action.SELECT:
                if self.ai_focus == 0:
                    self.current_screen = Screen.AI_KEYBOARD
                elif self.ai_focus == 1:
                    previous_messages = self.ai.messages
                    if self.ai_input.strip() == '/reset':
                        self.ai.clear(); self.ai_input = ''; self.ai_scroll = 0
                    elif self.ai.submit(self.ai_input):
                        self.ai_record_session = None
                        if (self.recorder and self.recorder.ai and not self.recorder.closed
                                and not self.ai_input.strip().startswith('/forget ')):
                            self.last_ai_message = previous_messages[-1] if previous_messages else None
                            self.ai_record_session = self.recorder.id
                            self.recorder.emit('ai_question', 'user / unverified AI chat', text=self.ai_input[:2048])
                        self.ai_input = ''; self.ai_scroll = 0
                elif self.ai_focus == 2:
                    self.ai.cancel()
                else:
                    self.ai.clear(); self.ai_input = ''; self.ai_scroll = 0

        elif self.current_screen == Screen.AI_KEYBOARD:
            if action == Action.BACK:
                self.current_screen = Screen.ASSISTANT_INFO
            elif action in (Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT):
                steps = {Action.UP: -8, Action.DOWN: 8, Action.LEFT: -1, Action.RIGHT: 1}
                self.ai_key = (self.ai_key + steps[action]) % len(SEARCH_KEYS)
            elif action == Action.CHAR_INPUT and char and len(self.ai_input) < 512:
                self.ai_input += char
            elif action == Action.BACKSPACE:
                self.ai_input = self.ai_input[:-1]
            elif action == Action.SELECT:
                key = SEARCH_KEYS[self.ai_key]
                if key == 'FIND':
                    self.current_screen = Screen.ASSISTANT_INFO
                elif key == 'DEL':
                    self.ai_input = self.ai_input[:-1]
                elif key == 'CLR':
                    self.ai_input = ''
                elif len(self.ai_input) < 512:
                    self.ai_input += ' ' if key == 'SPACE' else key.lower()

        elif self.current_screen == Screen.PLACES_SEARCH:
            if action == Action.CHAR_INPUT and char:
                if self.places_search_field == 0 and len(self.places_query) < 64:
                    self.places_query += char
                return
            elif action == Action.BACKSPACE:
                if self.places_search_field == 0 and self.places_query:
                    self.places_query = self.places_query[:-1]
                return
            elif action == Action.UP:
                self.places_search_field = (self.places_search_field - 1) % 5
            elif action == Action.DOWN:
                self.places_search_field = (self.places_search_field + 1) % 5
            elif action == Action.LEFT:
                if self.places_search_field == 0:
                    self.current_screen = Screen.SEARCH_KEYBOARD
                elif self.places_search_field == 1:
                    self.places_selected_cat_idx = (self.places_selected_cat_idx - 1) % len(self.places_categories)
            elif action == Action.RIGHT:
                if self.places_search_field == 0:
                    self.current_screen = Screen.SEARCH_KEYBOARD
                elif self.places_search_field == 1:
                    self.places_selected_cat_idx = (self.places_selected_cat_idx + 1) % len(self.places_categories)
            elif action == Action.SELECT:
                if self.places_search_field == 0:
                    if self.places_query.strip():
                        self.execute_places_name_search()
                    else:
                        self.current_screen = Screen.SEARCH_KEYBOARD
                elif self.places_search_field == 1:
                    self.places_selected_cat_idx = (self.places_selected_cat_idx + 1) % len(self.places_categories)
                elif self.places_search_field == 2:
                    self.execute_places_name_search()
                elif self.places_search_field == 3:
                    self.execute_places_nearby_search()
                elif self.places_search_field == 4:
                    self.current_screen = Screen.HOME
            elif action == Action.BACK:
                self.current_screen = Screen.HOME

        elif self.current_screen == Screen.SEARCH_KEYBOARD:
            if action == Action.BACK:
                self.current_screen = Screen.PLACES_SEARCH
            elif action in (Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT):
                steps = {Action.UP: -8, Action.DOWN: 8, Action.LEFT: -1, Action.RIGHT: 1}
                self.keyboard_index = (self.keyboard_index + steps[action]) % len(SEARCH_KEYS)
            elif action == Action.BACKSPACE:
                self.places_query = self.places_query[:-1]
            elif action == Action.CHAR_INPUT and char and len(self.places_query) < 64:
                self.places_query += char
            elif action == Action.SELECT:
                key = SEARCH_KEYS[self.keyboard_index]
                if key == "FIND":
                    self.execute_places_name_search()
                elif key == "DEL":
                    self.places_query = self.places_query[:-1]
                elif key == "CLR":
                    self.places_query = ""
                elif len(self.places_query) < 64:
                    self.places_query += " " if key == "SPACE" else key.lower()

        elif self.current_screen == Screen.PLACES_RESULTS:
            self.refresh_places_results(self.navigation_state())
            if action == Action.UP:
                if self.places_results:
                    self.places_results_idx = (self.places_results_idx - 1) % len(self.places_results)
            elif action == Action.DOWN:
                if self.places_results:
                    self.places_results_idx = (self.places_results_idx + 1) % len(self.places_results)
            elif action == Action.SELECT:
                if not self.search_busy and self.places_results and 0 <= self.places_results_idx < len(self.places_results):
                    self.selected_place = self.places_results[self.places_results_idx]
                    self.place_details_btn_idx = 0
                    self.detail_page = 0
                    self.current_screen = Screen.PLACE_DETAILS
            elif action == Action.BACK:
                self.current_screen = Screen.PLACES_SEARCH

        elif self.current_screen == Screen.PLACE_DETAILS:
            if action == Action.LEFT:
                self.detail_page = max(0, self.detail_page - 1)
            elif action == Action.RIGHT:
                self.detail_page += 1
            elif action == Action.UP:
                self.place_details_btn_idx = (self.place_details_btn_idx - 1) % 3
            elif action == Action.DOWN:
                self.place_details_btn_idx = (self.place_details_btn_idx + 1) % 3
            elif action == Action.SELECT:
                if self.place_details_btn_idx in (0, 1) and self.selected_place:
                    place = self.selected_place
                    activate = self.place_details_btn_idx == 0
                    wp = self.waypoint_mgr.add_waypoint(
                        place.name, place.lat, place.lon, description=place.category,
                        source=place.source, place_id=place.id, activate=activate,
                        provenance={"osm_type": place.osm_type, "osm_id": place.osm_id,
                                    "extracted_at": place.extracted_at, "region_id": place.region_id,
                                    "original_name": place.original_name, "metadata": place.metadata},
                    )
                    self.set_notification(f"{'Target' if activate else 'Saved'}: {wp.display_name}")
                    if activate:
                        self.current_screen = Screen.NAVIGATION
                elif self.place_details_btn_idx == 2:
                    self.current_screen = Screen.PLACES_RESULTS
            elif action == Action.BACK:
                self.current_screen = Screen.PLACES_RESULTS

    def render(self, display: DisplayAdapter) -> None:
        """Draws the current screen state into the 320x240 display adapter."""
        display.clear(Colors.BG)
        state = self.navigation_state()
        gps = state.gps
        now = time.time()

        # Universal Header Bar (Y: 0..22)
        self._render_header(display, gps, now, state.position_status)

        # Screen-Specific Viewport (Y: 23..218)
        if self.current_screen == Screen.HOME:
            self._render_home(display, state)
        elif self.current_screen == Screen.NAVIGATION:
            self._render_navigation(display, state)
        elif self.current_screen == Screen.WAYPOINTS:
            self._render_waypoints(display, state)
        elif self.current_screen == Screen.WAYPOINT_ACTIONS:
            self._render_waypoint_actions(display)
        elif self.current_screen == Screen.COORD_INPUT:
            self._render_coord_input(display)
        elif self.current_screen == Screen.STATUS:
            self._render_status(display, state)
        elif self.current_screen == Screen.ASSISTANT_INFO:
            self._render_assistant_info(display)
        elif self.current_screen == Screen.AI_KEYBOARD:
            self._render_ai_keyboard(display)
        elif self.current_screen == Screen.PLACES_SEARCH:
            self._render_places_search(display)
        elif self.current_screen == Screen.SEARCH_KEYBOARD:
            self._render_search_keyboard(display)
        elif self.current_screen == Screen.PLACES_RESULTS:
            self._render_places_results(display, state)
        elif self.current_screen == Screen.PLACE_DETAILS:
            self._render_place_details(display, state)

        # Universal Bottom Notification / Footer Bar (Y: 220..240)
        self._render_footer(display)
        display.present()

    def _render_header(self, display: DisplayAdapter, gps: nav.GPSReading, now: float, status=None) -> None:
        # Background bar
        display.draw_rect(0, 0, 320, 22, Colors.HEADER_BG)
        display.draw_line(0, 22, 320, 22, Colors.DIVIDER, width=1)

        # App branding
        display.draw_text(6, 4, "SURVIVE-X", size="med", color=Colors.CYAN)

        # Explicit Sensor Mode and Lock Badges (Stage 2.5)
        # CRITICAL RULE: NEVER display 'LOCKED 3D' in simulated or manual mode!
        gps_status = status or gps.validate(current_time=now)[1]
        if self.sensor_mode == nav.SensorMode.SIMULATED:
            if gps_status == nav.NavStatus.OK:
                badge_text = "SIM FIX"
                badge_bg = (18, 48, 56)
                badge_border = Colors.CYAN
                badge_col = Colors.CYAN
            elif gps_status == nav.NavStatus.STALE_GPS:
                badge_text = "SIM STALE"
                badge_bg = Colors.AMBER_DIM
                badge_border = Colors.AMBER
                badge_col = Colors.AMBER
            elif gps_status == nav.NavStatus.NO_FIX:
                badge_text = "SIM NOFIX"
                badge_bg = Colors.RED_DIM
                badge_border = Colors.RED
                badge_col = Colors.RED
            elif gps_status == nav.NavStatus.INVALID_COORDS:
                badge_text = "INV COORD"
                badge_bg = Colors.RED_DIM
                badge_border = Colors.RED
                badge_col = Colors.RED
            else:
                badge_text = "SIM POOR"
                badge_bg = Colors.AMBER_DIM
                badge_border = Colors.AMBER
                badge_col = Colors.AMBER

        elif self.sensor_mode == nav.SensorMode.MANUAL:
            if gps_status in (nav.NavStatus.OK, nav.NavStatus.NO_FIX):
                badge_text = "MANUAL" if gps.has_fix else "MANUAL UNSET"
                badge_bg = (18, 48, 56)
                badge_border = Colors.CYAN
                badge_col = Colors.CYAN
            else:
                badge_text = "INV COORD"
                badge_bg = Colors.RED_DIM
                badge_border = Colors.RED
                badge_col = Colors.RED

        else: # LIVE_GNSS
            badge_text = "LIVE FIX" if gps_status == nav.NavStatus.OK else ("NO GNSS HW" if self.live_sensors is None else gps_status.value)
            badge_bg = Colors.RED_DIM if gps_status != nav.NavStatus.OK else Colors.GREEN_DIM
            badge_border = Colors.RED if gps_status != nav.NavStatus.OK else Colors.GREEN
            badge_col = badge_border

        display.draw_rect(112, 3, 96, 16, badge_bg, border_color=badge_border, border_width=1, border_radius=3)
        display.draw_text(128, 5, badge_text, size="small", color=badge_col)

        # Clock
        time_str = time.strftime("%H:%M:%S", time.localtime(now))
        display.draw_text(256, 5, time_str, size="small", color=Colors.TEXT_MUTED)

    def _render_footer(self, display: DisplayAdapter) -> None:
        display.draw_line(0, 219, 320, 219, Colors.DIVIDER, width=1)
        display.draw_rect(0, 220, 320, 20, Colors.FOOTER_BG)
        prefix = 'STATUS'
        message = self.notification_msg
        if self.recorder:
            prefix = 'REC' if self.recorder.ready else 'REC INIT'
            if self.recorder.closed:
                prefix = 'REC OFF'
            if self.recorder.degraded:
                prefix, message = 'REC WARNING', self.recorder.degraded
            if self.recorder.error:
                prefix, message = 'REC ERROR', self.recorder.error
        display.draw_text(6, 224, f"{prefix}: {message[:50-len(prefix)]}", size="small",
                          color=Colors.RED if self.recorder and self.recorder.error else Colors.TEXT_MUTED)

    def _render_home(self, display: DisplayAdapter, state: nav.NavigationState) -> None:
        gps = state.gps
        display.draw_text(10, 26, "SYSTEM MODULES", size="med", color=Colors.CYAN)
        display.draw_text(10, 39, "Select module to operate", size="small", color=Colors.TEXT_DARK)

        # Menu options (Left Column: 6 items)
        for i, (label, _) in enumerate(self.home_menu_items):
            y = 48 + i * 19
            is_active = (i == self.home_menu_idx)
            bg_col = Colors.SEL_BG if is_active else Colors.PANEL
            border_col = Colors.SEL_BORDER if is_active else Colors.PANEL_BORDER
            text_col = Colors.TEXT_WHITE if is_active else Colors.TEXT_MUTED
            prefix = "▶ " if is_active else "   "

            display.draw_rect(10, y, 175, 18, bg_col, border_color=border_col, border_width=1, border_radius=3)
            display.draw_text(14, y + 3, f"{prefix}{label}", size="small", color=text_col)

        # Active Destination Quick Card (Right Column)
        display.draw_rect(192, 26, 120, 134, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        active_wp = self.waypoint_mgr.get_active_waypoint()
        title = "ACTIVE TARGET"
        target_name = "None"
        if active_wp:
            target_name = active_wp.name
            if active_wp.is_test_data:
                title = "TEST DATA TARGET"
            elif not active_wp.source:
                title = "UNVERIFIED TARGET"
        display.draw_text(198, 30, title, size="small", color=Colors.TEXT_MUTED)
        display.draw_text(198, 44, display.fit_text(target_name, 108), size="med", color=Colors.CYAN)

        display.draw_text(198, 62, "DISTANCE", size="small", color=Colors.TEXT_MUTED)
        sol = state.to_destination(active_wp)
        display.draw_text(198, 74, format_distance(sol.distance_m), size="large", color=Colors.TEXT_WHITE)
        display.draw_text(198, 96, "TRUE BEARING", size="small", color=Colors.TEXT_MUTED)
        display.draw_text(198, 108, format_bearing(sol.bearing_deg), size="med", color=Colors.TEXT_WHITE)
        suffix = " (SIM)" if self.sensor_mode == nav.SensorMode.SIMULATED else ""
        turn = f"Turn {sol.turn_angle_deg:+04.0f}°{suffix}" if sol.turn_angle_deg is not None else sol.arrow_label.replace("_", " ")
        display.draw_text(198, 126, display.fit_text(turn, 112, "small"), size="small", color=Colors.CYAN)

        # Position Card (Bottom Viewport)
        display.draw_rect(10, 165, 302, 50, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        if self.sensor_mode == nav.SensorMode.SIMULATED:
            pos_title = "CURRENT POSITION (SIMULATED):"
            alt_str = f"{gps.altitude_m:.0f}m (SIM)" if state.has_position and gps.altitude_m is not None else "N/A"
        elif self.sensor_mode == nav.SensorMode.MANUAL:
            pos_title = "CURRENT POSITION (MANUAL COORD):"
            alt_str = f"{gps.altitude_m:.0f}m (MANUAL)" if state.has_position and gps.altitude_m is not None else "N/A (MANUAL)"
        else:
            pos_title = "LIVE GNSS POSITION:"
            alt_str = f"{gps.altitude_m:.0f}m" if state.has_position and gps.altitude_m is not None else "N/A"

        display.draw_text(16, 169, pos_title, size="small", color=Colors.TEXT_MUTED)
        if not state.has_position:
            display.draw_text(16, 183, "Position unavailable", size="small", color=Colors.AMBER)
        else:
            display.draw_text(16, 183, f"Lat: {gps.lat:+.5f}° | Lon: {gps.lon:+.5f}° | Alt: {alt_str}", size="small", color=Colors.TEXT_WHITE)
        display.draw_text(16, 198, "[▲/▼: Select] [Enter: Open] [M: Mode] [Q: Quit]", size="small", color=Colors.TEXT_MUTED)

    def _render_navigation(self, display: DisplayAdapter, state: nav.NavigationState) -> None:
        dest = self.waypoint_mgr.get_active_waypoint()
        sol = state.to_destination(dest)
        display.draw_rect(8, 25, 304, 26, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_width=1, border_radius=3)
        title = dest.display_name if dest else "None selected"
        display.draw_text(14, 28, f"DEST: {title[:32]}", size="med", color=Colors.CYAN)
        if dest and nav.validate_wgs84(dest.lat, dest.lon)[0]:
            display.draw_text(14, 40, f"Target: {dest.lat:+.4f}°, {dest.lon:+.4f}°", size="small", color=Colors.TEXT_MUTED)

        # Left Vector Arrow Card
        display.draw_rect(8, 54, 130, 116, (20, 26, 36), border_color=Colors.PANEL_BORDER, border_radius=4)
        display.draw_nav_arrow(73, 102, 34, sol.turn_angle_deg, sol.status.name, sol.arrow_label)

        # Arrow Status Label below rotating chevron
        if sol.status == nav.NavStatus.OK:
            turn_cmd = f"TURN {sol.turn_angle_deg:+04.0f}°" if abs(sol.turn_angle_deg) >= 15.0 else "DEAD AHEAD"
            display.draw_text(20, 146, turn_cmd, size="med", color=Colors.GREEN if abs(sol.turn_angle_deg) < 15.0 else Colors.CYAN)
        elif sol.status == nav.NavStatus.ARRIVED:
            display.draw_text(20, 146, "AT DESTINATION", size="med", color=Colors.GREEN)
        elif sol.status == nav.NavStatus.NO_FIX:
            display.draw_text(20, 146, "NO GPS LOCK", size="med", color=Colors.RED)
        elif sol.status == nav.NavStatus.STALE_GPS:
            display.draw_text(20, 146, "GPS DATA STALE", size="med", color=Colors.AMBER)
        elif sol.status == nav.NavStatus.MISSING_HEADING:
            display.draw_text(14, 146, "HEADING UNAVAILABLE", size="small", color=Colors.AMBER)

        else:
            display.draw_text(14, 146, sol.arrow_label.replace("_", " "), size="small", color=Colors.AMBER)

        # Right Guidance Metrics Card
        display.draw_rect(144, 54, 168, 116, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        display.draw_text(152, 59, "DISTANCE TO TARGET", size="small", color=Colors.TEXT_MUTED)
        
        display.draw_text(152, 71, format_distance(sol.distance_m), size="large", color=Colors.TEXT_WHITE)
        display.draw_text(152, 94, f"Bearing: {format_bearing(sol.bearing_deg)}", size="small", color=Colors.TEXT_WHITE)
        if sol.true_heading_deg is not None:
            if self.sensor_mode != nav.SensorMode.SIMULATED:
                compass = state.compass
                display.draw_text(152, 108, f"Heading: {sol.true_heading_deg:05.1f}° True", size="small", color=Colors.TEXT_WHITE)
                display.draw_text(152, 121, f"Mag: {compass.magnetic_heading_deg:03.0f}° | Dec: {self.live_declination:+.1f}°", size="small", color=Colors.TEXT_MUTED)
            else:
                display.draw_text(152, 108, f"Heading: {sol.true_heading_deg:05.1f}° True (SIM)", size="small", color=Colors.TEXT_WHITE)
                display.draw_text(152, 121, f"(Mag: {self.sim_mag_heading:03.0f}° SIM | Dec: {self.declination_deg:+.1f}°)", size="small", color=Colors.TEXT_MUTED)
            display.draw_text(152, 138, f"Steer: Turn {sol.turn_angle_deg:+05.1f}°", size="med", color=Colors.GREEN)
        else:
            display.draw_text(152, 108, "HEADING UNAVAILABLE" if state.true_heading is None else "Steering unavailable", size="small", color=Colors.AMBER)

        # Mandatory Safety Warning Banner
        display.draw_rect(8, 174, 304, 24, (45, 34, 14), border_color=Colors.AMBER, border_radius=3)
        display.draw_text(14, 180, "CAUTION: Direct geodesic. Ignores terrain hazards.", size="small", color=Colors.AMBER)

        # Controls Hint
        display.draw_text(10, 202, "[WASD: GPS] [H/L: Turn] [G: Fix] [T: Stale] [Esc: Back]", size="small", color=Colors.TEXT_MUTED)

    def _render_waypoints(self, display: DisplayAdapter, state: nav.NavigationState) -> None:
        display.draw_rect(8, 25, 304, 20, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=3)
        display.draw_text(12, 28, "SAVED WAYPOINTS & BACKTRACK", size="med", color=Colors.CYAN)

        wps = self.waypoint_mgr.list_waypoints()
        items = [
            "+ SAVE CURRENT POSITION",
            "⮌ RETURN TO STARTING POINT",
        ]
        for wp in wps:
            dist = state.to_destination(wp).distance_m
            is_target = " [TARGET]" if (self.waypoint_mgr.active_waypoint_name == wp.name) else ""
            items.append(f"{wp.display_name[:23]} ({format_distance(dist)}){is_target}")

        visible_count = 5
        start_idx = max(0, min(self.waypoints_idx - visible_count // 2, len(items) - visible_count))
        for row, idx in enumerate(range(start_idx, min(len(items), start_idx + visible_count))):
            y = 48 + row * 26
            is_active = (idx == self.waypoints_idx)
            bg_col = Colors.SEL_BG if is_active else Colors.PANEL
            border_col = Colors.SEL_BORDER if is_active else Colors.PANEL_BORDER
            text_col = Colors.TEXT_WHITE if is_active else Colors.TEXT_MUTED
            prefix = "▶ " if is_active else "   "

            display.draw_rect(10, y, 300, 23, bg_col, border_color=border_col, border_width=1, border_radius=3)
            display.draw_text(16, y + 5, display.fit_text(f"{prefix}{items[idx]}", 288), size="med", color=text_col)

        active = self.waypoint_mgr.get_active_waypoint()
        title = active.display_name if active else "None"
        display.draw_text(12, 186, f"Total: {len(wps)} | Active: {title[:30]}", size="small", color=Colors.TEXT_WHITE)
        display.draw_text(12, 202, "[▲/▼: Scroll]  [Enter: Action Menu]  [Esc: Back]", size="small", color=Colors.TEXT_MUTED)

    def _render_waypoint_actions(self, display: DisplayAdapter) -> None:
        # Modal overlay box centered on screen
        display.draw_rect(35, 36, 250, 166, (20, 26, 36), border_color=Colors.CYAN, border_width=2, border_radius=6)
        display.draw_text(45, 44, "WAYPOINT ACTIONS", size="med", color=Colors.CYAN)

        name = self.selected_waypoint.display_name if self.selected_waypoint else "Selected"
        display.draw_text(45, 59, f"Name: {name[:20]}", size="med", color=Colors.TEXT_WHITE)
        if self.selected_waypoint:
            display.draw_text(45, 73, f"Pos: {self.selected_waypoint.lat:+.4f}°, {self.selected_waypoint.lon:+.4f}°", size="small", color=Colors.TEXT_MUTED)

        actions = [
            "1. Set as Active Destination",
            "2. Rename Waypoint",
            "3. Delete Waypoint",
            "4. Cancel",
        ]
        for i, act in enumerate(actions):
            y = 88 + i * 23
            is_active = (i == self.action_menu_idx)
            bg_col = Colors.SEL_BG if is_active else (26, 34, 46)
            border_col = Colors.SEL_BORDER if is_active else Colors.PANEL_BORDER
            text_col = Colors.TEXT_WHITE if is_active else Colors.TEXT_MUTED
            prefix = "▶ " if is_active else "   "

            display.draw_rect(45, y, 230, 20, bg_col, border_color=border_col, border_width=1, border_radius=3)
            display.draw_text(50, y + 4, f"{prefix}{act}", size="small", color=text_col)

        display.draw_text(45, 184, "[▲/▼: Select]  [Enter: Execute]  [Esc: Back]", size="small", color=Colors.TEXT_MUTED)

    def _render_coord_input(self, display: DisplayAdapter) -> None:
        display.draw_text(10, 26, "MANUAL COORDINATE ENTRY", size="med", color=Colors.CYAN)

        # 0. Target Toggle (Destination vs Current Position)
        is_target_active = (self.coord_active_field == 0)
        target_bg = Colors.SEL_BG if is_target_active else Colors.PANEL
        target_border = Colors.SEL_BORDER if is_target_active else Colors.PANEL_BORDER
        display.draw_rect(8, 44, 174, 20, target_bg, border_color=target_border, border_width=1, border_radius=3)
        target_txt = "▶ SET DESTINATION" if self.coord_target_type == 0 else "▶ SET CURRENT POS"
        display.draw_text(14, 48, target_txt, size="small", color=Colors.TEXT_WHITE if is_target_active else Colors.CYAN)

        # 1. Latitude Input Field
        is_lat_active = (self.coord_active_field == 1)
        lat_border = Colors.SEL_BORDER if is_lat_active else Colors.PANEL_BORDER
        lat_bg = Colors.SEL_BG if is_lat_active else Colors.PANEL
        display.draw_text(10, 68, "Latitude [-90°..+90°]:", size="small", color=Colors.TEXT_MUTED)
        display.draw_rect(8, 79, 174, 18, lat_bg, border_color=lat_border, border_width=1, border_radius=3)
        cursor_lat = "_" if is_lat_active else ""
        display.draw_text(12, 82, f"{self.coord_lat_str}{cursor_lat}"[:22], size="med", color=Colors.TEXT_WHITE)

        # 2. Longitude Input Field
        is_lon_active = (self.coord_active_field == 2)
        lon_border = Colors.SEL_BORDER if is_lon_active else Colors.PANEL_BORDER
        lon_bg = Colors.SEL_BG if is_lon_active else Colors.PANEL
        display.draw_text(10, 101, "Longitude [-180°..+180°]:", size="small", color=Colors.TEXT_MUTED)
        display.draw_rect(8, 112, 174, 18, lon_bg, border_color=lon_border, border_width=1, border_radius=3)
        cursor_lon = "_" if is_lon_active else ""
        display.draw_text(12, 115, f"{self.coord_lon_str}{cursor_lon}"[:22], size="med", color=Colors.TEXT_WHITE)

        # 3. Target Name & Optional Altitude Fields
        is_name_active = (self.coord_active_field == 3)
        name_border = Colors.SEL_BORDER if is_name_active else Colors.PANEL_BORDER
        name_bg = Colors.SEL_BG if is_name_active else Colors.PANEL
        display.draw_text(10, 134, "Name (Opt):", size="small", color=Colors.TEXT_MUTED)
        display.draw_rect(8, 145, 104, 18, name_bg, border_color=name_border, border_width=1, border_radius=3)
        cursor_name = "_" if is_name_active else ""
        display.draw_text(12, 148, f"{self.coord_name_str}{cursor_name}"[:13], size="med", color=Colors.TEXT_WHITE)

        is_alt_active = (self.coord_active_field == 4)
        alt_border = Colors.SEL_BORDER if is_alt_active else Colors.PANEL_BORDER
        alt_bg = Colors.SEL_BG if is_alt_active else Colors.PANEL
        display.draw_text(118, 134, "Alt(m):", size="small", color=Colors.TEXT_MUTED)
        display.draw_rect(116, 145, 66, 18, alt_bg, border_color=alt_border, border_width=1, border_radius=3)
        cursor_alt = "_" if is_alt_active else ""
        display.draw_text(119, 148, f"{self.coord_alt_str}{cursor_alt}"[:8], size="med", color=Colors.TEXT_WHITE)

        # 4. Action Buttons (Apply & Navigate / Cancel)
        is_apply_active = (self.coord_active_field == 6)
        display.draw_rect(8, 169, 104, 22, Colors.SEL_BG if is_apply_active else (25, 60, 40),
                          border_color=Colors.GREEN if is_apply_active else Colors.PANEL_BORDER, border_width=1, border_radius=3)
        display.draw_text(14, 174, "[ APPLY & NAV ]", size="small", color=Colors.GREEN if is_apply_active else Colors.TEXT_WHITE)

        is_cancel_active = (self.coord_active_field == 7)
        display.draw_rect(116, 169, 66, 22, Colors.SEL_BG if is_cancel_active else Colors.PANEL,
                          border_color=Colors.SEL_BORDER if is_cancel_active else Colors.PANEL_BORDER, border_width=1, border_radius=3)
        display.draw_text(124, 174, "[ CANCEL ]", size="small", color=Colors.RED if is_cancel_active else Colors.TEXT_MUTED)

        # 5. On-Screen Keypad Card (Right Column)
        display.draw_rect(190, 44, 122, 147, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        display.draw_text(196, 48, "ON-SCREEN KEYPAD", size="small", color=Colors.TEXT_MUTED)

        keypad_grid = [
            ["1", "2", "3", "+"],
            ["4", "5", "6", "-"],
            ["7", "8", "9", "."],
            ["DEL", "0", "CLR", "OK"],
        ]

        is_keypad_active = (self.coord_active_field == 5)
        for r in range(4):
            for c in range(4):
                kx = 194 + c * 28
                ky = 63 + r * 22
                k_label = keypad_grid[r][c]
                is_k_sel = is_keypad_active and (self.coord_keypad_row == r and self.coord_keypad_col == c)

                k_bg = Colors.SEL_BG if is_k_sel else Colors.PANEL_ALT
                k_border = Colors.CYAN if is_k_sel else Colors.PANEL_BORDER
                k_col = Colors.TEXT_WHITE if is_k_sel else (Colors.GREEN if k_label == "OK" else (Colors.AMBER if k_label in ("DEL", "CLR") else Colors.TEXT_MUTED))

                display.draw_rect(kx, ky, 25, 19, k_bg, border_color=k_border, border_width=1, border_radius=2)
                offset_x = 4 if len(k_label) == 3 else (7 if len(k_label) == 2 else 9)
                display.draw_text(kx + offset_x, ky + 4, k_label, size="small", color=k_col)

        display.draw_text(194, 154, "[Joy: ▲▼◀▶|Enter]", size="small", color=Colors.TEXT_DARK)
        display.draw_text(194, 166, "[Mac: Direct Type]", size="small", color=Colors.TEXT_DARK)

        # Error Notice Banner or Footer Help
        if self.coord_error:
            display.draw_rect(8, 196, 304, 20, Colors.RED_DIM, border_color=Colors.RED, border_radius=2)
            display.draw_text(12, 200, f"ERR: {self.coord_error[:48]}", size="small", color=Colors.RED)
        else:
            display.draw_text(10, 201, "[▲/▼: Field] [Type keys / Pad] [Enter: Apply] [Esc: Back]", size="small", color=Colors.TEXT_MUTED)

    def _render_status(self, display: DisplayAdapter, state: nav.NavigationState) -> None:
        gps = state.gps
        display.draw_text(10, 26, "HARDWARE & SENSOR DIAGNOSTICS", size="med", color=Colors.CYAN)

        gps_status = state.position_status

        # Sensor Mode Banner
        display.draw_rect(8, 42, 304, 26, Colors.PANEL, border_color=Colors.CYAN, border_radius=4)
        display.draw_text(14, 45, f"SENSOR MODE: {self.sensor_mode.value}", size="med", color=Colors.CYAN)
        display.draw_text(14, 57, "[Press M or Enter to cycle: SIM / MANUAL / LIVE]", size="small", color=Colors.TEXT_MUTED)

        # GPS Diagnostic Box
        display.draw_rect(8, 72, 304, 58, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        if self.sensor_mode == nav.SensorMode.SIMULATED:
            if gps_status == nav.NavStatus.OK:
                rcv_str, rcv_col = "SIMULATED FIX (TEST MODE - NO HW)", Colors.CYAN
            elif gps_status == nav.NavStatus.STALE_GPS:
                rcv_str, rcv_col = "SIMULATED STALE FIX (>10s old)", Colors.AMBER
            elif gps_status == nav.NavStatus.NO_FIX:
                rcv_str, rcv_col = "SIMULATED NO FIX (SEARCHING)", Colors.RED
            else:
                rcv_str, rcv_col = gps_status.value, Colors.RED
            alt_info = f"{gps.altitude_m:.1f} m (SIM)" if state.has_position and gps.altitude_m is not None else "N/A"
            sat_info = f"{gps.satellites or 0} locked (SIM)"
            acc_info = f"±{gps.accuracy_m:.1f} m (SIM)" if state.has_position and gps.accuracy_m is not None else "N/A"
        elif self.sensor_mode == nav.SensorMode.MANUAL:
            rcv_str = "MANUAL COORDINATES (USER ENTERED)" if state.has_position else "MANUAL POSITION UNAVAILABLE"
            rcv_col = Colors.CYAN if state.has_position else Colors.AMBER
            alt_info = f"{gps.altitude_m:.1f} m (MANUAL)" if state.has_position and gps.altitude_m is not None else "N/A (MANUAL)"
            sat_info = "N/A (MANUAL)"
            acc_info = "UNKNOWN"
        else: # LIVE_GNSS
            rcv_str, rcv_col = ("LIVE FIX", Colors.GREEN) if state.has_position else (gps_status.value, Colors.RED)
            alt_info = f"{gps.altitude_m:.1f} m" if gps.altitude_m is not None else "N/A"
            sat_info = str(gps.satellites) if gps.satellites is not None else "N/A"
            acc_info = f"{gps.accuracy_m:.1f} m" if gps.accuracy_m is not None else "UNKNOWN"

        display.draw_text(14, 75, f"RECEIVER: {rcv_str}", size="small", color=rcv_col)
        if state.has_position:
            display.draw_text(14, 88, f"Coordinates: {gps.lat:+.6f}°, {gps.lon:+.6f}°", size="small", color=Colors.TEXT_WHITE)
            display.draw_text(14, 101, f"Altitude:    {alt_info} | Satellites: {sat_info}", size="small", color=Colors.TEXT_MUTED)
            display.draw_text(14, 114, f"Precision:   {acc_info} (HDOP: {gps.hdop if gps.hdop is not None else 'N/A'})", size="small", color=Colors.TEXT_MUTED)
        elif self.sensor_mode != nav.SensorMode.LIVE_GNSS:
            display.draw_text(14, 88, f"Position unavailable: {gps_status.value}", size="small", color=Colors.AMBER)
        else:
            display.draw_text(14, 88, f"Live position unavailable: {gps_status.value}", size="small", color=Colors.TEXT_MUTED)
            display.draw_text(14, 101, "Check GNSS device and signal.", size="small", color=Colors.TEXT_MUTED)
            display.draw_text(14, 114, "Press M to return to SIMULATED or MANUAL mode.", size="small", color=Colors.AMBER)

        # Compass Diagnostic Box
        display.draw_rect(8, 134, 304, 40, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        if state.true_heading is not None and self.sensor_mode != nav.SensorMode.SIMULATED:
            display.draw_text(14, 137, "MAGNETOMETER: LIVE", size="small", color=Colors.CYAN)
            display.draw_text(14, 149, f"Heading: {state.true_heading:.1f}° True", size="small", color=Colors.TEXT_WHITE)
            display.draw_text(14, 161, f"Declination: {self.live_declination:+.1f}°", size="small", color=Colors.TEXT_MUTED)
        elif state.true_heading is not None:
            display.draw_text(14, 137, "MAGNETOMETER: SIMULATED (H/L ROTATE)", size="small", color=Colors.CYAN)
            t_head = state.true_heading
            display.draw_text(14, 149, f"Heading:     {t_head:05.1f}° True ({self.sim_mag_heading:05.1f}° Mag SIM)", size="small", color=Colors.TEXT_WHITE)
            display.draw_text(14, 161, f"Declination: +{self.declination_deg:.1f}° East (simulation only)", size="small", color=Colors.TEXT_MUTED)
        else:
            display.draw_text(14, 137, "MAGNETOMETER: UNAVAILABLE / OFFLINE", size="small", color=Colors.RED)
            display.draw_text(14, 151, "Digital compass hardware disconnected.", size="small", color=Colors.TEXT_MUTED)

        # Health Alert Banner
        display.draw_rect(8, 178, 304, 20, (24, 40, 30) if gps_status == nav.NavStatus.OK else (45, 34, 14), border_color=Colors.GREEN if gps_status == nav.NavStatus.OK else Colors.AMBER, border_radius=3)
        if self.sensor_mode == nav.SensorMode.LIVE_GNSS and not state.has_position:
            display.draw_text(14, 182, f"ALERTS: LIVE GPS {gps_status.value}", size="small", color=Colors.AMBER)
        elif state.has_position and state.true_heading is not None:
            label = "Position valid | LIVE heading available" if self.sensor_mode != nav.SensorMode.SIMULATED else "Position valid | SIM heading available"
            display.draw_text(14, 182, label, size="small", color=Colors.GREEN)
        else:
            display.draw_text(14, 182, f"ALERTS: GPS={gps_status.value} | COMPASS={'ONLINE' if state.true_heading is not None else 'OFFLINE'}", size="small", color=Colors.AMBER)

        controls = "[M: Mode]  [G: Fix]  [T: Stale]  [C: Compass]  [Esc: Back]"
        if self.recorder:
            controls = "Recording: LEFT stop / RIGHT start | AI " + ("on" if self.recorder.ai else "off")
        display.draw_text(10, 202, controls, size="small", color=Colors.TEXT_MUTED)

    def _render_assistant_info(self, display: DisplayAdapter) -> None:
        display.draw_text(8, 26, 'OFFLINE AI  ' + self.ai.status, size='med', color=Colors.CYAN)
        display.draw_text(8, 45, 'AI responses are unverified', size='small', color=Colors.AMBER)
        lines = []
        for role, message in self.ai.messages:
            lines.extend(display.wrap_text(role + ': ' + message, 300, 'small'))
        if self.ai.status == 'ERROR':
            lines.extend(display.wrap_text('Error: ' + self.ai.error, 300, 'small'))
        visible = 7
        self.ai_scroll = min(self.ai_scroll, max(0, len(lines) - visible))
        end = len(lines) - self.ai_scroll
        for index, line in enumerate(lines[max(0, end-visible):end]):
            display.draw_text(8, 62 + index * 13, line, size='small')
        display.draw_text(8, 157, display.fit_text('> ' + self.ai_input[-42:], 300, 'small'), size='small')
        for index, name in enumerate(('KEYS', 'SEND', 'CANCEL', 'CLEAR')):
            x = 8 + index * 77
            display.draw_rect(x, 177, 72, 22, Colors.SEL_BG if self.ai_focus == index else Colors.PANEL,
                              border_color=Colors.PANEL_BORDER)
            display.draw_text(x + 3, 182, name, size='small')
        display.draw_text(8, 205, 'Up/down scroll  Left/right choose  Esc back', size='small')

    def _render_ai_keyboard(self, display):
        display.draw_text(10, 28, 'AI INPUT KEYBOARD', color=Colors.CYAN)
        display.draw_text(10, 49, display.fit_text(self.ai_input + '_', 300), color=Colors.TEXT_WHITE)
        for index, key in enumerate(SEARCH_KEYS):
            x, y = 8 + index % 8 * 38, 74 + index // 8 * 25
            display.draw_rect(x, y, 36, 23, Colors.SEL_BG if index == self.ai_key else Colors.PANEL,
                              border_color=Colors.CYAN if index == self.ai_key else Colors.PANEL_BORDER)
            display.draw_text(x + 2, y + 6, display.fit_text('DONE' if key == 'FIND' else key, 32, 'small'), size='small')
        display.draw_text(10, 204, 'Directions: move  Select: key  Back: chat', size='small')

    def _render_search_keyboard(self, display):
        display.draw_text(10, 28, "PLACE NAME KEYBOARD", color=Colors.CYAN)
        display.draw_text(10, 49, display.fit_text(self.places_query + "_", 300), color=Colors.TEXT_WHITE)
        for index, key in enumerate(SEARCH_KEYS):
            x, y = 8 + index % 8 * 38, 74 + index // 8 * 25
            selected = index == self.keyboard_index
            display.draw_rect(x, y, 36, 23, Colors.SEL_BG if selected else Colors.PANEL,
                              border_color=Colors.CYAN if selected else Colors.PANEL_BORDER)
            display.draw_text(x + 2, y + 6, display.fit_text(key, 32, "small"), size="small")
        display.draw_text(10, 204, "Directions: move | Select: key | Back: fields", size="small")

    def _render_places_search(self, display: DisplayAdapter) -> None:
        display.draw_text(10, 26, "PLACES & ASSISTANCE SEARCH", size="med", color=Colors.CYAN)

        # Field 0: Query Box
        display.draw_text(10, 44, "Place Name (Left/Right: keyboard):", size="small", color=Colors.TEXT_MUTED)
        is_q_active = (self.places_search_field == 0)
        q_bg = Colors.SEL_BG if is_q_active else Colors.PANEL
        q_border = Colors.SEL_BORDER if is_q_active else Colors.PANEL_BORDER
        display.draw_rect(8, 56, 304, 22, q_bg, border_color=q_border, border_width=1, border_radius=3)
        cursor = "_" if is_q_active else ""
        display.draw_text(14, 60, display.fit_text(f"Q: {self.places_query}{cursor}", 290), size="med", color=Colors.TEXT_WHITE)

        # Field 1: Category Filter
        display.draw_text(10, 82, "Category Filter (◀/▶ to cycle):", size="small", color=Colors.TEXT_MUTED)
        is_c_active = (self.places_search_field == 1)
        c_bg = Colors.SEL_BG if is_c_active else Colors.PANEL
        c_border = Colors.SEL_BORDER if is_c_active else Colors.PANEL_BORDER
        display.draw_rect(8, 94, 304, 22, c_bg, border_color=c_border, border_width=1, border_radius=3)
        cat_key = self.places_categories[self.places_selected_cat_idx]
        cat_name = "ALL CATEGORIES" if cat_key is None else plc.CATEGORY_LABELS.get(cat_key, cat_key.upper())
        cat_disp = f"◀  {cat_name.upper()}  ▶"
        display.draw_text(14, 98, cat_disp[:38], size="med", color=Colors.CYAN if is_c_active else Colors.TEXT_WHITE)

        # Field 2: Search by Name Button
        is_s_active = (self.places_search_field == 2)
        s_bg = Colors.SEL_BG if is_s_active else (22, 38, 56)
        s_border = Colors.CYAN if is_s_active else Colors.PANEL_BORDER
        display.draw_rect(8, 122, 304, 22, s_bg, border_color=s_border, border_width=1, border_radius=3)
        display.draw_text(14, 126, "▶ SEARCH BY NAME", size="med", color=Colors.CYAN if is_s_active else Colors.TEXT_WHITE)

        # Field 3: Nearby Assistance Button
        is_n_active = (self.places_search_field == 3)
        n_bg = Colors.SEL_BG if is_n_active else (20, 50, 32)
        n_border = Colors.GREEN if is_n_active else Colors.PANEL_BORDER
        display.draw_rect(8, 148, 304, 22, n_bg, border_color=n_border, border_width=1, border_radius=3)
        display.draw_text(14, 152, "▶ FIND NEARBY ASSISTANCE (50 KM)", size="med", color=Colors.GREEN if is_n_active else Colors.TEXT_WHITE)

        # Field 4: Back Button
        is_b_active = (self.places_search_field == 4)
        b_bg = Colors.SEL_BG if is_b_active else Colors.PANEL
        b_border = Colors.RED if is_b_active else Colors.PANEL_BORDER
        display.draw_rect(8, 174, 304, 20, b_bg, border_color=b_border, border_width=1, border_radius=3)
        display.draw_text(14, 177, "◀ BACK TO MAIN MENU", size="small", color=Colors.RED if is_b_active else Colors.TEXT_MUTED)

        # Region coverage notice
        cov = self.coverage_metadata
        reg_info = "TEST DATA | " + cov.region_name if cov and "Synthetic" in cov.region_name else "© OpenStreetMap contributors | ODbL 1.0" if cov else "UNKNOWN COVERAGE"
        display.draw_text(10, 202, reg_info[:48], size="small", color=Colors.TEXT_DARK)

    def refresh_places_results(self, state):
        reference = (state.gps.lat, state.gps.lon) if state.has_position else (None, None)
        if self.places_search_performed and reference != self.results_reference:
            if self.nearby_radius is None:
                self.execute_places_name_search(state)
            else:
                self.execute_places_nearby_search(self.nearby_radius, state)

    def _render_places_results(self, display: DisplayAdapter, state=None) -> None:
        state = state or self.navigation_state()
        self.refresh_places_results(state)
        display.draw_rect(8, 25, 304, 20, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=3)
        display.draw_text(12, 28, f"SEARCH RESULTS ({len(self.places_results)} FOUND)", size="med", color=Colors.CYAN)

        if not self.places_results:
            display.draw_rect(8, 50, 304, 134, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
            if self.nearby_radius is not None and not state.has_position:
                display.draw_text(20, 68, "POSITION UNAVAILABLE", size="med", color=Colors.AMBER)
                display.draw_text(20, 92, "Nearby search needs a valid position.", size="small", color=Colors.TEXT_WHITE)
                return
            title = "SEARCHING..." if self.search_busy else self.places_outcome or "NO MATCH"
            display.draw_text(20, 68, title, size="med", color=Colors.AMBER)
            display.draw_text(20, 92, self.places_coverage, size="small", color=Colors.AMBER)
            display.draw_text(20, 107, "Mapped coverage can be incomplete.", size="small", color=Colors.TEXT_MUTED)
            display.draw_text(20, 122, "Try another name or category.", size="small", color=Colors.TEXT_MUTED)
            display.draw_text(12, 202, "[Esc / Back: Return to Search]", size="small", color=Colors.TEXT_MUTED)
            return

        visible_count = 5
        start_idx = max(0, min(self.places_results_idx - visible_count // 2, len(self.places_results) - visible_count))
        for row, idx in enumerate(range(start_idx, min(len(self.places_results), start_idx + visible_count))):
            y = 48 + row * 27
            p = self.places_results[idx]
            is_active = (idx == self.places_results_idx)
            bg_col = Colors.SEL_BG if is_active else Colors.PANEL
            border_col = Colors.SEL_BORDER if is_active else Colors.PANEL_BORDER
            text_col = Colors.TEXT_WHITE if is_active else Colors.TEXT_MUTED
            prefix = "▶ " if is_active else "  "

            badge = f"TEST DATA #{p.id}" if p.is_test_data else CATEGORY_BADGES.get(p.category, "[MISC]")
            display.draw_rect(8, y, 304, 25, bg_col, border_color=border_col, border_width=1, border_radius=3)
            # Left: Badge and Name
            name_str = display.fit_text(f"{prefix}{badge} {p.name}", 202)
            display.draw_text(12, y + 4, name_str, size="med", color=text_col)
            # Right: Distance & Bearing
            solution = state.to_destination(p)
            vector_str = format_distance(solution.distance_m)
            display.draw_text(220, y + 5, display.fit_text(vector_str, 88, "small"), size="small", color=Colors.CYAN if is_active else Colors.TEXT_MUTED)

        total_str = f"{self.places_results_idx + 1}/{len(self.places_results)} | {self.places_coverage}"
        display.draw_text(12, 187, total_str[:48], size="small", color=Colors.AMBER if self.places_is_outside_coverage else Colors.TEXT_WHITE)
        display.draw_text(12, 202, "[▲/▼: Scroll]  [Enter: Details & Nav]  [Esc: Back]", size="small", color=Colors.TEXT_MUTED)

    def _render_place_details(self, display: DisplayAdapter, state: nav.NavigationState) -> None:
        display.draw_rect(8, 25, 304, 18, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=3)
        display.draw_text(12, 27, "PLACE DETAILS & SAFETY GUIDANCE", size="med", color=Colors.CYAN)

        p = self.selected_place
        if not p:
            display.draw_text(12, 50, "No place selected.", size="med", color=Colors.RED)
            return

        # Main Info Card
        display.draw_rect(8, 45, 304, 110, Colors.PANEL, border_color=Colors.PANEL_BORDER, border_radius=4)
        name_lines = display.wrap_text(p.display_name, 292, "small")
        name_pages = [name_lines[i:i + 3] for i in range(0, len(name_lines), 3)]
        coords = f"{p.lat:+.5f}, {p.lon:+.5f}" if nav.validate_wgs84(p.lat, p.lon)[0] else "unavailable"
        origin = f"OSM {p.osm_type}/{p.osm_id}" if p.osm_id else p.source
        pages = name_pages + [
            [origin, f"Coords: {coords}", f"Category: {p.category}"],
            [f"Extracted: {p.extracted_at or 'unknown'}", p.region_id or p.admin_level or "Region unknown",
             "Access / hours / safety UNVERIFIED"],
            ["© OpenStreetMap contributors" if p.osm_id else "TEST DATA" if p.is_test_data else p.source,
             "ODbL 1.0 | openstreetmap.org/copyright" if p.osm_id else "Fixture use only",
             (p.metadata or {}).get("coordinate_method", "")],
        ]
        self.detail_page = min(self.detail_page, len(pages) - 1)
        for index, line in enumerate(pages[self.detail_page]):
            display.draw_text(12, 49 + index * 14, display.fit_text(line, 292, "small"),
                              size="small", color=Colors.TEXT_WHITE)
        vector = state.to_destination(p)
        display.draw_text(12, 93, f"Direct vector: {format_distance(vector.distance_m)} / {format_bearing(vector.bearing_deg)}",
                          size="small", color=Colors.GREEN if vector.distance_m is not None else Colors.AMBER)
        label = "TEST DATA | " if p.is_test_data else ""
        display.draw_text(12, 107, f"{label}Left/Right: page {self.detail_page + 1}/{len(pages)}", size="small", color=Colors.CYAN)

        # CRITICAL SAFETY WARNING (Always displayed)
        display.draw_rect(10, 120, 300, 32, Colors.AMBER_DIM, border_color=Colors.AMBER, border_radius=3)
        display.draw_text(14, 123, "! CAUTION: Straight-line guidance only.", size="small", color=Colors.AMBER)
        display.draw_text(14, 136, "! Does NOT account for terrain, trails, or hazards.", size="small", color=Colors.AMBER)

        # Action Buttons
        # Button 0: Set active destination
        is_nav_act = (self.place_details_btn_idx == 0)
        display.draw_rect(8, 158, 304, 18, Colors.SEL_BG if is_nav_act else (20, 50, 32),
                          border_color=Colors.GREEN if is_nav_act else Colors.PANEL_BORDER, border_width=1, border_radius=3)
        display.draw_text(14, 161, "▶ SET AS ACTIVE DESTINATION", size="small", color=Colors.GREEN if is_nav_act else Colors.TEXT_WHITE)

        # Button 1: Save as waypoint
        is_wp_act = (self.place_details_btn_idx == 1)
        display.draw_rect(8, 178, 304, 18, Colors.SEL_BG if is_wp_act else Colors.PANEL,
                          border_color=Colors.CYAN if is_wp_act else Colors.PANEL_BORDER, border_width=1, border_radius=3)
        display.draw_text(14, 181, "▶ SAVE AS WAYPOINT", size="small", color=Colors.CYAN if is_wp_act else Colors.TEXT_WHITE)

        # Button 2: Back to results
        is_back_act = (self.place_details_btn_idx == 2)
        display.draw_rect(8, 198, 304, 18, Colors.SEL_BG if is_back_act else Colors.PANEL,
                          border_color=Colors.RED if is_back_act else Colors.PANEL_BORDER, border_width=1, border_radius=3)
        display.draw_text(14, 201, "◀ BACK TO RESULTS", size="small", color=Colors.RED if is_back_act else Colors.TEXT_MUTED)


# ==============================================================================
# 5. Automated Verification Test Suite (--test)
# ==============================================================================

def run_app_tests() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        return _run_app_tests(Path(directory))


def _run_app_tests(scratch: Path) -> bool:
    """
    Executes automated self-tests verifying:
    1. Menu navigation via discrete actions
    2. Arrow rotation relative to device heading
    3. Distance updates when simulated position moves
    4. Waypoint selection as active destination
    5. Return to starting position
    6. GPS failure handling
    7. Stale GPS detection & recovery
    8. Missing compass reading handling
    9. Screen layout bounds (all text and rects within 320x240)
    10. Stage 1 geodesic tests still pass
    """
    print("\n" + "=" * 65)
    print(" SURVIVE-X — Stage 2 UI Simulator Automated Self-Test Suite")
    print("=" * 65)

    passed = 0
    total = 0

    def assert_test(cond: bool, msg: str) -> None:
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"  [PASS] {msg}")
        else:
            print(f"  [FAIL] {msg}")
            raise AssertionError(f"Test failed: {msg}")

    # 1. Run Stage 1 Geodesic Math Tests
    print("\n[Step 1: Stage 1 Geodesic Mathematical Suite]")
    assert_test(nav.run_tests(), "Stage 1 navigation assertions strictly preserved")

    # 2. App State Initialization & Starting Position
    print("\n[Step 2: App Initialization & Starting Position]")
    test_wp_path = str(scratch / "test_ui_waypoints.json")
    if os.path.exists(test_wp_path):
        os.remove(test_wp_path)

    try:
        test_db = scratch / "places.db"
        test_db.write_bytes(Path(plc.DB_FILE_DEFAULT).read_bytes())
        app = SURVIVEXApp(storage_path=test_wp_path, db_path=str(test_db))
        display = HeadlessDisplayAdapter()

        assert_test(app.current_screen == Screen.HOME, "Initial screen is HOME")
        assert_test(app.start_position.name == "Start Position", "Starting position automatically captured")
        assert_test(app.waypoint_mgr.get_active_waypoint() is not None, "Active waypoint initialized")

        # 3. Menu Navigation via Actions (UP, DOWN, SELECT, BACK)
        print("\n[Step 3: Keyboard-Only Menu Traversal]")
        assert_test(app.home_menu_idx == 0, "Initial menu index is 0")
        app.handle_action(Action.DOWN)
        assert_test(app.home_menu_idx == 1, "DOWN navigates to item 1")
        app.handle_action(Action.DOWN)
        assert_test(app.home_menu_idx == 2, "DOWN navigates to item 2")
        app.handle_action(Action.UP)
        assert_test(app.home_menu_idx == 1, "UP navigates back to item 1")

        # Open Saved Locations (item 1)
        app.handle_action(Action.SELECT)
        assert_test(app.current_screen == Screen.WAYPOINTS, "SELECT opens WAYPOINTS screen")

        # Return to Home via BACK
        app.handle_action(Action.BACK)
        assert_test(app.current_screen == Screen.HOME, "BACK returns to HOME screen")

        # Open Live Navigation (item 0)
        app.home_menu_idx = 0
        app.handle_action(Action.SELECT)
        assert_test(app.current_screen == Screen.NAVIGATION, "SELECT opens NAVIGATION screen")

        # 4. Arrow Rotation Relative to Device Heading
        print("\n[Step 4: Relative Arrow Rotation Verification]")
        dest = app.waypoint_mgr.get_active_waypoint()
        initial_bearing = nav.initial_bearing_deg(app.sim_lat, app.sim_lon, dest.lat, dest.lon)

        # Align heading directly with bearing -> turn angle must be 0° (↑ AHEAD)
        app.sim_mag_heading = (initial_bearing - app.declination_deg) % 360.0
        sol_ahead = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(abs(sol_ahead.turn_angle_deg) < 1.0, f"Turn angle 0° when aligned ({sol_ahead.turn_angle_deg:.1f}°)")
        assert_test(sol_ahead.arrow == "↑", "Aligned arrow is '↑'")

        # Rotate heading 90° right -> target is now 90° to the left (← LEFT)
        app.sim_mag_heading = (app.sim_mag_heading + 90.0) % 360.0
        sol_left = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(abs(sol_left.turn_angle_deg - (-90.0)) < 1.0, f"Turn angle is -90° when turned right ({sol_left.turn_angle_deg:.1f}°)")
        assert_test(sol_left.arrow == "←", "Left relative arrow is '←'")

        # Rotate heading 180° -> target is now directly behind (↓ BEHIND)
        app.sim_mag_heading = (app.sim_mag_heading + 90.0) % 360.0
        sol_behind = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(abs(abs(sol_behind.turn_angle_deg) - 180.0) < 1.0, f"Turn angle is 180° when reversed ({sol_behind.turn_angle_deg:.1f}°)")
        assert_test(sol_behind.arrow == "↓", "Direct reverse arrow is '↓'")

        # 5. Distance Updates When Simulated Position Changes
        print("\n[Step 5: Dynamic Position & Distance Updates]")
        d_orig = nav.haversine_distance_m(app.sim_lat, app.sim_lon, dest.lat, dest.lon)
        app.handle_action(Action.SIM_MOVE_NORTH)  # +0.001° lat toward summit
        d_moved = nav.haversine_distance_m(app.sim_lat, app.sim_lon, dest.lat, dest.lon)
        assert_test(d_moved < d_orig, f"Distance reduced as GPS moved toward destination ({d_orig:.1f}m -> {d_moved:.1f}m)")

        # 6. Waypoint Selection Updates Active Destination
        print("\n[Step 6: Waypoint Selection & Active Target Update]")
        app.current_screen = Screen.WAYPOINTS
        app.waypoints_idx = 3  # Select second saved waypoint (e.g., Camp Muir)
        app.handle_action(Action.SELECT)
        assert_test(app.current_screen == Screen.WAYPOINT_ACTIONS, "Selecting waypoint opens action modal")
        app.action_menu_idx = 0  # "1. Set as Active Destination"
        app.handle_action(Action.SELECT)
        assert_test(app.current_screen == Screen.NAVIGATION, "Executing Set Target returns to NAVIGATION")
        assert_test(app.waypoint_mgr.active_waypoint_name == "Camp Muir", "Active target updated to 'Camp Muir'")

        # 7. Return to Saved Starting Position
        print("\n[Step 7: Return to Starting Position (Backtrack)]")
        app.current_screen = Screen.WAYPOINTS
        app.waypoints_idx = 1  # "Return to starting point"
        app.handle_action(Action.SELECT)
        assert_test(app.current_screen == Screen.NAVIGATION, "Return to start navigates to live view")
        assert_test(app.waypoint_mgr.active_waypoint_name == "Start Point", "Target set to 'Start Point'")

        # 8. GPS Failure Handling (No Satellite Fix)
        print("\n[Step 8: GPS Loss Disables Directional Guidance]")
        app.sim_has_fix = False
        sol_nofix = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(sol_nofix.status == nav.NavStatus.NO_FIX, "GPS loss resolves to NO_GPS_FIX status")
        assert_test(sol_nofix.arrow == "?", "Directional arrow is disabled and replaced by '?'")
        assert_test(sol_nofix.arrow_label == "NO_GPS_FIX", "Guidance label matches NO_GPS_FIX")
        app.sim_has_fix = True

        # 9. Stale GPS Detection & Simulated Stale Mode
        print("\n[Step 9: Stale GPS Detection & Simulated Mode]")
        app.sim_stale_mode = True
        app.sim_gps_timestamp = time.time() - 25.0  # 25 seconds old
        sol_stale = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(sol_stale.status == nav.NavStatus.STALE_GPS, "Old GPS reading triggers STALE_GPS status")
        assert_test(sol_stale.arrow_label == "STALE_GPS_DATA", "Stale GPS arrow label is STALE_GPS_DATA")
        app.sim_stale_mode = False
        app.sim_gps_timestamp = time.time()

        # 10. Regression Test: Simulated GPS Stays Fresh and Valid While UI is Running
        print("\n[Step 10: Regression Test: GPS Remains Fresh While UI Is Running]")
        t_base = time.time()
        # Advance simulated time past 10s staleness threshold
        t_future = t_base + 15.0
        app.tick(current_time=t_future)
        gps_running = app.get_current_gps()
        is_fresh, status_fresh, _ = gps_running.validate(current_time=t_future)
        assert_test(is_fresh is True, "Running UI keeps simulated GPS fresh past 10s threshold")
        assert_test(status_fresh == nav.NavStatus.OK, "Running UI GPS status remains NavStatus.OK")
        sol_running = nav.compute_nav_solution(gps_running, app.get_current_compass(current_time=t_future), dest, declination_deg=app.declination_deg, current_time=t_future)
        assert_test(sol_running.status == nav.NavStatus.OK, "Running UI navigation solution status remains OK")
        assert_test(sol_running.arrow_label != "GPS_UNAVAILABLE", "Running UI guidance label is not GPS_UNAVAILABLE")
        # Restore to current clock
        app.tick(current_time=time.time())

        # 11. GPS Status Consistency Across All Discrete States
        print("\n[Step 11: Header & Guidance State Consistency Check]")
        # A. Normal locked state
        v_ok, s_ok, _ = app.get_current_gps().validate()
        assert_test(s_ok == nav.NavStatus.OK, "Normal GPS reading resolves to NavStatus.OK")
        # B. Stale state
        app.sim_stale_mode = True
        app.sim_gps_timestamp = time.time() - 35.0
        v_stale, s_stale, _ = app.get_current_gps().validate()
        assert_test(s_stale == nav.NavStatus.STALE_GPS, "Stale reading resolves to NavStatus.STALE_GPS")
        sol_s = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(sol_s.status == s_stale, "Header status and navigation solution status strictly match on STALE_GPS")
        app.sim_stale_mode = False
        app.sim_gps_timestamp = time.time()
        # C. No fix state
        app.sim_has_fix = False
        v_nofix, s_nofix, _ = app.get_current_gps().validate()
        assert_test(s_nofix == nav.NavStatus.NO_FIX, "Unfixed reading resolves to NavStatus.NO_FIX")
        sol_nf = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(sol_nf.status == s_nofix, "Header status and navigation solution status strictly match on NO_FIX")
        app.sim_has_fix = True
        # D. Invalid coordinates
        app.sim_lat = 95.0
        v_inv, s_inv, _ = app.get_current_gps().validate()
        assert_test(s_inv == nav.NavStatus.INVALID_COORDS, "Out of bounds latitude resolves to INVALID_COORDS")
        sol_inv = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(sol_inv.status == s_inv, "Header status and navigation solution status strictly match on INVALID_COORDS")
        app.sim_lat = 46.786500

        # 12. Missing Compass Handling
        print("\n[Step 12: Missing Compass Handling]")
        app.sim_compass_available = False
        sol_no_compass = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), dest, declination_deg=app.declination_deg)
        assert_test(sol_no_compass.status == nav.NavStatus.MISSING_HEADING, "Missing compass resolves to MISSING_HEADING")
        assert_test(sol_no_compass.arrow == "—", "Missing compass arrow rendered as '—'")
        assert_test(sol_no_compass.bearing_deg > 0.0, "True bearing calculation remains valid")
        app.sim_compass_available = True

        # 13. Screen Coordinate Layout Bounds Check (All screens fit in 320x240)
        print("\n[Step 13: 320x240 Pixel Bounding Envelope Check]")
        if not app.selected_place:
            sample_places = app.places_db.search_by_name("Muir", limit=1)
            app.selected_place = sample_places[0] if sample_places else None
        for screen in [Screen.HOME, Screen.NAVIGATION, Screen.WAYPOINTS, Screen.WAYPOINT_ACTIONS, Screen.STATUS, Screen.ASSISTANT_INFO, Screen.COORD_INPUT, Screen.PLACES_SEARCH, Screen.PLACES_RESULTS, Screen.PLACE_DETAILS]:
            app.current_screen = screen
            display.clear()
            app.render(display)
            assert_test(len(display.draw_log) > 0, f"Screen {screen.name} rendered {len(display.draw_log)} elements within bounds")

        # 14. Explicit Sensor Mode Cycling & Indication (Stage 2.5)
        print("\n[Step 14: Explicit Sensor Mode Cycling & Badge Check]")
        assert_test(app.sensor_mode == nav.SensorMode.SIMULATED, "Default mode is SIMULATED")
        # Check simulated mode badge is SIM FIX, never LOCKED 3D
        display.clear()
        app._render_header(display, app.get_current_gps(), time.time())
        assert_test(any(d.get("text") == "SIM FIX" for d in display.draw_log), "Simulated mode displays 'SIM FIX' badge")
        assert_test(all(d.get("text") != "LOCKED 3D" for d in display.draw_log), "Simulated mode NEVER displays 'LOCKED 3D'")

        # Cycle to MANUAL COORDINATES
        app.cycle_sensor_mode()
        assert_test(app.sensor_mode == nav.SensorMode.MANUAL, "Mode cycled to MANUAL COORDINATES")
        display.clear()
        app._render_header(display, app.get_current_gps(), time.time())
        assert_test(any(d.get("text") == "MANUAL UNSET" for d in display.draw_log), "Manual mode requires user-entered coordinates")
        assert_test(all(d.get("text") != "LOCKED 3D" for d in display.draw_log), "Manual mode NEVER displays 'LOCKED 3D'")

        # Cycle to LIVE GNSS
        app.cycle_sensor_mode()
        assert_test(app.sensor_mode == nav.SensorMode.LIVE_GNSS, "Mode cycled to LIVE GNSS")
        display.clear()
        app._render_header(display, app.get_current_gps(), time.time())
        assert_test(any(d.get("text") == "NO GNSS HW" for d in display.draw_log), "Live GNSS without hardware displays 'NO GNSS HW' badge")
        assert_test(all(d.get("text") != "LOCKED 3D" for d in display.draw_log), "Live GNSS disconnected NEVER displays 'LOCKED 3D'")

        # Cycle back to SIMULATED
        app.cycle_sensor_mode()
        assert_test(app.sensor_mode == nav.SensorMode.SIMULATED, "Mode cycled back to SIMULATED")

        # 15. Manual Coordinate Entry & Bangalore-to-Mysore Geodesic Navigation
        print("\n[Step 15: Bangalore-to-Mysore Manual Geodesic Navigation]")
        # Set manual current position to Bangalore (12.9716° N, 77.5946° E)
        ok_pos = app.set_manual_coordinates(12.9716, 77.5946, alt=920.0)
        assert_test(ok_pos is True, "Bangalore position accepted")
        assert_test(app.sensor_mode == nav.SensorMode.MANUAL, "Setting coordinates activates MANUAL mode")
        assert_test(app.manual_lat == 12.9716 and app.manual_lon == 77.5946, "Manual coordinates match Bangalore")
        assert_test(app.manual_alt == 920.0, "Manual altitude preserved")

        # Set manual destination to Mysore (12.2958° N, 76.6394° E)
        ok_dest = app.set_manual_destination(12.2958, 76.6394, name="Mysore", alt=763.0)
        assert_test(ok_dest is True, "Mysore destination accepted")
        assert_test(app.waypoint_mgr.active_waypoint_name == "Mysore", "Active destination updated to Mysore")

        # Compute Geodesic Navigation Solution
        sol_bm = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), app.waypoint_mgr.get_active_waypoint(), declination_deg=0.0)
        assert_test(sol_bm.status == nav.NavStatus.POSITION_UNCERTAIN and sol_bm.turn_angle_deg is None, "Manual position retains geodesics without unverified steering")
        # Expected distance: ~128.02 km (128,017 m)
        dist_bm_km = sol_bm.distance_m / 1000.0
        assert_test(abs(dist_bm_km - 128.02) < 0.2, f"Bangalore -> Mysore distance is ~128.02 km ({dist_bm_km:.2f} km)")
        # Expected bearing: ~234.16° True (South-West)
        assert_test(abs(sol_bm.bearing_deg - 234.16) < 0.5, f"Bangalore -> Mysore bearing is ~234.16° ({sol_bm.bearing_deg:.2f}°)")

        # 16. Northern & Southern Hemisphere Coordinates
        print("\n[Step 16: Northern & Southern Hemisphere Geodesics]")
        # Southern Hemisphere: Sydney (-33.8688, 151.2093) to Melbourne (-37.8136, 144.9631)
        dist_syd_mel = nav.haversine_distance_m(-33.8688, 151.2093, -37.8136, 144.9631)
        bearing_syd_mel = nav.initial_bearing_deg(-33.8688, 151.2093, -37.8136, 144.9631)
        assert_test(abs(dist_syd_mel / 1000.0 - 713.43) < 0.5, f"Sydney -> Melbourne distance is ~713.43 km ({dist_syd_mel/1000.0:.2f} km)")
        assert_test(abs(bearing_syd_mel - 230.28) < 0.5, f"Sydney -> Melbourne bearing is ~230.28° ({bearing_syd_mel:.2f}°)")

        # Equator Crossing: Kampala (+0.3476° N, 32.5825° E) to Nairobi (-1.2921° S, 36.8219° E)
        dist_eq = nav.haversine_distance_m(0.3476, 32.5825, -1.2921, 36.8219)
        bearing_eq = nav.initial_bearing_deg(0.3476, 32.5825, -1.2921, 36.8219)
        assert_test(abs(dist_eq / 1000.0 - 505.40) < 0.5, f"Kampala -> Nairobi distance is ~505.40 km ({dist_eq/1000.0:.2f} km)")
        assert_test(abs(bearing_eq - 111.15) < 0.5, f"Kampala -> Nairobi bearing is ~111.15° ({bearing_eq:.2f}°)")

        # 17. Coordinate Validation & Error Handling (Preserve Existing Position)
        print("\n[Step 17: Coordinate Validation & Position Preservation]")
        # A. Invalid Latitude (> 90.0)
        res_lat_high = app.set_manual_coordinates(95.0, 77.5946)
        assert_test(res_lat_high is False, "Latitude 95.0° rejected")
        assert_test(app.manual_lat == 12.9716, "Preserves valid latitude on out-of-range input")
        assert_test(app.coord_error is not None, "Error message recorded for out-of-range latitude")

        # B. Invalid Latitude (< -90.0)
        res_lat_low = app.set_manual_coordinates(-95.0, 77.5946)
        assert_test(res_lat_low is False, "Latitude -95.0° rejected")

        # C. Invalid Longitude (> 180.0)
        res_lon_high = app.set_manual_coordinates(12.9716, 185.0)
        assert_test(res_lon_high is False, "Longitude 185.0° rejected")
        assert_test(app.manual_lon == 77.5946, "Preserves valid longitude on out-of-range input")

        # D. Invalid Longitude (< -180.0)
        res_lon_low = app.set_manual_coordinates(12.9716, -185.0)
        assert_test(res_lon_low is False, "Longitude -185.0° rejected")

        # E. Editor String Parsing Error
        app.coord_lat_str = "not_a_number"
        app.coord_lon_str = "77.5946"
        res_parse = app.validate_and_apply_coords()
        assert_test(res_parse is False, "String 'not_a_number' rejected in editor")
        assert_test(app.manual_lat == 12.9716, "Current position untouched when editor encounters error")

        # 18. Simulated Altitude & Compass Heading Rotation
        print("\n[Step 18: Simulated Altitude & Compass Rotation]")
        # Manual mode with no altitude
        app.manual_alt = None
        gps_no_alt = app.get_current_gps()
        assert_test(gps_no_alt.altitude_m is None, "Manual GPS altitude is None when unsupplied")

        # Switch to SIMULATED
        app.sensor_mode = nav.SensorMode.SIMULATED
        gps_sim_alt = app.get_current_gps()
        assert_test(gps_sim_alt.altitude_m == 1600.0, "Simulated GPS altitude is marked 1600m")

        # Heading rotation with H / L
        orig_hdg = app.sim_mag_heading
        app.handle_action(Action.SIM_ROTATE_RIGHT)
        assert_test(app.sim_mag_heading == (orig_hdg + 15.0) % 360.0, f"H/L rotates heading clockwise ({orig_hdg:.0f}° -> {app.sim_mag_heading:.0f}°)")
        app.handle_action(Action.SIM_ROTATE_LEFT)
        assert_test(app.sim_mag_heading == orig_hdg, "H/L rotates heading counter-clockwise back to baseline")

        # 19. On-Screen Keypad & Direct Character Entry
        print("\n[Step 19: On-Screen Keypad & Character Input]")
        app.current_screen = Screen.COORD_INPUT
        app.coord_active_field = 1  # Lat field
        app.coord_lat_str = "+12"
        # Direct character input
        app.handle_action(Action.CHAR_INPUT, char=".")
        app.handle_action(Action.CHAR_INPUT, char="9")
        assert_test(app.coord_lat_str == "+12.9", f"Direct typing appends characters ({app.coord_lat_str})")
        # Backspace
        app.handle_action(Action.BACKSPACE)
        assert_test(app.coord_lat_str == "+12.", f"Backspace deletes last character ({app.coord_lat_str})")
        # Joystick navigation into Keypad grid
        app.coord_active_field = 5  # Keypad focused
        app.coord_keypad_row = 0; app.coord_keypad_col = 0  # Button '1'
        app.handle_action(Action.SELECT)
        assert_test(app.coord_lat_str == "+12.1", f"Keypad [1] button inputs '1' ({app.coord_lat_str})")

        # 20. Offline Places Database Initialization & Coverage Check (Stage 3)
        print("\n[Step 20: Offline Places Database & Coverage Metadata]")
        assert_test(app.places_db is not None, "PlacesDatabase initialized")
        cov = app.places_db.get_coverage_metadata()
        assert_test(cov is not None and cov.place_count >= 20, f"Coverage metadata loaded: {cov.place_count if cov else 0} places in {cov.region_name if cov else 'N/A'}")
        assert_test(cov.contains(46.7865, -121.7350) is True, "Mount Rainier trailhead is inside coverage area")
        assert_test(cov.contains(12.9716, 77.5946) is False, "Bangalore is outside coverage area")

        # 21. Case-Insensitive Prefix and Autocomplete Name Search
        print("\n[Step 21: Case-Insensitive Prefix & Autocomplete Search]")
        app.places_query = "long"
        app.places_selected_cat_idx = 0  # ALL
        app.execute_places_name_search()
        assert_test(len(app.places_results) > 0, f"Found {len(app.places_results)} places matching prefix 'long'")
        assert_test(any("longmire" in p.name.lower() for p in app.places_results), "Prefix search found 'Longmire' fixtures")
        assert_test(app.places_results[0].distance_m is not None, "Search results include computed geodesic distance")
        assert_test(app.places_results[0].bearing_deg is not None, "Search results include computed true bearing")

        # 22. Duplicate Name Disambiguation & Distinct Entries
        print("\n[Step 22: Duplicate Name Disambiguation]")
        app.places_query = "Wilderness Ranger Station"
        app.execute_places_name_search()
        dup_ids = [p.id for p in app.places_results]
        assert_test(len(dup_ids) >= 2, f"Found {len(dup_ids)} duplicate/variant entries for 'Wilderness Ranger Station'")
        assert_test(len(dup_ids) == len(set(dup_ids)), "Duplicate names have strictly distinct database IDs")

        # 23. Nearby Assistance Discovery Sorted Geodesically
        print("\n[Step 23: Nearby Assistance Discovery Sorted Geodesically]")
        app.sim_lat = 46.786500
        app.sim_lon = -121.735000
        app.sensor_mode = nav.SensorMode.SIMULATED
        app.places_selected_cat_idx = 0  # ALL
        app.execute_places_nearby_search(radius_km=50.0)
        assert_test(len(app.places_results) > 0, f"Found {len(app.places_results)} nearby assistance points")
        distances = [p.distance_m for p in app.places_results if p.distance_m is not None]
        is_sorted = all(distances[i] <= distances[i + 1] for i in range(len(distances) - 1))
        assert_test(is_sorted is True, "Nearby assistance points are strictly sorted ascending by distance")

        # Test Category Filter on Nearby: Hospitals
        hosp_idx = app.places_categories.index("hospital")
        app.places_selected_cat_idx = hosp_idx
        app.execute_places_nearby_search(radius_km=50.0)
        assert_test(len(app.places_results) > 0, "Found nearby hospital")
        assert_test(all(p.category == "hospital" for p in app.places_results), "Category filter strictly enforced for hospital")
        app.places_selected_cat_idx = 0  # Reset to ALL

        # 24. Outside Coverage Area Indication
        print("\n[Step 24: Outside Coverage Area Detection & Indication]")
        app.set_manual_coordinates(12.9716, 77.5946)
        app.execute_places_nearby_search(radius_km=50.0)
        assert_test(app.places_is_outside_coverage is True, "Places outside coverage flagged correctly")
        assert_test(len(app.places_results) == 0, "No places found 50km from Bangalore in Cascades database")
        display.clear()
        app._render_places_results(display)
        assert_test(any("OUTSIDE COVERAGE" in d.get("text", "") for d in display.draw_log), "UI displays outside coverage notice")

        # 25. Place Details, Safety Warning & Navigation Target Assignment
        print("\n[Step 25: Place Details, Safety Warning & Nav Target Assignment]")
        app.sensor_mode = nav.SensorMode.SIMULATED
        app.sim_lat = 46.786500
        app.sim_lon = -121.735000
        app.places_query = "Muir"
        app.places_selected_cat_idx = 0
        app.execute_places_name_search()
        assert_test(len(app.places_results) > 0, "Places available for selection")
        target_place = app.places_results[0]
        app.selected_place = target_place
        app.current_screen = Screen.PLACE_DETAILS

        # Verify safety warning rendered
        display.clear()
        app.render(display)
        has_warning = any("Straight-line guidance only" in d.get("text", "") for d in display.draw_log)
        assert_test(has_warning is True, "Safety warning rendered on PLACE_DETAILS screen")

        # Execute "Set as active destination" (button 0)
        app.place_details_btn_idx = 0
        app.handle_action(Action.SELECT)
        assert_test(app.current_screen == Screen.NAVIGATION, "Selecting destination transitions to NAVIGATION")
        assert_test(app.waypoint_mgr.active_waypoint_name == target_place.name, f"Active waypoint updated to '{target_place.name}'")
        sol_place = nav.compute_nav_solution(app.get_current_gps(), app.get_current_compass(), app.waypoint_mgr.get_active_waypoint(), declination_deg=app.declination_deg)
        assert_test(sol_place.status == nav.NavStatus.OK, "Navigation solution is OK toward selected place")
        assert_test(sol_place.distance_m > 0.0, "Geodesic distance to place is positive")

        # 26. Save Place as Waypoint Persistence
        print("\n[Step 26: Save Place as Persistent Waypoint]")
        app.current_screen = Screen.PLACE_DETAILS
        app.place_details_btn_idx = 1  # [ SAVE AS WAYPOINT ]
        app.handle_action(Action.SELECT)
        saved_names = [w.name for w in app.waypoint_mgr.list_waypoints()]
        assert_test(target_place.name in saved_names, f"Place '{target_place.name}' saved to Waypoints list")

    finally:
        if os.path.exists(test_wp_path):
            os.remove(test_wp_path)
        tmp = test_wp_path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)

    print("\n" + "=" * 65)
    print(f" ALL STAGE 1, 2, 2.5 & 3 TESTS PASSED: {passed} / {total} assertions verified.")
    print("=" * 65)
    return True


# ==============================================================================
# 6. Interactive Demo Mode (--demo)
# ==============================================================================

def run_app_demo(scale: int = 3, step_delay: float = 0.5) -> None:
    """
    Automated scripted demonstration:
    Walks through all screens, turns heading, moves GPS, and exercises all menus
    in the graphical Pygame simulator.
    """
    if pygame is None:
        print("Error: Pygame is required for the graphical demo.")
        sys.exit(1)

    print("\n--- Launching Automated Graphical UI Demonstration (320x240 @ 3x) ---\n")
    display = PygameDisplayAdapter(scale=scale, title="SURVIVE-X — Demonstration Walkthrough")
    app = SURVIVEXApp(storage_path="waypoints_demo.json")

    demo_script = [
        (Action.NONE, "HOME Screen: Main Tactical Menu"),
        (Action.DOWN, "HOME Screen: Move selection down to Saved Locations"),
        (Action.DOWN, "HOME Screen: Move selection to Places & Nearby Assistance"),
        (Action.SELECT, "PLACES: Offline Spatial Search & Discovery"),
        (Action.DOWN, "PLACES: Focus Category Filter"),
        (Action.DOWN, "PLACES: Focus Search by Name"),
        (Action.SELECT, "PLACES: Search Results with Geodesic Vectors"),
        (Action.SELECT, "DETAILS: Place Information & Terrain Safety Warning"),
        (Action.SELECT, "NAVIGATION: Active Target Updated to Selected Place"),
        (Action.SIM_ROTATE_RIGHT, "NAVIGATION Screen: Rotate heading +15° Right"),
        (Action.SIM_ROTATE_RIGHT, "NAVIGATION Screen: Rotate heading +15° Right"),
        (Action.SIM_MOVE_NORTH, "NAVIGATION Screen: Move GPS +111m North"),
        (Action.SIM_MOVE_NORTH, "NAVIGATION Screen: Move GPS +111m North"),
        (Action.SIM_TOGGLE_FIX, "NAVIGATION Screen: Simulate GPS Loss (No Satellite Lock)"),
        (Action.SIM_TOGGLE_FIX, "NAVIGATION Screen: Restore GPS Fix"),
        (Action.SIM_TOGGLE_STALE, "NAVIGATION Screen: Simulate Stale GPS Data (>30s old)"),
        (Action.SIM_TOGGLE_STALE, "NAVIGATION Screen: Recover from Stale GPS Data"),
        (Action.BACK, "HOME Screen: Return to Main Menu"),
        (Action.DOWN, "HOME Screen: Move to Enter Coordinates"),
        (Action.DOWN, "HOME Screen: Move to Sensor Diagnostics"),
        (Action.SELECT, "STATUS Screen: GPS & Compass Sensor Health Diagnostics"),
        (Action.BACK, "HOME Screen: Return to Main Menu"),
    ]

    try:
        clock = pygame.time.Clock()
        for action, desc in demo_script:
            app.set_notification(desc)
            app.handle_action(action)
            app.tick()
            app.render(display)
            start_t = time.time()
            while (time.time() - start_t) < step_delay:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_q):
                        return
                clock.tick(30)
    finally:
        display.close()
        if os.path.exists("waypoints_demo.json"):
            os.remove("waypoints_demo.json")
        if os.path.exists("waypoints_demo.json.tmp"):
            os.remove("waypoints_demo.json.tmp")

    print("\n--- Graphical Simulator Demonstration Completed Successfully ---")


# ==============================================================================
# 7. Main Interactive Application Loop
# ==============================================================================

def run_interactive(scale: int = 3, db_path=None, recorder=None) -> None:
    """Runs live graphical Pygame simulator on macOS."""
    if pygame is None:
        print("\n[ERROR] Pygame is required for the macOS desktop simulator.")
        print(f"Please launch using the local virtual environment:")
        print(f"  /Users/kavya/SURVIVE-X-QWEN-OPT/.venv/bin/python3 app.py\n")
        sys.exit(1)

    display = PygameDisplayAdapter(scale=scale)
    app = SURVIVEXApp(db_path=db_path, async_search=True, recorder=recorder)
    input_adapter = PygameInputAdapter(app_ref=app)
    clock = pygame.time.Clock()

    print(f"\n[SURVIVE-X] Graphical 320x240 Simulator running (Window: {320*scale}x{240*scale}).")
    print("Controls: Arrow keys / Enter / Esc for menus; WASD for GPS; H/L for Heading; M=Mode; G=Fix; T=Stale; Q=Quit.\n")

    try:
        dirty = True
        while app.running:
            action = input_adapter.read_action(timeout_sec=0.01)
            while action != Action.NONE:
                if action == Action.QUIT:
                    app.running = False
                    break
                app.handle_action(action, char=input_adapter.last_char)
                dirty = True
                action = input_adapter.read_action(timeout_sec=0.0)

            dirty = app.tick() or dirty
            if dirty:
                app.render(display)
                dirty = False
            clock.tick(30)

    except (KeyboardInterrupt, SystemExit):
        app.running = False
    finally:
        app.close()
        display.close()
        print("\nExited SURVIVE-X Graphical Simulator. Stay safe!\n")


def main():
    parser = argparse.ArgumentParser(description="SURVIVE-X 320x240 Graphical SPI TFT Simulator")
    parser.add_argument("--test", action="store_true", help="Run automated self-tests and layout verification")
    parser.add_argument("--demo", action="store_true", help="Run scripted automated graphical demonstration")
    parser.add_argument("--scale", type=int, default=3, choices=[1, 2, 3, 4], help="Window scale factor (default: 3 = 960x720)")
    parser.add_argument("--delay", type=float, default=0.5, help="Step delay for demonstration (default: 0.5s)")
    parser.add_argument("--db", help="Offline SQLite places database")
    journey.add_options(parser)
    args = parser.parse_args()

    if args.test:
        success = run_app_tests()
        sys.exit(0 if success else 1)

    if args.demo:
        run_app_demo(scale=args.scale, step_delay=args.delay)
        sys.exit(0)

    run_interactive(scale=args.scale, db_path=args.db, recorder=journey.configured(args))


if __name__ == "__main__":
    main()
