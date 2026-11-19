#!/usr/bin/env python3
"""Bounded OSM extraction and atomic offline database publication. Development tool."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import urllib.parse
import urllib.request

import navigation as nav
import places

BOUNDS = (12.70, 77.45, 13.05, 77.75)  # south, west, north, east
REGION = "Bengaluru–Bannerghatta"
ENDPOINT = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
ATTRIBUTION = "© OpenStreetMap contributors | ODbL 1.0 | https://www.openstreetmap.org/copyright"
MAX_BYTES = 32 * 1024 * 1024
QUERY = """[out:json][timeout:60][maxsize:134217728];
(
 nwr["place"~"^(city|town|village|hamlet|suburb|neighbourhood|isolated_dwelling)$"](12.70,77.45,13.05,77.75);
 nwr["amenity"~"^(hospital|clinic|police|ranger_station|shelter|drinking_water|water_point)$"](12.70,77.45,13.05,77.75);
 nwr["healthcare"~"^(hospital|clinic)$"](12.70,77.45,13.05,77.75);
 nwr["office"="forestry"](12.70,77.45,13.05,77.75);
 nwr["natural"="spring"](12.70,77.45,13.05,77.75);
 nwr["man_made"~"^(water_well|water_tap)$"](12.70,77.45,13.05,77.75);
);
out center tags;"""


def category_for(tags):
    amenity = tags.get("amenity")
    if amenity in ("hospital", "clinic", "police", "ranger_station", "shelter"):
        return amenity
    if tags.get("healthcare") in ("hospital", "clinic"):
        return tags["healthcare"]
    if tags.get("office") == "forestry":
        return "ranger_station"
    if (amenity in ("drinking_water", "water_point") or tags.get("natural") == "spring"
            or tags.get("man_made") in ("water_well", "water_tap")):
        return "water_source"
    if tags.get("place") in ("city", "town", "village", "hamlet", "suburb", "neighbourhood", "isolated_dwelling"):
        return "settlement"
    return None


def stable_id(element_type, element_id):
    if element_type not in ("node", "way", "relation") or type(element_id) is not int or not 0 < element_id < 2**60:
        raise ValueError("Invalid OSM identity")
    return element_id * 4 + ("node", "way", "relation").index(element_type)


def fetch(endpoint=ENDPOINT):
    request = urllib.request.Request(
        endpoint, data=urllib.parse.urlencode({"data": QUERY}).encode(),
        headers={"User-Agent": "SURVIVE-X-offline-extract/1.0", "Content-Type": "application/x-www-form-urlencoded"},
    )
    # One request. A rate-limit or timeout is surfaced without automatic retries.
    with urllib.request.urlopen(request, timeout=75) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Extract exceeds the 32 MiB cap")
    return data


def build_database(raw, output, extracted_at=None, endpoint=ENDPOINT):
    output = Path(output).resolve()
    if output == Path(places.DB_FILE_DEFAULT).resolve():
        raise ValueError("Use a separate OSM database; the original fixture database is protected.")
    if len(raw) > MAX_BYTES:
        raise ValueError("Extract exceeds the 32 MiB cap")
    document = json.loads(raw)
    if document.get("remark") or not isinstance(document.get("elements"), list):
        raise ValueError("Incomplete or invalid Overpass response")
    timestamp = document.get("osm3s", {}).get("timestamp_osm_base")
    if not timestamp:
        raise ValueError("Missing OSM source timestamp")
    extracted_at = extracted_at or datetime.now(timezone.utc).isoformat()
    seen = set()
    rows = []
    aliases = []
    skipped = 0
    south, west, north, east = BOUNDS
    for element in document["elements"]:
        kind, ident = element.get("type"), element.get("id")
        key = stable_id(kind, ident)
        if key in seen:
            continue
        seen.add(key)
        tags = element.get("tags", {})
        category = category_for(tags)
        position = element if kind == "node" else element.get("center", {})
        lat, lon = position.get("lat"), position.get("lon")
        if not category or not nav.validate_wgs84(lat, lon)[0] or not (south <= lat <= north and west <= lon <= east):
            skipped += 1
            continue
        original_name = tags.get("name", "")
        name = original_name or tags.get("name:en") or f"{category.replace('_', ' ')} [OSM {kind}/{ident}]"
        for tag in ("name:en", "name:kn", "alt_name", "official_name", "short_name"):
            for alias in tags.get(tag, "").split(";"):
                if alias.strip():
                    aliases.append((alias.strip().casefold(), key))
        metadata = {"tags": tags, "coordinate_method": "node" if kind == "node" else "bounding_box_center",
                    "operational_status": "UNVERIFIED"}
        rows.append((key, name, name.casefold(), lat, lon, category, "OpenStreetMap",
                     tags.get("addr:suburb") or tags.get("addr:city"), json.dumps(metadata, ensure_ascii=False),
                     kind, ident, extracted_at, REGION, original_name))
    if not rows:
        raise ValueError("No usable places; existing database preserved")
    # Build and validate before touching the deployed path. Directory lock serializes publishers.
    directory_fd = os.open(output.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(dir=output.parent, prefix=".places-import-") as directory:
            staged = Path(directory) / "places.db"
            if output.exists():
                with sqlite3.connect(output.as_uri() + "?mode=ro", uri=True) as old:
                    if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Existing database corrupt; replacement refused")
                    if old.execute("SELECT COUNT(*) FROM places WHERE source != 'OpenStreetMap'").fetchone()[0]:
                        raise ValueError("Output contains non-OSM data; choose a separate file")
                    with sqlite3.connect(staged) as copy:
                        old.backup(copy)
            db = places.PlacesDatabase(str(staged))
            try:
                with db.conn:
                    db.conn.execute("DELETE FROM places WHERE region_id=?", (REGION,))
                    db.conn.executemany("""INSERT INTO places
                        (id,name,name_normalized,lat,lon,category,source,admin_level,metadata,
                         osm_type,osm_id,extracted_at,region_id,original_name)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
                    db.conn.executemany("INSERT OR IGNORE INTO place_names VALUES (?,?)", aliases)
                    db.conn.execute("INSERT OR REPLACE INTO coverage_meta VALUES (?,?,?,?,?,?)",
                                    (REGION, south, north, west, east, "Bounded OSM extract; mapping completeness and facility access unverified."))
                    db.conn.execute("INSERT OR REPLACE INTO import_meta VALUES (?,?,?,?,?,?,?)",
                                    (REGION, extracted_at, timestamp, endpoint, QUERY, hashlib.sha256(raw).hexdigest(), ATTRIBUTION))
                if db.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Staged database integrity check failed")
                counts = dict(db.conn.execute("SELECT category,COUNT(*) FROM places WHERE region_id=? GROUP BY category", (REGION,)))
            finally:
                db.close()
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            if output.exists():
                backup_stage = Path(directory) / "previous.db"
                with sqlite3.connect(output.as_uri() + "?mode=ro", uri=True) as old:
                    with sqlite3.connect(backup_stage) as backup:
                        old.backup(backup)
                with backup_stage.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(backup_stage, output.with_suffix(".previous.db"))
                os.fsync(directory_fd)
            os.replace(staged, output)
            os.fsync(directory_fd)
        return {"counts": counts, "skipped": skipped, "bytes": output.stat().st_size,
                "extracted_at": extracted_at, "osm_timestamp": timestamp, "bounds": BOUNDS}
    finally:
        os.close(directory_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Previously downloaded Overpass JSON")
    source.add_argument("--fetch", action="store_true", help="One bounded network query; runtime never uses this")
    parser.add_argument("--endpoint", default=ENDPOINT, help="Overpass source URL recorded in provenance")
    parser.add_argument("--output", type=Path, default=Path(places.REAL_DB_FILE))
    args = parser.parse_args()
    raw = fetch(args.endpoint) if args.fetch else args.input.read_bytes()
    print(json.dumps(build_database(raw, args.output, endpoint=args.endpoint), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
