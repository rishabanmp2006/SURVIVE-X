#!/usr/bin/env python3
"""
SURVIVE-X — Stage 3: Offline Places Database & Assistance Discovery
Target Hardware: Arduino UNO Q (2GB RAM, Quad Cortex-A53, Linux ARM64)
Host Environment: macOS Darwin arm64 (Apple Silicon M3)

Provides deterministic, offline geographic place search and nearby assistance discovery
using the Python standard library sqlite3 engine:
1. SQLite spatial schema with indexed case-insensitive prefix and exact search.
2. Coordinate bounding-box prefiltering with antimeridian longitude wrapping
   and polar clamping before precise spherical Haversine distance calculations.
3. Category filtering (settlement, hospital, clinic, police, ranger_station, shelter, water_source).
4. Duplicate-name resolution and disambiguation by administrative area and distance.
5. Coverage bounding-box metadata with explicit outside-coverage detection.
6. Clearly labeled synthetic test fixtures (never fabricating fake real-world data).
7. Zero cloud APIs, zero external databases, zero map downloads.
"""

import argparse
import json
import heapq
import math
import os
import tempfile
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Re-use existing Stage 1 navigation module for geodesic calculations
import navigation as nav

REAL_DB_FILE = str(Path(__file__).resolve().parent / "places_bengaluru.db")

DB_FILE_DEFAULT = str(Path(__file__).resolve().parent / "places.db")

# Canonical assistance categories
CANONICAL_CATEGORIES = [
    "settlement",
    "hospital",
    "clinic",
    "police",
    "ranger_station",
    "shelter",
    "water_source",
]

CATEGORY_LABELS = {
    "settlement": "Settlement",
    "hospital": "Hospital",
    "clinic": "Clinic",
    "police": "Police / Sheriff",
    "ranger_station": "Ranger Station",
    "shelter": "Mapped Shelter",
    "water_source": "Mapped Water",
}


@dataclass
class Place:
    id: int
    name: str
    lat: float
    lon: float
    category: str
    source: str
    admin_level: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    osm_type: Optional[str] = None
    osm_id: Optional[int] = None
    extracted_at: Optional[str] = None
    region_id: Optional[str] = None
    original_name: Optional[str] = None
    distance_m: Optional[float] = None
    bearing_deg: Optional[float] = None

    @property
    def is_test_data(self):
        return self.source == "SYNTHETIC_TEST_FIXTURE"

    @property
    def display_name(self):
        return f"TEST DATA {self.name}" if self.is_test_data else self.name

    def formatted_distance(self) -> str:
        if self.distance_m is None:
            return "—"
        if self.distance_m >= 1000.0:
            return f"{self.distance_m / 1000.0:.1f} km"
        return f"{self.distance_m:,.0f} m"

    def formatted_bearing(self) -> str:
        if self.bearing_deg is None:
            return "—"
        return f"{self.bearing_deg:03.0f}°"


@dataclass
class CoverageMeta:
    region_name: str
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    place_count: int = 0
    description: str = ""

    def contains(self, lat: float, lon: float) -> bool:
        """Checks if a WGS84 point falls inside this coverage bounding box."""
        if not (self.min_lat <= lat <= self.max_lat):
            return False
        if self.min_lon <= self.max_lon:
            return self.min_lon <= lon <= self.max_lon
        # Wraps antimeridian
        return lon >= self.min_lon or lon <= self.max_lon


