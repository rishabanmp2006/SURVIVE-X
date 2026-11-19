# Stage 6 — UNO Q deployment preparation

## Status

The Linux host, profile 3 RPC adapter, and recorded-sensor ingestion are implemented and tested on Mac with a mock Bridge. No UNO Q or physical sensor was used. The target sketch is **transport scaffolding only**. It assembles bounded fragments and deliberately does not parse, draw, read inputs, or acknowledge display frames. The existing Wokwi receiver is the executable protocol reference. Moving its parser and renderer behind a controller-independent interface, choosing a verified Zephyr-compatible TFT library, and compiling against the installed UNO Q core are still required before this can operate a real display.

## Architecture and evidence

The QRB2210 runs Debian ARM64 and owns SQLite, navigation, application state, Qwen and frame generation. STM32U585 runs Arduino on Zephyr and is the proposed owner of SPI drawing, joystick and Back. The sensor reader currently runs on Linux only if an explicit POSIX NMEA device is supplied. A magnetometer driver must deliver calibrated, board-oriented magnetic headings; no device, address or calibration is assumed. Arduino documents the two processors and 3.3 V MCU headers. The JCTL MPU interfaces use 1.8 V. [Arduino UNO Q datasheet](https://docs.arduino.cc/resources/datasheets/ABX00162-datasheet.pdf)

The Arduino Router provides MessagePack-RPC between the processors. `Serial1` and `/dev/ttyHS1` belong to the router and are not application serial ports. `unoq_bridge.py` uses the official `arduino.router_bridge.Bridge` API from a worker thread. It does not open either Bridge-owned UART. The Python handler only enqueues events and makes no nested RPC calls. Arduino specifies a 256-byte maximum for a sketch-originated call. The prototype fragments ASCII protocol records into 80-byte strings, with explicit part index and count. Actual MessagePack sizes and API compatibility must be measured against the installed board software. [Arduino Bridge API](https://docs.arduino.cc/software/app-lab/bridge/bridge-api) · [Arduino Router Bridge Python](https://github.com/arduino/arduino-router-bridge-py)

The mock exercises hello, bounded RPC fragmentation, profile 3 FULL/DELTA, CRC, ACK, event forwarding, interruption and new-session recovery. The real MCU must enforce the existing receiver's complete profile 3 validation, frame staging, screen/base checks, CRC and watchdog before emitting an ACK. The scaffold accepts fragments through `sx_part(index, count, fragment)`. It proposes `sx_event(kind, session, frame_id, action, sequence)` in the other direction. These signatures and callback scheduling require target verification. A successful fragment RPC means only that the scaffold accepted bytes. It is not a display-frame ACK.

## Connection planning

| Signal or subsystem | Verified board capability | Proposed connection status |
|---|---|---|
| SPI | Arduino documents D10 SS, D11 MOSI, D12 MISO and D13 SCK. JSPI and JDIGITAL SPI share SPI2 and cannot be used simultaneously as SPI. | Candidate bus only. TFT controller, power, readback and actual wiring must be confirmed. |
| TFT CS, D/C, reset, backlight | MCU GPIO operates in the 3.3 V domain. | Pins and electrical drive remain unassigned until the display module is identified. |
| Joystick X/Y | A0–A5 support ADC. ADC resolution is configurable. A0/A1 are not 5 V tolerant. | Candidate analog inputs, no final assignment. Confirm joystick supply and maximum output, then measure center/endpoints. |
| Joystick switch and Back | MCU digital GPIO is available. | Pins, polarity, and required external pull-ups remain unassigned. |
| GNSS | Hardware integration depends on the selected module and interface. | Linux NMEA serial path and baud are explicit runtime parameters. MCU acquisition via Bridge is not implemented. |
| Compass | I2C and GPIO capabilities are documented. | Sensor, address, bus, calibration, orientation and tilt handling are unknown. |
| Battery, charging and shutdown | Board power domains and approved input options are documented separately. | Entire power path remains provisional until battery and protection components are identified. |

All analog pins used as ADC inputs must remain within the documented reference range. The board's 5 V supply pin is a power output and does not establish the TFT's logic-voltage compatibility. Do not transfer ESP32-S3 pin assignments or ADC thresholds to UNO Q. [UNO Q user manual](https://docs.arduino.cc/tutorials/uno-q/user-manual) · [UNO Q pinout](https://docs.arduino.cc/resources/pinouts/ABX00162-full-pinout.pdf) · [UNO Q power specifications](https://docs.arduino.cc/tutorials/uno-q/power-specification/)

## Linux preparation and launch

Use an ARM64 build of `llama-server` compatible with the existing GGUF. Do not copy the Mac executable to Linux. Provision the Python application dependencies and Arduino's `arduino-router-bridge` package on the target through the approved board workflow. Provision the existing GGUF and read-only places DB through the operator's chosen transfer method. The application reads no model on startup and maintains at most one inference process per request. The model is reloaded per request and the navigation loop continues if inference fails. The existing Mac `app.py` launcher is unchanged.

The board image, target library versions and final file locations must be confirmed first. With those values chosen, check the magic bytes and required CLI options without loading the model:

```sh
python3 unoq_app.py --model /path/to/model.gguf --llama-server /path/to/arm64/llama-server \\
  --database /path/to/places.db --waypoints /path/to/writable/waypoints.json --check
```

After MCU protocol, drivers and hardware have been implemented and verified, remove `--check` to run the headless Linux application. Add `--declination DEGREES` only for a verified local east-positive value. An optional `--gnss-port /verified/device --gnss-baud 9600` enables the Linux NMEA reader. No GNSS port is assumed. With no source, LIVE_GNSS remains no-fix and compass unavailable. Without verified declination the true heading and arrow remain unavailable. The target sketch is incomplete, so this launch does not yet produce an operational TFT.

Stop with SIGINT or SIGTERM. The host closes the sensor reader, RPC worker, model worker and read-only DB. Check `systemctl status arduino-router` and `journalctl -u arduino-router` on the board if the RPC service is unavailable. Select and test a supervisor and restart policy only after the actual board application is validated. [UNO Q Router troubleshooting](https://docs.arduino.cc/tutorials/uno-q/routerbridge-multilanguage/)

## Sensors and data limits

`hardware_sensors.py` consumes bounded, checksummed NMEA GGA sentences, including fix quality, coordinates, satellites, altitude and HDOP. It timestamps them at host receipt. It does not derive metre accuracy from HDOP or use GNSS time to establish sample age. An upstream device repeating old sentences cannot be detected from receipt timestamps alone. Unsupported, malformed and checksum-failing sentences do not refresh the last valid sample. A no-fix GGA and serial disconnection immediately clear the fix. Existing `navigation.py` rejects stale and invalid positions. The optional POSIX reader opens only the specified GNSS device and reconnects after failures.

Compass input requires an explicitly calibrated magnetic heading and a sample timestamp. Unknown sensor orientation, hard/soft iron correction, tilt compensation and declination are never inferred. No live magnetometer driver or MCU sensor message is implemented. These interfaces are tested with recorded samples and simulated disconnects, not real modules.

## Hardware test and measurement plan

Use temporary waypoint files and a known read-only database snapshot for commissioning. Record board image, core, Bridge/router version, model checksum, screen module and sensor firmware alongside every result.

1. Record `free -m`, `/proc/meminfo`, eMMC free space, processor temperature and frequency after boot, then after navigation is steady. Record process and system peak memory during one bounded AI request and during concurrent navigation.
2. With an instrumented but otherwise idle system, measure action sampling, RPC latency, frame ACK, SPI drawing time and visible joystick-to-highlight latency. Repeat for full and delta frames and record distributions, not only a best result. Exercise dropped messages, router restart and MCU reset.
3. Compare GNSS fix, checksum errors, age, HDOP and disconnect recovery against a recorded reference. Validate compass calibration and device orientation, and confirm true heading with a verified declination. Test absence and stale-data arrow suppression before allowing guidance.
4. Measure AI startup, first answer, generation rate, CPU frequency and temperature under concurrent navigation. Do not infer UNO Q performance from Mac results.
5. With the identified power components and manufacturer's specifications, characterize input current, battery cutoff, safe shutdown and restart under load. Check eMMC and waypoint persistence across controlled shutdown and recovery. Do not perform battery wiring based on this document.

## First power-on checklist

- Confirm board revision, peripherals, supply ratings, signal levels and current budgets from their datasheets. Leave unverified peripherals disconnected.
- Power the UNO Q using a documented board supply and confirm normal boot and router service. Inspect voltage domains before choosing any GPIO connection.
- Compile and run an official minimal Bridge example in App Lab. Then complete and test the profile 3 MCU receiver and selected TFT driver with explicit pin configuration.
- Confirm the display and controls with deterministic frames, watchdog loss, restart and full/delta recovery. Test sensors and calibration separately.
- Check model and DB paths, backup the intended writable waypoint file, run `--check`, then measure navigation and AI resource use before any operational use.
