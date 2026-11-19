# Stage 4 — Offline places and display boundary

## Runtime and data ownership

Run the application from the project directory.

```sh
.venv/bin/python -B app.py
.venv/bin/python -B app.py --db places.db
.venv/bin/python -B places.py --db places_bengaluru.db --search Bannerghatta
.venv/bin/python -B navigation.py --test
.venv/bin/python -B places.py --test
.venv/bin/python -B app.py --test
.venv/bin/python -B -m unittest -v test_navigation_safety test_stage4
```

The first launch command selects `places_bengaluru.db` when installed. The second explicitly selects the preserved Rainier fixtures. Existing waypoint data stays in `waypoints.json`. Runtime database connections are read-only. Search runs in one background worker with a separate SQLite connection. The UI retains one active query and one replaceable pending request. A generation number discards superseded results. Position loss clears reference-dependent results, while name searches remain available.

The navigation and places modules use the Python standard library. Application state, actions and primitive drawing contracts can be imported without Pygame. Pygame is needed for the desktop adapter. Qwen runs independently.

The original `places.db` remains the recoverable fixture dataset. Real OSM records occupy a separate file. The importer validates a staged database, retains the prior deployed database as `places_bengaluru.previous.db`, then atomically replaces the deployed path. A directory lock serializes publishers. Files and directory metadata are synced. Unknown non-OSM output is rejected. Waypoints are outside the database replacement transaction.

Restart the app after publishing an updated database so every read-only connection uses the same new snapshot. Existing open connections can retain the prior SQLite inode. Saved waypoints retain their original position and provenance when the source changes or deletes an element. A changed position can create another historical waypoint rather than moving a saved destination silently.

## Extraction and representation

The selected bounds are south 12.70°, west 77.45°, north 13.05°, east 77.75°. They include central Bengaluru, southern suburbs and the Bannerghatta area. This is a category-filtered vector extract. Coverage describes the extraction extent. Mapping completeness, facility operation, access and emergency capability remain unverified.

`import_places.py` contains the exact bounded Overpass query and category policy. The query selects settlements, hospitals, clinics, police, ranger/forest offices, mapped shelters and water points. Categories with no returned records remain empty. Bus shelters retain their original tags. A water category conveys a mapped feature and carries no potability guarantee.

Nodes retain their supplied coordinates. Ways and relations use Overpass's reported bounding-box center, recorded as `bounding_box_center`. Such a center can fall outside the actual footprint and is unsuitable for inferring an entrance. Centers outside the requested extent are skipped. Every accepted element retains its type, ID, original name, tags, extraction time and region. Unnamed features use an explicit category plus OSM identity as a display label. Their original name remains empty.

Stable integer place IDs encode `osm_id * 4 + type_code`, where node, way and relation use 0, 1 and 2. A unique index also covers the original element type and ID. Duplicate names remain separate places. English, Kannada, alternate, official and short names are indexed aliases when supplied by OSM. Original names are preserved.

Exact matches precede prefix matches and substring matches before the result limit. Prefix queries use B-tree ranges. Literal substring queries use `instr`, so user-entered percent and underscore characters remain literal. Substring search scans candidate text. Nearby search uses a bounding box, spherical distance and a bounded result heap. The UI reports EMPTY DATABASE, UNKNOWN COVERAGE, OUTSIDE COVERAGE and NO MATCH explicitly. Unknown position produces unknown positional coverage even when an extract has defined bounds.

Import or update using either a bounded fetch or a previously saved response.

```sh
.venv/bin/python -B import_places.py --fetch
.venv/bin/python -B import_places.py --input /absolute/path/overpass.json
```

The fetch makes one request with a 60-second server timeout, 128 MiB server memory allowance and 32 MiB response-size cap. HTTP failures and Overpass error remarks stop publication. Review server policy before repeated updates. Source downloads stay outside the embedded deployment. The database records endpoint, query, source timestamp, extraction timestamp and response SHA-256.