class PlacesDatabase:
    """
    Offline SQLite spatial places and assistance database.
    Optimized for low-memory embedded execution on Arduino UNO Q (Linux ARM64).
    """

    def __init__(self, db_path: str = DB_FILE_DEFAULT, read_only: bool = False):
        self.db_path = Path(db_path)
        if read_only:
            self.conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        else:
            self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            self._init_schema()
        self.has_aliases = self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='place_names'").fetchone() is not None

    def _init_schema(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL,
                admin_level TEXT,
                metadata TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_places_name_norm ON places(name_normalized);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_places_coords ON places(lat, lon);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_places_category ON places(category);")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS coverage_meta (
                region_name TEXT PRIMARY KEY,
                min_lat REAL NOT NULL,
                max_lat REAL NOT NULL,
                min_lon REAL NOT NULL,
                max_lon REAL NOT NULL,
                description TEXT
            );
        """)
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(places)")}
        for name, kind in (("osm_type", "TEXT"), ("osm_id", "INTEGER"), ("extracted_at", "TEXT"),
                           ("region_id", "TEXT"), ("original_name", "TEXT")):
            if name not in columns:
                cursor.execute(f"ALTER TABLE places ADD COLUMN {name} {kind}")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_osm_identity ON places(osm_type, osm_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_places_region ON places(region_id)")
        cursor.execute("""CREATE TABLE IF NOT EXISTS import_meta (
            region_id TEXT PRIMARY KEY, extracted_at TEXT NOT NULL, osm_timestamp TEXT,
            endpoint TEXT, query TEXT, sha256 TEXT, attribution TEXT NOT NULL)""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS place_names (
            normalized TEXT NOT NULL, place_id INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
            PRIMARY KEY(normalized, place_id))""")
        self.conn.commit()

    def insert_place(
        self,
        name: str,
        lat: float,
        lon: float,
        category: str,
        source: str = "SYNTHETIC_TEST_FIXTURE",
        admin_level: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        valid, err = nav.validate_wgs84(lat, lon)
        if not valid:
            raise ValueError(f"Invalid place coordinates ({lat}, {lon}): {err}")

        name_clean = name.strip()
        if not name_clean:
            raise ValueError("Place name cannot be empty.")

        name_norm = name_clean.casefold()
        meta_json = json.dumps(metadata) if metadata else None

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO places (name, name_normalized, lat, lon, category, source, admin_level, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name_clean, name_norm, float(lat), float(lon), category.strip().lower(), source.strip(), admin_level, meta_json),
        )
        self.conn.commit()
        return cursor.lastrowid

    def set_coverage(
        self,
        region_name: str,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        description: str = "",
    ) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO coverage_meta (region_name, min_lat, max_lat, min_lon, max_lon, description)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (region_name.strip(), float(min_lat), float(max_lat), float(min_lon), float(max_lon), description.strip()),
        )
        self.conn.commit()

    def get_coverages(self) -> List[CoverageMeta]:
        rows = self.conn.execute("SELECT * FROM coverage_meta ORDER BY region_name")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(places)")}
        coverages = []
        for row in rows:
            if "region_id" in columns:
                count = self.conn.execute("SELECT COUNT(*) FROM places WHERE region_id=? OR (region_id IS NULL AND source='SYNTHETIC_TEST_FIXTURE' AND ? LIKE '%Synthetic%')", (row["region_name"], row["region_name"])).fetchone()[0]
            else:
                count = self.conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
            coverages.append(CoverageMeta(**dict(row), place_count=count))
        return coverages

    def get_coverage(self) -> Optional[CoverageMeta]:
        coverages = self.get_coverages()
        return coverages[0] if coverages else None

    get_coverage_metadata = get_coverage

    def coverage_state(self, lat=None, lon=None) -> str:
        if self.conn.execute("SELECT COUNT(*) FROM places").fetchone()[0] == 0:
            return "EMPTY DATABASE"
        regions = self.get_coverages()
        if not regions or lat is None or lon is None:
            return "UNKNOWN COVERAGE"
        if not nav.validate_wgs84(lat, lon)[0]:
            return "UNKNOWN COVERAGE"
        return "IN COVERAGE" if any(c.contains(lat, lon) for c in regions) else "OUTSIDE COVERAGE"

    def is_within_coverage(self, lat: float, lon: float) -> Tuple[bool, Optional[CoverageMeta]]:
        regions = self.get_coverages()
        for region in regions:
            if region.contains(lat, lon):
                return True, region
        return False, regions[0] if regions else None

    def get_place_by_id(self, place_id: int) -> Optional[Place]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM places WHERE id = ?", (place_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_place(row)

    def fixture_at(self, lat, lon):
        """Recognize older waypoints saved without provenance at a fixture location."""
        row = self.conn.execute(
            "SELECT * FROM places WHERE lat=? AND lon=? AND source='SYNTHETIC_TEST_FIXTURE' LIMIT 1",
            (lat, lon),
        ).fetchone()
        return self._row_to_place(row) if row else None

    def _row_to_place(self, row: sqlite3.Row) -> Place:
        meta = None
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except Exception:
                pass
        return Place(
            id=row["id"],
            name=row["name"],
            lat=row["lat"],
            lon=row["lon"],
            category=row["category"],
            source=row["source"],
            admin_level=row["admin_level"],
            metadata=meta,
            **{key: row[key] for key in ("osm_type", "osm_id", "extracted_at", "region_id", "original_name") if key in row.keys()},
        )

    def search_by_name(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 50,
        ref_lat: Optional[float] = None,
        ref_lon: Optional[float] = None,
    ) -> List[Place]:
        """
        Indexed case-insensitive prefix and substring name search.
        Handles duplicate names by preserving separate entries with distinct IDs and admin levels.
        If reference coordinates are supplied, computes distance & bearing and sorts by relevance.
        """
        q = query.strip().casefold()
        if not q:
            return []
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Limit must be an integer from 1 to 1000.")
        if (ref_lat is None) != (ref_lon is None):
            raise ValueError("Both reference coordinates are required.")
        if ref_lat is not None and not nav.validate_wgs84(ref_lat, ref_lon)[0]:
            raise ValueError("Invalid reference coordinates.")
        category_sql = " AND category=?" if category else ""
        category_params = [category.strip().lower()] if category else []
        # Binary prefix range uses idx_places_name_norm. instr treats % and _ literally.
        upper = q + chr(0x10ffff)
        groups = [
            ("name_normalized = ?", [q]),
            ("name_normalized > ? AND name_normalized < ?", [q, upper]),
            ("instr(name_normalized, ?) > 0 AND NOT (name_normalized >= ? AND name_normalized < ?)", [q, q, upper]),
        ]
        results = []
        seen = set()
        for predicate, params in groups:
            sql = "SELECT * FROM places WHERE " + predicate + category_sql
            arguments = params + category_params
            if self.has_aliases:
                sql += (" UNION SELECT p.* FROM places p JOIN place_names a ON p.id=a.place_id WHERE "
                        + predicate.replace("name_normalized", "a.normalized") + category_sql)
                arguments = arguments + arguments
            rows = self.conn.execute(sql, arguments)
            def candidates():
                for row in rows:
                    place = self._row_to_place(row)
                    if place.id in seen:
                        continue
                    if not nav.validate_wgs84(place.lat, place.lon)[0]:
                        continue
                    if ref_lat is not None:
                        place.distance_m = nav.haversine_distance_m(ref_lat, ref_lon, place.lat, place.lon)
                        if place.distance_m > 0:
                            place.bearing_deg = nav.initial_bearing_deg(ref_lat, ref_lon, place.lat, place.lon)
                    yield place
            results.extend(heapq.nsmallest(limit - len(results), candidates(),
                                          key=lambda p: (p.distance_m or 0, p.name.casefold(), p.id)))
            seen.update(p.id for p in results)
            if len(results) == limit:
                break
        return results

    def search_nearby(
        self,
        lat: float,
        lon: float,
        radius_km: float = 50.0,
        category: Optional[str] = None,
        limit: int = 25,
    ) -> List[Place]:
        """
        Finds nearby places sorted ascending by straight-line geodesic distance.
        Uses a bounding-box prefilter with antimeridian longitude wrapping and polar clamping.
        """
        valid, err = nav.validate_wgs84(lat, lon)
        if not valid:
            raise ValueError(f"Invalid reference coordinates ({lat}, {lon}): {err}")

        if not nav.finite_number(radius_km) or radius_km <= 0:
            raise ValueError("Radius must be positive and finite.")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Limit must be an integer from 1 to 1000.")
        # Compute bounding box in degrees
        # 1 deg latitude ~ 111.195 km
        delta_lat = radius_km / 111.195
        lat_min = lat - delta_lat
        lat_max = lat + delta_lat

        # Polar clamping
        if lat_min < -90.0 or lat_max > 90.0:
            lat_min = max(-90.0, lat_min)
            lat_max = min(90.0, lat_max)
            lon_wrap = False
            lon_span_all = True
        else:
            lon_span_all = False
            # Longitude delta based on parallel radius: r * cos(lat)
            cos_lat = math.cos(math.radians(max(abs(lat_min), abs(lat_max))))
            if cos_lat < 1e-6:
                lon_span_all = True
                lon_wrap = False
            else:
                delta_lon = radius_km / (111.195 * cos_lat)
                lon_min = lon - delta_lon
                lon_max = lon + delta_lon
                lon_wrap = (lon_min < -180.0) or (lon_max > 180.0)

        cursor = self.conn.cursor()
        sql = "SELECT * FROM places WHERE (lat BETWEEN ? AND ?)"
        params: List[Any] = [lat_min, lat_max]

        if not lon_span_all:
            if lon_wrap:
                if lon_min < -180.0:
                    norm_min = lon_min + 360.0
                    sql += " AND (lon >= ? OR lon <= ?)"
                    params.extend([norm_min, lon_max])
                else:
                    norm_max = lon_max - 360.0
                    sql += " AND (lon >= ? OR lon <= ?)"
                    params.extend([lon_min, norm_max])
            else:
                sql += " AND (lon BETWEEN ? AND ?)"
                params.extend([lon_min, lon_max])

        if category:
            sql += " AND category = ?"
            params.append(category.strip().lower())

        cursor.execute(sql, params)
        rows = cursor

        max_dist_m = radius_km * 1000.0
        def candidates():
            for row in rows:
                place = self._row_to_place(row)
                if not nav.validate_wgs84(place.lat, place.lon)[0]:
                    continue
                distance = nav.haversine_distance_m(lat, lon, place.lat, place.lon)
                if distance <= max_dist_m:
                    place.distance_m = distance
                    if distance > 0:
                        place.bearing_deg = nav.initial_bearing_deg(lat, lon, place.lat, place.lon)
                    yield place
        return heapq.nsmallest(limit, candidates(), key=lambda p: (p.distance_m, p.name, p.id))

    def populate_synthetic_fixtures(self, force: bool = False) -> int:
        """
        Populates synthetic offline testing fixtures for Mount Rainier / Cascades extent.
        All records are explicitly labeled with source='SYNTHETIC_TEST_FIXTURE'.
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM places")
        count = cursor.fetchone()[0]
        if count > 0 and not force:
            return count

        if force:
            cursor.execute("DELETE FROM places")
            cursor.execute("DELETE FROM coverage_meta")
            self.conn.commit()

        # Define synthetic extent coverage
        self.set_coverage(
            region_name="Pacific Northwest Cascades (Synthetic)",
            min_lat=46.50,
            max_lat=47.25,
            min_lon=-122.30,
            max_lon=-121.30,
            description="Synthetic emergency, assistance and shelter points for Rainier offline validation.",
        )

        fixtures = [
            # 1. Settlements
            ("Paradise Base Village", 46.7865, -121.7350, "settlement", "Pierce County", {"elevation_m": 1600, "facility": "Subalpine Hub"}),
            ("Longmire Settlement", 46.7497, -121.8122, "settlement", "Pierce County", {"elevation_m": 840, "facility": "Valley Outpost"}),
            ("Ashford Outpost", 46.7580, -122.0310, "settlement", "Pierce County", {"elevation_m": 530, "facility": "Gateway Settlement"}),
            ("Sunrise Alpine Village", 46.9147, -121.6420, "settlement", "Pierce County", {"elevation_m": 1950, "facility": "North Slope Station"}),

            # 2. Hospitals (Emergency Care)
            ("Morton Community Aid Hospital", 46.5580, -122.2790, "hospital", "Lewis County", {"beds": 25, "emergency_24h": True}),
            ("Enumclaw Valley Medical Center", 47.2040, -121.9910, "hospital", "King County", {"trauma_service": True, "emergency_24h": True}),

            # 3. Clinics (First Aid Points)
            ("Paradise Emergency Clinic", 46.7872, -121.7345, "clinic", "Pierce County", {"first_aid": True, "mountain_rescue_station": True}),
            ("Longmire First Aid Clinic", 46.7502, -121.8118, "clinic", "Pierce County", {"first_aid": True, "splinting_kits": True}),

            # 4. Police / Sheriff
            ("Pierce County Wilderness Sheriff", 46.7592, -122.0295, "police", "Pierce County", {"vhf_radio": "155.055 MHz", "search_rescue_base": True}),
            ("Lewis County Sheriff Detachment", 46.5595, -122.2770, "police", "Lewis County", {"rural_patrol": True}),

            # 5. Ranger Stations (Resource & Permit Hubs)
            ("Longmire Ranger Station", 46.7490, -121.8130, "ranger_station", "Mount Rainier District", {"backcountry_permits": True, "emergency_beacon": True}),
            ("White River Ranger Station", 46.9025, -121.5560, "ranger_station", "Mount Rainier District", {"backcountry_permits": True}),
            ("Carbon River Ranger Station", 47.0005, -121.9210, "ranger_station", "Mount Rainier District", {"emergency_shelter": True}),
            ("Paradise Ranger Outpost", 46.7860, -121.7360, "ranger_station", "Mount Rainier District", {"climbing_briefings": True}),

            # 6. Shelters (Severe Weather Refuges)
            ("Camp Muir High Shelter", 46.8355, -121.7320, "shelter", "Rainier Alpine", {"elevation_m": 3070, "capacity": 25, "stone_cabin": True}),
            ("Camp Hazard Emergency Shelter", 46.8450, -121.7480, "shelter", "Rainier Alpine", {"elevation_m": 3400, "capacity": 6, "bivy_cache": True}),
            ("Summerland Emergency Cabin", 46.8670, -121.6550, "shelter", "Rainier Alpine", {"elevation_m": 1800, "capacity": 12}),
            ("Mystic Lake Shelter Cabin", 46.8920, -121.7580, "shelter", "Rainier Alpine", {"elevation_m": 1740, "log_shelter": True}),

            # 7. Water Sources (Mapped Streams & Springs)
            ("Paradise River Melt Spring", 46.7850, -121.7330, "water_source", "Mount Rainier", {"flow": "Perennial", "water_type": "Glacier Spring", "filter_required": True}),
            ("Reflection Lakes Filter Point", 46.7680, -121.7310, "water_source", "Mount Rainier", {"flow": "Lake Surface", "water_type": "Freshwater", "filter_required": True}),
            ("Fryingpan Creek Spring", 46.8780, -121.6210, "water_source", "Mount Rainier", {"flow": "Seasonal", "water_type": "Mountain Creek", "filter_required": True}),
            ("Nisqually Glacier Melt Outwash", 46.7720, -121.7850, "water_source", "Mount Rainier", {"flow": "Heavy Silty", "water_type": "Glacial", "prefilter_required": True}),

            # 8. Duplicate Names (for duplicate-name disambiguation testing)
            ("Wilderness Ranger Station", 46.7495, -121.8125, "ranger_station", "Longmire South", {"entrance": "Nisqually"}),
            ("Wilderness Ranger Station", 46.9030, -121.5570, "ranger_station", "White River North", {"entrance": "Sunrise"}),
            ("Mountain Spring Water", 46.7855, -121.7325, "water_source", "Paradise Basin", {"potable_treatment": True}),
            ("Mountain Spring Water", 46.9150, -121.6410, "water_source", "Sunrise Rim", {"potable_treatment": True}),
        ]

        added = 0
        for name, lat, lon, cat, admin, meta in fixtures:
            self.insert_place(
                name=name,
                lat=lat,
                lon=lon,
                category=cat,
                source="SYNTHETIC_TEST_FIXTURE",
                admin_level=admin,
                metadata=meta,
            )
            added += 1

        self.conn.execute("UPDATE places SET region_id=? WHERE source='SYNTHETIC_TEST_FIXTURE'",
                          ("Pacific Northwest Cascades (Synthetic)",))
        self.conn.commit()
        return added

    def close(self) -> None:
        if self.conn:
            self.conn.close()


