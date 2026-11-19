# SURVIVE-X Wokwi display replay

## Run

**Replace the browser firmware.** Stop the running simulation. For a new project, open the [ESP32-S3 template](https://wokwi.com/projects/new/esp32-s3). Replace the entire contents of its diagram.json tab with this folder’s diagram.json. Click inside the sketch editor, press F1, choose **Upload Firmware and Start Simulation…**, then pick **build/wokwi.ino.bin**. The template’s sketch is unused. The TFT should show Bengaluru with a GPS status and an INTERACTIVE / READY footer. The sample stays selected until input. Each accepted frame prints a JSON acknowledgement. No online project save is necessary. [Wokwi custom firmware instructions](https://docs.wokwi.com/guides/esp32)

**VS Code.** Open this folder as the workspace and run **Wokwi: Start Simulator** with an activated extension. The supplied configuration selects the same application binary tested in the browser. The ELF supplies symbols. [Wokwi project configuration](https://docs.wokwi.com/vscode/project-config)

## Controls and replay

The simulator boots in **INTERACTIVE**. Hover over the joystick and click a direction arrow or its center circle. With the joystick focused, arrow keys and Space also work. Click the separate blue button for Back. [Wokwi joystick controls](https://docs.wokwi.com/parts/wokwi-analog-joystick)

| Screen | Controls |
|---|---|
| Navigation | Center, Up or Down opens Places. Left/right steps through the previous/next flash sample. Back acknowledges the input on the root screen. |
| Places | Up/down moves the highlight. Center or Right chooses it. Back or Left cancels. |

In the serial monitor input field, enter one command per line and press Return.

| Command | Effect |
|---|---|
| `demo` or `resume` | Start **DEMO REPLAY** from the current sample. While navigation is visible, advance every two simulated seconds. |
| `pause` | Return to INTERACTIVE and hold the sample. Any physical input also takes control until replay is explicitly resumed. |
| `trace on` / `trace off` | Print a raw GPIO snapshot on enable and one diagnostic record per accepted action. Tracing defaults to off. |

The two captured places come from host exact-name searches. In UART operation, the host computes guidance and receives v1 action records. The local Places list stays usable while the receiver validates and acknowledges incoming frames. Back reveals the latest valid frame, or the loss message if the watchdog expired. Once a place is chosen, guidance requires a frame for its matching navigation destination ID. Reset the simulator to return from a negotiated UART stream to the flash samples.

## Wiring

Arduino Nano ESP32 (ESP32-S3), using the board’s Arduino pin names and default SPI bus. The diagram uses 3V3 and common ground. Digital inputs use internal pull-ups.

| Peripheral | Connections |
|---|---|
| ILI9341 | SCK D13, MOSI D11, MISO D12, CS D10, D/C D9. Rotation 1, landscape 320×240. |
| Analog joystick | HORZ A0, VERT A1, SEL D4. High axis values mean left/up. |
| Back switch | D2 to ground when pressed |
| Monitor | TX1 to RX, RX0 to TX, 115200 8N1 |

The Nano maps A0/A1 to GPIO1/2 and D4/D2 to GPIO7/5. The 12-bit ADC uses the inclusive range 1024–3072 as neutral on each axis. Buttons are active low. The first sampled action fires immediately. All controls must return to neutral for 30 simulated milliseconds to re-arm. [Wokwi joystick](https://docs.wokwi.com/parts/wokwi-analog-joystick) · [ILI9341](https://docs.wokwi.com/parts/wokwi-ili9341) · [Pushbutton](https://docs.wokwi.com/parts/wokwi-pushbutton)

## Generate, rebuild and test

Run from the parent project directory. The Python generator uses the existing NavigationState, arrow geometry and read-only places_bengaluru.db. Flash replay needs only the resulting application binary. Qwen, SQLite, Python and Pygame are all unnecessary on the controller.

```sh
.venv/bin/python -B wokwi/replay.py
.venv/bin/python -B wokwi/replay.py --stream > /tmp/survive-x-replay.jsonl
.venv/bin/python -B wokwi/tests/check.py
wokwi-cli lint --offline --warnings-as-errors wokwi
arduino-cli compile --fqbn arduino:esp32:nano_nora --output-dir wokwi/build wokwi
```

The Mac build used Arduino IDE’s CLI at `/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli`, core `arduino:esp32@2.0.18-arduino.5`, ArduinoJson 7.4.3, Adafruit ILI9341 1.7.0, GFX 1.13.0 and BusIO 1.17.4. Native receiver tests need a C++17 compiler and ArduinoJson headers. Override their path with ARDUINOJSON_SRC when necessary. The tests build in a temporary directory.

## Stage 4 v1 simulator profile

The Mac generates all metrics, statuses, strings and final polygon vertices. Flash stores 16 navigation frames (eight samples × two destinations) and two menu frames. The adapter indexes them by replay sample and selection. Sample timestamps follow virtual time, including an 11-second jump for stale data. Playback compresses each transition to two simulated seconds. Flash replay does not model a live link.

The JSONL export includes hello, raw gps/compass, destination, authoritative nav/menu and frame messages. The hello records the database and font source hashes. The adapter can receive this stream over UART, one LF-terminated record at a time. Start with hello and a fresh producer session. An accepted hello switches off flash playback until reset. Send full frames at intervals below one second for continuous display. The export is a capture for deterministic replay and can be sent as a burst. Replaying its raw timestamp gaps in real time also exercises the link watchdog. Joystick input emits action envelopes. Host interaction beyond the recorded selection demo requires a host serial action consumer.

The negotiated subset uses whole-screen reset frames, up to 16 draw chunks and one primitive per chunk. Text is printable ASCII in the pinned GFX classic 6×8 font, scale 1 or 2. Transcript numbers use degrees and metres. Each draw body contains frame_id, index and payload. Payload is the exact JSON string encoding either `["t",x,y,scale,rgb565,text]` or `["p",rgb565,[x,y],…]` with seven arrow vertices. Text extents must fit the screen, which supplies the implicit clip rectangle. Polygons use scanline fill. Unsupported draw operations fail validation. This narrow profile covers the two demo screens. It omits general fonts, arbitrary shape lists and incremental frames.

A full frame starts with frame_begin and a null base. frame_end supplies the chunk count and IEEE CRC-32 of all payload bytes, each followed by LF. The receiver validates bounds, order, frame identity, checksum and version before painting. A gap, bad frame or one second without a complete serial frame clears guidance. A complete, current reset frame restores it. Duplicates are ignored. Flash uses the same primitive validation, CRC algorithm and renderer directly on immutable records. Drawing a menu therefore leaves a partially received UART transaction intact. Each payload fits 160 bytes. UART envelopes can exceed 256 bytes. UNO Q Bridge still needs transport fragmentation and packet sizing. The TFT receives local SPI pixel writes during a clear or draw. No framebuffer crosses the host link.

## Verification and limits

- In the original sketch a deterministic 10 ms Select pulse produced no event. A 31 ms pulse opened Places and the menu survived another four seconds. Sending the plain line `pause` enabled external mode before envelope validation. A subsequent Back emitted its action but left the menu flag set. The original timer ran by default on navigation and already stopped in Places.
- The replacement application and ELF were compiled for the Nano ESP32. Program storage is 355,821 bytes and static RAM is 37,712 bytes. Use the application-only BIN with the browser template’s bootloader.
- On the rebuilt browser simulation, center read 2048 on both axes. Up/Left read 4095 on the relevant axis and Down/Right read 0. Select and Back each went from 1 to 0. All six actions appeared in the trace. Up/down moved the highlight, Select chose Bannerughatta, Back cancelled Places, loss removed guidance, and the next valid sample restored the arrow. Replay advanced, paused and resumed. A serial frame was acknowledged while the selected row remained visible through the subsequent link timeout. [Browser capture](tests/browser-controls.jpg)
- Native tests exercise the sketch with simulated GPIO, clock, UART and TFT. Coverage includes bounce, held input, manual takeover, loss, stale data, missing heading, destination changes, polygon bounds, interrupted transactions, checksums, duplicate packets, link timeout and recovery. Full host frames still use the existing v1 JSONL format.
- Offline names came from © OpenStreetMap contributors under ODbL 1.0. Capture provenance includes stable OSM identity and the database hash. [OSM attribution](https://www.openstreetmap.org/copyright)

This ESP32-S3 build uses Arduino SPI, UART, GPIO and ADC. The UNO Q needs STM32U585 firmware and Linux Bridge integration. Its pin mapping, bus timing, voltage levels, battery behavior, real GNSS quality and compass calibration require physical testing. The replay injects telemetry at the application boundary and exercises no sensor register or NMEA driver.

## Stage 5.5 profile 2 prototype

The application-state encoder is `wokwi/display_adapter.py`. It renders the existing `SURVIVEXApp` into GFX classic 6x8 drawing operations and produces v1 JSONL with `hello.body.profile=2`. Full frames declare HOME, NAVIGATION, PLACES or AI_CHAT in `frame_begin.body.screen`. The controller emits its existing v1 `action` records, which `ActionReceiver` applies to the Linux app. The optional `wokwi/serial_bridge.py` connects these methods to a POSIX serial device and runs the same app state alongside Pygame. It has been exercised with a pseudo-terminal, not with browser Wokwi or UNO Q. Profile 2 does not provide local chat state or inference in the controller.

Profile 2 permits at most 128 primitives and 160 encoded bytes per draw payload. Text is printable ASCII, with unavailable glyphs replaced by `?`. Each frame remains a reset frame, uses the existing per-draw sequence checks and CRC-32 over the screen name plus LF followed by each payload plus LF, and is staged in fixed-size buffers before painting. Text, filled rectangles with optional one-pixel borders, lines and seven-point filled polygons are accepted. The receiver preserves the displayed AI screen on GPS-loss telemetry, while navigation guidance still clears. The frame must arrive within three simulated seconds. A fresh complete frame is needed after watchdog expiry. Metadata such as AI status, focused control and scroll position is represented by the host-generated primitives, not by independent controller state.

Create a deterministic chat capture without model inference using:

```sh
.venv/bin/python -B wokwi/replay.py --chat-replay > /tmp/survivex-chat-replay.jsonl
.venv/bin/python -B wokwi/tests/check_chat.py
.venv/bin/python -B wokwi/tests/check_bridge.py
```

The JSONL capture requires a transport that sends each LF-terminated record to the receiver within its three-second frame deadline and refreshes frames for the one-second watchdog. The browser serial monitor's per-line interaction did not deliver a complete capture within that deadline during testing. The standalone browser boot continues to run the original navigation/places flash replay. Browser joystick-to-chat interaction and a live Mac-to-browser-Wokwi connection remain unverified. Browser Wokwi does not expose a tested serial device to this bridge.

The rebuilt Nano ESP32 application uses 358,457 bytes of flash and 73,824 bytes of static RAM. Measured profile 2 JSONL frames from the current app used approximately 3,376 bytes for an empty AI screen and 13,058 bytes for the AI keyboard, including envelopes and LF. At 115200 baud with 8N1, estimated UART transmission times are 0.29 and 1.13 seconds respectively, excluding rendering, processing and acknowledgments. SPI transfer time and UNO Q resource use remain unmeasured.

For a suitable POSIX serial endpoint, the prototype bridge is invoked with an explicit waypoint store:

```sh
.venv/bin/python wokwi/serial_bridge.py --port /dev/REPLACE_WITH_SERIAL_PORT --waypoints /tmp/survivex-test-waypoints.json --db places_bengaluru.db
```

Replace the port argument with a verified serial endpoint. The bridge uses 115200 baud, handles partial reads and writes without blocking the UI, and coalesces application changes while a frame is in flight. It currently sends whole-screen heartbeat frames. The pseudo-terminal test verified that joystick action envelopes moved the shared app state from HOME to AI_CHAT and caused a new frame to be sent. No physical device or browser serial endpoint was tested.

## Stage 5.6 incremental profile

The serial bridge negotiates `profile: 3` in the existing v1 hello. Profile 2 and legacy v1 full frames remain available. Profile 3 uses `frame_begin` with `mode` set to `FULL` or `DELTA`, a screen name, a new frame ID and a `base_frame_id`. FULL uses a null base. DELTA names the last acknowledged frame and must use the same screen. The receiver rejects a delta when its base is unknown, stale, or invalid after a link timeout. The CRC-32 input is the ASCII screen name, mode and decimal base ID (zero for FULL), each followed by LF, then each exact payload followed by LF. Frame ID, ordered chunk index, count and session sequence are checked separately.

Profile 3 can group ordered primitives in a payload `["b", primitive, ...]` within the existing 160-byte payload limit. Nested batches are rejected. The controller validates every primitive and checksum before painting. It sends the frame acknowledgment only after drawing. A delta replays affected regions in drawing order, starting with the current background. If an intersecting primitive cannot be clipped safely or the delta is no smaller than a complete frame, the encoder uses FULL. No RGB565 framebuffer is allocated. The receive staging and cached compatibility arrays each remain 128 × 161 bytes, plus metadata and bounded JSON parsing allocations.

The bridge keeps only one unacknowledged frame. It advances its baseline only on an ACK containing the current session and pending frame ID. While the serial port is busy it coalesces app changes into the next update. When idle it sends a small base-ID ping every 400 ms to maintain the watchdog. A resync request, receiver restart, lost ACK or lost contact triggers a new random session and a FULL refresh. The controller checks inputs between received serial batches and drawing primitives. It cannot poll during a single blocking TFT operation, so the worst-case hardware input latency still requires measurement.

On the Mac test data, JSONL bytes including envelopes and LF were approximately:

| Update | Profile 3 FULL | Profile 3 DELTA |
|---|---:|---:|
| Home | 2,975 | — |
| Navigation | 4,376 | — |
| Places search | 2,120 | — |
| Empty AI chat | 1,730 | — |
| AI chat with response | 2,000 | 1,789 |
| AI keyboard | 5,463 | — |
| Keyboard highlight movement | 5,531 | 675 |
| One character entered | 5,531 | 450 |
| AI READY to GENERATING | 1,730 | 469 |
| Navigation GPS loss | 3,751 | FULL fallback |
| Navigation GPS recovery | 4,442 | FULL fallback |

Values vary with sequence number, timestamp and displayed text. At 115200 baud and 8N1, 675 bytes alone are about 59 ms of wire time. The baud-paced pseudo-terminal test measured 63.6 ms from host receipt of the simulated action to the final delta byte. It does not include joystick sampling, receiver processing, SPI drawing, or acknowledgment. The unpaced pseudo-terminal measurement was 1.5 ms. Browser Wokwi's firmware upload failed during the Stage 5.6 attempt, so neither the full-to-delta transition nor the 150 ms end-to-end target has been measured there. UNO Q Bridge throughput and physical performance remain unverified.

Run the focused tests from the project root:

```sh
.venv/bin/python -B wokwi/tests/check.py
.venv/bin/python -B wokwi/tests/check_chat.py
.venv/bin/python -B wokwi/tests/check_delta.py
.venv/bin/python -B wokwi/tests/check_bridge.py
.venv/bin/python -B wokwi/tests/measure_latency.py
.venv/bin/python -m unittest test_stage5 test_stage4 test_navigation_safety
.venv/bin/python app.py --test
```

The final Stage 5.6 ESP32-S3 build uses 362,081 bytes of flash and 73,832 bytes of static RAM. The browser upload attempt failed in the file chooser, so the rebuilt binary has native and pseudo-terminal verification only. To run the browser's existing flash replay, open the ESP32-S3 Wokwi template, replace `diagram.json` with `wokwi/diagram.json`, then use F1 → Upload Firmware and Start Simulation to select `wokwi/build/wokwi.ino.bin`. Browser profile 3 chat interaction still requires a working transport and verification.