Attribution is **© OpenStreetMap contributors**, licensed under **ODbL 1.0**. The UI provides attribution on search and OSM detail pages. Deployment and redistribution must retain attribution and applicable database licensing terms. [OSM copyright and license](https://www.openstreetmap.org/copyright)

The endpoint is a public Overpass service. The selected region and category filters are intentionally bounded. Public services have no application availability guarantee. [OSM public instance list](https://wiki.openstreetmap.org/wiki/Overpass_API) · [Overpass usage guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html)

## Installed extract and host measurements

Imported on 18 November 2026 at 11:46:23 UTC from the VK Maps public Overpass endpoint. OSM source timestamp is 2026-11-18 11:43:18 UTC. The main FOSSGIS endpoint returned HTTP failures, and Private.coffee timed out. The VK endpoint returned an explicit memory-budget error at 32 MiB, then completed the identical bounded query with 128 MiB.

The raw response was 1,057,464 bytes and stayed in temporary storage. Its SHA-256 is `25bcdee0ea5738fa52640b7237a9507b6b991bce0576169db11b803cee5dcd98`. Independent comparison checked all 3,553 imported coordinates, original names and original tags against that response. Six records with centers outside the import bounds were skipped.

| Category | Imported records |
|---|---|
| Settlements | 1,025 |
| Hospitals | 1,033 |
| Clinics | 918 |
| Police | 130 |
| Ranger/forest offices | 0 |
| Shelters | 291 |
| Mapped water sources | 156 |

Element types are 3,054 nodes, 476 ways and 23 relations. There are 163 duplicated display-name groups. These are retained as distinct OSM identities. The real database contains zero synthetic-fixture rows.

The original fixture DB remains 40,960 bytes. The new real DB is 2,465,792 bytes, approximately 2.35 MiB. Waypoint and fixture database hashes matched before and after testing.

Representative source records include Bengaluru, node 3401391999 at 12.9767936, 77.590082; Bannerughatta, node 1782880841 at 12.813337, 77.5789509; and Fortis Hospitals, Bannerghatta Road, way 44091443 with supplied center 12.8947511, 77.5985816. OSM spells the settlement “Bannerughatta”. Searching “Banner” finds spelling variants without inventing aliases.

Mac Python 3.12 measurements used one read-only connection, one first call and 100 repeated calls per query. Results are host warm-cache measurements.

| Query | First call ms | Median ms | P95 ms |
|---|---|---|---|
| Exact Bannerughatta | 1.120 | 0.298 | 0.301 |
| Prefix Beng | 0.867 | 0.839 | 0.851 |
| Substring ghatta | 0.439 | 0.383 | 0.412 |
| Nearby 25 km | 26.878 | 26.063 | 26.315 |
| Nearby hospitals 25 km | 7.928 | 7.869 | 8.102 |

Nearby origin was 12.813337, 77.5789509. The verification/query process reached 38.56 MiB peak RSS, including parsed source JSON. The Pygame UI render process reached 62.45 MiB, including a temporary nine-screen contact sheet. These measurements do not isolate target runtime memory.

## 320×240 interaction and rendering

Four directions, SELECT and BACK operate every place-search step. On the query field, SELECT opens the keyboard when empty. LEFT or RIGHT opens it for editing an existing query. The 8-column keyboard includes A–Z, 0–9, SPACE, DEL, CLR and FIND. Direct desktop typing remains supported. Results scroll. SELECT opens details. LEFT/RIGHT page through the full name, OSM identity, coordinates, category, date, region and attribution. UP/DOWN select navigation, save or back.

Long names are width-fitted in result rows and fully available across detail pages. Rendering uses a 320×240 surface. Text metrics belong to the display adapter. The Mac adapter selects a system Kannada font for Kannada text when available. A target font pack and Indic shaping must be selected and visually verified before physical deployment. The joystick keyboard currently enters Latin letters and digits; mapped English aliases support English searches where present.

The main loop redraws on input, completed searches and the one-second clock tick. The logical frame can still be a full redraw on those changes. Partial SPI dirty-region scheduling remains a transport/backend concern.

The display boundary supports text, rectangles, lines, circles and polygons. Highlights are bordered rectangles. Arrow geometry is calculated once by the Python display base class and emitted as primitives. Hardware and simulator renderers receive rotated polygon points. They need no navigation or arrow-angle mathematics. Pygame and the in-memory test adapter implement the same primitive interface.

## UNO Q display architecture

Use the STM32U585 to operate the TFT and joystick. Linux owns search, navigation, persistence, UI state and text layout. Arduino documents MCU SPI and a bidirectional Bridge RPC API. The documented SPI interfaces are associated with the MCU. The inspected documentation does not establish a supported direct Linux-to-this-TFT SPI device path. Direct Linux control would require a separately verified exposed bus, kernel support and driver. No Linux device name is assumed. [UNO Q user manual](https://docs.arduino.cc/tutorials/uno-q/user-manual/) · [UNO Q pinout](https://docs.arduino.cc/resources/pinouts/ABX00162-full-pinout.pdf)

Maker/MCU signal banks operate at 3.3 V. Processor banks operate at 1.8 V. A header's 5 V supply does not establish 5 V signal compatibility. Exact TFT module supply, logic levels, backlight circuit, reset/DC requirements, controller, SPI mode and maximum clock need its datasheet. Physical wiring and level shifting must follow the actual board revision and module. [UNO Q power specifications](https://docs.arduino.cc/tutorials/uno-q/power-specification/)

Bridge is a documented RPC transport. The receiver should enqueue a compact update and draw in a safe main-loop context. Keep RPC handlers short and obey the documented callback restrictions. The published API lists a 256-byte sketch-call limit and a 115200-baud internal link; verify these against the installed firmware/router version. Logical frames below use small chunks to accommodate that limit. Do not open the Bridge-owned serial transport directly. [Arduino Bridge API](https://docs.arduino.cc/software/app-lab/bridge/bridge-api)

An RGB565 full frame requires 153,600 bytes. At 30 frames/s, pixel payload alone requires 4.608 MB/s. At an illustrative 8 MHz SPI clock, one full frame takes at least 153.6 ms. At 24 MHz it takes at least 51.2 ms. These estimates omit commands, gaps and software overhead. Small primitive updates avoid sending the framebuffer across Bridge. A 320×16 RGB565 strip buffer needs 10,240 bytes. Streaming into controller RAM can avoid a full MCU framebuffer. Fonts, shaping tables, queues, stack, Bridge and driver memory must still be budgeted. No UNO Q throughput, latency, power or thermal measurements have been made in this stage.

## Version 1 communication contract

This section specifies the future boundary. Transport encoding, firmware, Bridge calls and the Wokwi project remain subsequent work.

Logical messages use UTF-8 JSON for capture/replay. The future wire transport can use MessagePack with the same keys, types and semantics. The envelope is `{"v":1,"session":"id","seq":12,"t_ms":2500,"kind":"gps","body":{...}}`. A session identifies one producer boot or one deterministic replay. Sequence numbers increase per producer. Receivers discard duplicates and out-of-order messages. Unsupported versions produce a protocol error and no guidance update.

All values use JSON finite numbers. Unavailable values are `null`. Coordinates use WGS84 decimal degrees, headings/bearings use degrees, distance and accuracy use metres. Clock timestamps are session-relative integer milliseconds. Transport reception never rewrites the sample timestamp.

Message bodies are as follows.

| kind | Body fields and ownership |
|---|---|
| `hello` | `width=320`, `height=240`, color format RGB565, font-pack ID/hash, protocol limits and supported primitive list. Both sides negotiate the same layout/font metrics. |
| `gps` | `mode` as SIMULATED, MANUAL or LIVE_GNSS, `sample_ms`, `lat`, `lon`, `altitude_m`, `fix`, `accuracy_m`, `hdop`, `satellites`. Linux constructs GPSReading and runs existing validation. |
| `compass` | `sample_ms`, `magnetic_deg`, `valid`, `calibrated`, `source`. Linux validates freshness/calibration and uses an explicitly supplied declination. |
| `action` | `action` as UP, DOWN, LEFT, RIGHT, SELECT or BACK. Optional CHAR_INPUT is for host/replay input. Receiver dispatches the existing Action handler. |
| `destination` | Host/replay command with stable identity, name, coordinates, source, test-data flag and provenance. Linux validates and selects through the waypoint/application API. |
| `nav` | Linux output with source mode, position status, destination identity/name/source/test-data, `distance_m`, `bearing_deg`, `true_heading_deg`, `turn_angle_deg`, arrow/status labels and sample age. Receiver displays supplied results. |
| `menu` | Linux output with screen enum name, selected item identity/index, page, search-busy flag and outcome/coverage strings. |
| `frame_begin` | `frame_id`, `base_frame_id` or null for a full reset, and expected chunk count. |
| `draw` | `frame_id`, chunk index and ordered primitives. Each text operation includes position, font ID, color, clip rectangle and UTF-8 text. Rectangles include fill/border. Lines include endpoints/width. Circles include center/radius. Polygons include final integer points, fill and border. |
| `frame_end` | `frame_id`, chunk count and checksum. Receiver accepts a frame only when all chunks validate. |
| `ack` / `resync` | Applied sequence/frame ID or request for a complete frame after a gap, restart or checksum failure. |

Logical names or text can exceed one wire message. Transport framing fragments messages into payloads of at most 160 bytes, reserving space for version/session/sequence/chunk metadata and RPC overhead. Validate actual serialized packet size below the installed Bridge limit. Reassemble at most one bounded frame at a time. Reject oversized strings, coordinates outside the screen, excessive polygon points and unknown primitives.

Keep a bounded latest-frame queue and prioritize sensor-loss state over cosmetic redraws. A receiver must not retain an active arrow indefinitely after link loss. After a negotiated 1-second communication watchdog expires, draw a link-loss overlay and invalidate guidance. Resume only after a complete current frame. The watchdog concerns communication health; Linux still owns sensor sample-age validation. Atomic visual presentation depends on controller capabilities and buffering; chunk validation alone cannot guarantee tear-free physical presentation.

### Deterministic replay

A replay runner advances a virtual clock using `t_ms`. Convert sample times into the same positive time domain supplied as `current_time` to NavigationState.from_readings. Preserve age when messages are delayed. A deliberate stale case advances the clock without new sample data. A future adapter may add a clock injection point to the application; the navigation engine already accepts explicit time.

A minimum trace contains a valid GPS/compass sample and destination, a heading change, GPS fix loss, recovery, a clock advance beyond 10 seconds, stale-heading age beyond 5 seconds, invalid coordinates, another destination, then full recovery. Run the same inputs through the Python navigation engine used by physical input adapters. Compare status, optional distance/bearing/turn values, menu state and emitted primitives. Record a fixed DB snapshot hash, font hash and source mode with each trace.

## Subsequent hardware simulation stage

Use a representative supported board with a virtual ILI9341 TFT in landscape mode, a virtual analog joystick and a separate BACK button. The ILI9341 model is 240×320 and uses SPI. The built-in ILI9341 model omits reset and backlight pins. Support for the eventual controller must be checked when its model is known. Controller initialization and virtual SPI transactions belong in the future firmware renderer. [Wokwi ILI9341](https://docs.wokwi.com/parts/wokwi-ili9341) · [Wokwi supported hardware](https://docs.wokwi.com/getting-started/supported-hardware)

The joystick supplies two analog axes and a select button. Apply dead zones, debounce and repeat in the simulator firmware, then emit the six application actions. Wokwi documents MCU families including ESP32, RP2040 and selected STM32 parts. Its supported list does not include the UNO Q Linux/STM32U585 combination. Representative-board pin assignments and firmware remain specific to that simulator board. [Wokwi joystick](https://docs.wokwi.com/parts/wokwi-analog-joystick) · [Supported microcontrollers](https://docs.wokwi.com/getting-started/supported-hardware)

The inspected built-in sensor list has no GNSS receiver or magnetometer model. MPU6050 represents accelerometer/gyro data. Use recorded telemetry through the versioned protocol first. Later, if sensor-protocol validation is required, Wokwi's beta custom-chip API provides UART and I²C interfaces for a chosen GNSS sentence producer or magnetometer register model. Define those against the eventual sensor datasheets. [Wokwi custom chips](https://docs.wokwi.com/chips-api/getting-started)

The simulator firmware consumes the same versioned primitive frames intended for the physical adapter. Python continues to calculate navigation. Exercise joystick-only search, long text, selection, partial-frame rejection, resync, display loss warnings, GPS loss, stale data, heading rotation, destination changes and recovery.

Passing these tests establishes behavior of the representative simulator components and protocol implementation. Physical UNO Q validation still must cover Bridge/router compatibility, electrical wiring, voltage levels, battery/charging/shutdown, real sensor noise/calibration, TFT readability, SPI throughput, memory, CPU load and thermal limits.

## Verification and remaining limits

The final test run passed the existing navigation suite (47 assertions), places suite (31 assertions), application suite (115 assertions, plus its nested navigation run), all 12 Stage 3.5 regressions and all 11 Stage 4 regressions. Tests use temporary data. Runtime lookup was exercised with socket construction and connection attempts rejected by the test harness. This verifies offline execution without changing the host network configuration.

Stage 4 tests cover real installed records, source identity and tags, bounded imports, duplicate OSM identities, exact/prefix/substring ranking, aliases, wildcard literals, indexed prefix planning, nearby filtering/order, coverage states, atomic update failures, recoverable backups, waypoint provenance after update, joystick-only search, delayed asynchronous search during position loss, deterministic sensor replay and long-name pagination. Pygame rendered every screen at native 320×240 in the dummy video driver. Contact sheets were visually inspected in temporary storage.

Physical TFT legibility, font shaping and bus behavior remain unverified. The fallback font set depends on installed system fonts. OSM addresses and names may be incomplete, misspelled or outdated. Ways and relations have representative centers without guaranteed entrances. The zero ranger-office count reflects the selected tags and source snapshot. Search result caps are 30 names and 25 nearby places in the UI. The importer currently configures one bounded region; expansion requires an explicit extraction plan. No firmware, physical driver or Wokwi circuit has been created.


## Stage 5 AI display boundary

The Linux application owns Qwen inference, chat state and the joystick keyboard. The MCU receives bounded drawing primitives and input actions, with no access to model state or navigation authority from the model. Chat uses the existing logical v1 text and rectangle operations. It needs a negotiated font with matching measurements and support for printable UTF-8, clipping, rectangles and sufficiently many bounded chunks. The current Wokwi receiver implements an ASCII-only text and polygon subset with at most 16 primitives per frame, so a chat-capable receiver needs expanded primitives, bounded text/chunk capacity and possibly incremental updates. Maintain sequence, checksum, watchdog and resynchronization rules. No STM32 Bridge implementation is included.

The Mac application starts a local llama-server per question using the model's own chat template. It tokenizes the fully templated prompt with the same model and reserves 128 output tokens plus 16 tokens in a 1024-token context. The server is CPU-only with four threads and a single slot. Cancel terminates the server process. This avoids a resident model between questions at the cost of reloading it for each request. The model and llama-server paths can be set with `SURVIVEX_MODEL` and `SURVIVEX_LLAMA_SERVER`. Install a compatible ARM64 llama.cpp build and supply board-specific paths for UNO Q. Model load, RAM, thermal behavior, and display transport remain to be measured on the board.

On Mac launch `.venv/bin/python app.py`. In Offline AI, type on the keyboard, move focus with left/right, and press Enter on SEND. KEYS opens the joystick alphabet and digits. Up/down scrolls responses, CANCEL stops inference, CLEAR resets chat and `/reset` submitted via SEND also resets it. Back leaves the screen while a request continues. Responses are unverified. User-only context holds up to three successful prompts. A new explicit absence constraint or common correction clears older prompt context to reduce stale facts, but automatic detection of all superseded facts is not guaranteed.


### Profile 2 prototype

The Wokwi adapter prototype extends the v1 envelope with `profile=2` in hello and a validated `screen` in frame_begin. It accepts up to 128 primitives per frame with 160-byte draw payloads and retains the v1 sequence and payload checksum scheme. Linux formats bounded chat text and interaction state into ASCII GFX operations. The MCU forwards joystick actions and does not own the conversation. The profile currently sends full reset frames and includes a POSIX serial bridge tested on a pseudo-terminal and still needs changed-region updates and a verified STM32 Bridge implementation. Browser replay and physical hardware behavior have not been verified. See `wokwi/README.md` for test and build details.

### Incremental profile 3

The Wokwi serial prototype negotiates profile 3 while keeping the v1 envelope. FULL initializes a screen and DELTA requires the receiver's current acknowledged frame and screen as its base. CRC-32 covers the screen, mode, decimal base and the ordered bounded draw payloads. Bounded batches reduce repeated JSON envelopes. The Linux encoder redraws affected regions from the current drawing stack and falls back to a full frame when it cannot prove that region redraw is safe. The POSIX bridge waits for the frame ACK before advancing its baseline and starts a new session with a FULL frame after resync, timeout or receiver restart. A base-ID heartbeat maintains the watchdog. See `wokwi/README.md` for limits, measurements and unverified hardware requirements.