# ==============================================================================
# Automated Self-Test Suite for Offline Places Engine
# ==============================================================================

def run_places_tests() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        return _run_places_tests(Path(directory))


def _run_places_tests(scratch: Path) -> bool:
    """
    Executes automated self-tests verifying:
    1. Exact and prefix name lookup.
    2. Duplicate name handling and disambiguation.
    3. Empty database handling.
    4. Search with no results.
    5. Nearby places sorting by distance.
    6. Category filtering.
    7. Invalid coordinate handling.
    8. Longitude wraparound across antimeridian.
    9. Coverage metadata and outside-coverage detection.
    """
    print("\n" + "=" * 65)
    print(" SURVIVE-X — Stage 3: Offline Places Database Automated Tests")
    print("=" * 65)

    test_db_path = str(scratch / "test_places_scratch.db")
    if os.path.exists(test_db_path):
        os.remove(test_db_path)

    passed = 0
    total = 0

    def assert_eq(cond: bool, msg: str) -> None:
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print(f"  [PASS] {msg}")
        else:
            print(f"  [FAIL] {msg}")
            raise AssertionError(f"Test failure: {msg}")

    try:
        # 1. Empty Database Handling
        print("\n[Test 1: Empty Database Handling]")
        db = PlacesDatabase(db_path=test_db_path)
        cov_empty = db.get_coverage()
        assert_eq(cov_empty is None, "Empty database reports None coverage")
        empty_search = db.search_by_name("Paradise")
        assert_eq(len(empty_search) == 0, "Search on empty database safely returns empty list")
        empty_nearby = db.search_nearby(46.7865, -121.7350, radius_km=10.0)
        assert_eq(len(empty_nearby) == 0, "Nearby search on empty database safely returns empty list")

        # 2. Populate Synthetic Fixtures
        print("\n[Test 2: Synthetic Fixtures Population]")
        added = db.populate_synthetic_fixtures(force=True)
        assert_eq(added >= 20, f"Populated {added} synthetic test fixtures")
        cov = db.get_coverage()
        assert_eq(cov is not None, "Coverage metadata recorded")
        assert_eq("Cascades" in cov.region_name, "Coverage region matches synthetic Cascades")
        assert_eq(cov.place_count == added, f"Coverage place count ({cov.place_count}) matches inserted count")

        # 3. Exact and Prefix Name Search
        print("\n[Test 3: Exact & Prefix Name Search]")
        # Exact match
        exact_res = db.search_by_name("Paradise Base Village")
        assert_eq(len(exact_res) >= 1, "Found exact match for 'Paradise Base Village'")
        assert_eq(exact_res[0].name == "Paradise Base Village", "Top result is exact match")

        # Case-insensitivity
        case_res = db.search_by_name("pArAdIsE bAsE ViLlAgE")
        assert_eq(len(case_res) >= 1, "Case-insensitive exact search succeeded")

        # Prefix search
        prefix_res = db.search_by_name("Camp")
        assert_eq(len(prefix_res) >= 2, f"Prefix 'Camp' found {len(prefix_res)} places")
        assert_eq(all(p.name.startswith("Camp") for p in prefix_res[:2]), "Prefix results start with 'Camp'")

        # Substring search
        sub_res = db.search_by_name("Muir")
        assert_eq(len(sub_res) >= 1, "Substring 'Muir' finds Camp Muir High Shelter")

        # 4. Duplicate Name Disambiguation
        print("\n[Test 4: Duplicate Name Handling]")
        dups = db.search_by_name("Wilderness Ranger Station")
        assert_eq(len(dups) == 2, f"Duplicate search returned both distinct entries ({len(dups)})")
        assert_eq(dups[0].id != dups[1].id, "Duplicate entries have unique IDs")
        assert_eq(dups[0].admin_level != dups[1].admin_level, "Duplicate entries preserve distinct admin levels")

        # 5. Search with No Results
        print("\n[Test 5: Search with No Results]")
        no_res = db.search_by_name("Atlantis Underwater Citadel")
        assert_eq(len(no_res) == 0, "Unknown place returns empty list without error")

        # 6. Category Filtering
        print("\n[Test 6: Category Filtering]")
        hospitals = db.search_by_name("", category="hospital", limit=10)
        # Search all hospitals via nearby or prefix
        all_hosp = db.search_nearby(46.7865, -121.7350, radius_km=100.0, category="hospital")
        assert_eq(len(all_hosp) >= 2, f"Found {len(all_hosp)} hospitals within 100km")
        assert_eq(all(h.category == "hospital" for h in all_hosp), "All results have hospital category")

        clinics = db.search_nearby(46.7865, -121.7350, radius_km=20.0, category="clinic")
        assert_eq(len(clinics) >= 1, "Found clinics within 20km of Paradise")
        assert_eq(clinics[0].category == "clinic", "Result is strictly a clinic")

        # 7. Nearby Place Sorting by Distance
        print("\n[Test 7: Nearby Place Sorting]")
        nearby = db.search_nearby(46.7865, -121.7350, radius_km=10.0, limit=10)
        assert_eq(len(nearby) >= 3, f"Found {len(nearby)} nearby places within 10km")
        # Distances must be monotonically non-decreasing
        is_sorted = all(nearby[i].distance_m <= nearby[i + 1].distance_m for i in range(len(nearby) - 1))
        assert_eq(is_sorted, "Nearby places are strictly sorted ascending by distance")
        assert_eq(nearby[0].distance_m < 200.0, f"Closest place is < 200m ({nearby[0].distance_m:.1f}m)")

        # 8. Coordinate Validation & Invalid Input Rejection
        print("\n[Test 8: Invalid Coordinates]")
        inv_coord_caught = False
        try:
            db.insert_place("Bad Place", 95.0, 0.0, "settlement")
        except ValueError:
            inv_coord_caught = True
        assert_eq(inv_coord_caught, "Latitude 95.0° properly rejected on insert")

        inv_nearby_caught = False
        try:
            db.search_nearby(-95.0, 0.0)
        except ValueError:
            inv_nearby_caught = True
        assert_eq(inv_nearby_caught, "Latitude -95.0° properly rejected on search_nearby")

        # 9. Longitude Wraparound across Antimeridian (+180° / -180°)
        print("\n[Test 9: Antimeridian Longitude Wraparound]")
        # Insert two points on opposite sides of the antimeridian
        p_east = db.insert_place("Antimeridian East Island", 0.0, 179.8, "settlement", source="SYNTHETIC_TEST_FIXTURE")
        p_west = db.insert_place("Antimeridian West Outpost", 0.0, -179.8, "settlement", source="SYNTHETIC_TEST_FIXTURE")
        # Search from 179.9° with radius 50km
        # Distance between 179.9° and -179.8° is only 0.3° (~33.3 km)
        wrap_results = db.search_nearby(0.0, 179.9, radius_km=50.0)
        wrap_names = [p.name for p in wrap_results]
        assert_eq("Antimeridian East Island" in wrap_names, "Found east side of antimeridian")
        assert_eq("Antimeridian West Outpost" in wrap_names, "Found west side of antimeridian across ±180° boundary")

        # 10. Coverage Bounding Box & Outside Coverage State
        print("\n[Test 10: Coverage Bounds & Outside Coverage Detection]")
        in_cov, _ = db.is_within_coverage(46.7865, -121.7350)
        assert_eq(in_cov is True, "Rainier coordinates fall inside Cascades coverage")

        # Bangalore coordinates are outside Cascades coverage
        out_cov, cov_info = db.is_within_coverage(12.9716, 77.5946)
        assert_eq(out_cov is False, "Bangalore correctly identified as OUTSIDE Cascades coverage")
        assert_eq(cov_info is not None, "Coverage metadata supplied for outside-region warning")

        db.close()

    finally:
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

    print("\n" + "=" * 65)
    print(f" ALL STAGE 3 PLACES TESTS PASSED: {passed} / {total} assertions verified.")
    print("=" * 65)
    return True


def main():
    parser = argparse.ArgumentParser(description="SURVIVE-X Offline Places Database Engine")
    parser.add_argument("--db", default=REAL_DB_FILE if Path(REAL_DB_FILE).exists() else DB_FILE_DEFAULT, help="Offline database path")
    parser.add_argument("--test", action="store_true", help="Run automated places database tests")
    parser.add_argument("--populate", action="store_true", help="Populate default synthetic places database")
    parser.add_argument("--search", type=str, help="Search places by name")
    parser.add_argument("--category", type=str, help="Filter by category")
    parser.add_argument("--nearby", nargs=2, type=float, metavar=("LAT", "LON"), help="Search nearby coordinates")
    parser.add_argument("--radius", type=float, default=25.0, help="Search radius in km (default: 25.0)")

    args = parser.parse_args()

    if args.test:
        success = run_places_tests()
        sys.exit(0 if success else 1)

    db = PlacesDatabase(args.db, read_only=not args.populate and Path(args.db).exists())

    if args.populate:
        if Path(args.db).resolve() != Path(DB_FILE_DEFAULT).resolve() and db.conn.execute("SELECT COUNT(*) FROM places WHERE source='OpenStreetMap'").fetchone()[0]:
            raise ValueError("Refusing to populate fixtures into an OSM database")
        count = db.populate_synthetic_fixtures(force=False)
        print(f"Populated {count} synthetic test places in {DB_FILE_DEFAULT}.")
        sys.exit(0)

    if args.search:
        results = db.search_by_name(args.search, category=args.category)
        print(f"\nSearch results for '{args.search}' ({len(results)} found):")
        for p in results:
            print(f"  [{p.category.upper()[:8]:8}] {p.display_name} ({p.lat:+.4f}°, {p.lon:+.4f}°) - Admin: {p.admin_level or 'N/A'}")
        sys.exit(0)

    if args.nearby:
        lat, lon = args.nearby
        results = db.search_nearby(lat, lon, radius_km=args.radius, category=args.category)
        print(f"\nNearby places within {args.radius}km of ({lat:+.4f}°, {lon:+.4f}°) [{len(results)} found]:")
        for p in results:
            print(f"  [{p.category.upper()[:8]:8}] {p.display_name:<28} {p.formatted_distance():>8} | True Hdg: {p.formatted_bearing()}")
        sys.exit(0)

    cov = db.get_coverage()
    if cov:
        print(f"Places database online: {cov.place_count} places ({cov.region_name}).")
    else:
        print(db.coverage_state())
    db.close()



if __name__ == "__main__":
    main()
