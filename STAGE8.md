# Local journey recording and rescue review

Recording is off by default. Enabling it saves timestamped positions, sensor changes, destinations, navigation actions and searches. An additional opt-in saves the text of submitted AI questions and displayed replies. These files can reveal sensitive places, routines and conversations.

## Run the synthetic demonstration on macOS

From the project directory:

```sh
demo_dir=$(mktemp -d)
.venv/bin/python journey.py demo "$demo_dir/trek.json" --store "$demo_dir/trek.sqlite"
.venv/bin/python journey.py list "$demo_dir/trek.sqlite"
.venv/bin/python journey.py export "$demo_dir/trek.sqlite" synthetic-trek-01 "$demo_dir/recovered.json"
open rescue.html
open "$demo_dir"
```

In **Open journey**, choose **recovered.json**. The second Python process reopens the persisted store. The two JSON exports should be byte-identical. The synthetic session has six position samples, two destinations, a water search, a fix outage, recovery, a 120-second interval between observations and no recorded end. The last synthetic sample is **12.95560, 77.58420**. Its three route segments contain three, two and one samples respectively. Every event carries the synthetic origin. Synthetic demo stores reject device sessions and vice versa.

Select a point for its coordinates, position source, receiver time, quality, uncertainty and receipt time. Select a timeline filter to inspect searches, destinations or AI entries. **Save incident summary** exports a local text report. The coordinate plot is north-up, with a local longitude scale and no basemap. It is intended for local journeys. The time-gap threshold is 1.5 times the configured checkpoint interval. Source changes, observed position loss and logging errors also break connectors. A missing end has no recorded cause.

## Record on the device

Create a private directory owned by the operator. Start the Mac app with:

```sh
mkdir -p -m 700 "$HOME/survivex-private"
.venv/bin/python app.py --record-journey "$HOME/survivex-private/journeys.sqlite" --checkpoint 30
```

The same options can be appended to the existing Stage 6 Linux command for `unoq_app.py`. Add `--record-ai` only with consent to save question and response content. Search terms and destinations are included whenever recording is enabled. Keyboard keystrokes and unrelated files are excluded. Use Sensor Diagnostics **Left** to end recording and **Right** to start a new session under the same preferences. Launch without the recording options to disable the recorder entirely. On a configured device the footer shows recording initialization, active recording, recording off or a recording error. An error also goes to standard error.

Startup and shutdown events refer to the recording application. Unexpected power loss cannot generate a shutdown event. The recorder samples the validated navigation state at UI ticks and selected actions. It records source/health transitions immediately upon observation and samples positions every 30 seconds by default (configurable from 1 to 3600 seconds). Loss produces status records without replacement coordinates. Host observation and position sample timestamps are epoch seconds. GGA supplies UTC time of day without a date, so the recorder stores it separately. Manual and simulated samples retain their labels. Only live-source samples qualify for the device summary's first and last valid fix. AI text in the timeline is unverified.

## Recovery, integrity and retention

The operator can use the following commands while signed in to the appropriate local OS account. Substitute the actual journey ID printed by the list command.

```sh
.venv/bin/python journey.py list "$HOME/survivex-private/journeys.sqlite"
.venv/bin/python journey.py export "$HOME/survivex-private/journeys.sqlite" JOURNEY_ID incident.json
.venv/bin/python journey.py delete "$HOME/survivex-private/journeys.sqlite" JOURNEY_ID --confirm DELETE
```

Exports use a read transaction and require a new output filename. An export taken during recording contains only the committed prefix. The dashboard imports JSON read-only and checks sequence, session, origin, record shape, SHA-256 links, count and final digest before displaying records. A failed import clears the view. HTML content from records is displayed as text. The dashboard has no server or external tile, font or script dependency. Reading the file requires access to the OS account or a copy deliberately provided by the owner.

The SQLite schema uses application ID SXJ1, user version 1 and a primary index on journey and sequence. The background writer uses a 256-event queue, transactions of up to 64 records, SQLite rollback journaling and FULL synchronization. Each transaction commits records and the session head together. Startup checks database structure. The UI does no database writes on the recording path. Abrupt termination can lose queued events and uncommitted transactions. Reopening starts a new session and preserves previously committed records.

No automatic expiry or eviction is configured. The database budget is 64 MiB of SQLite pages, plus temporary journal overhead. Each session has a limit of 20,000 events or 12 MiB of encoded record text. Below 16 MiB of filesystem headroom the writer stops and attempts a terminal error record. A full filesystem, full queue, corrupt database or session limit stops recording with a visible error. Some failures prevent writing their own error record. Stop and restart recording after resolving the cause. Export before deleting if retention is desired. Deletion requires an idle writer and affects the selected session. SQLite free pages, filesystem snapshots, backups, flash wear leveling and prior exports can retain copies.

New database and export files use owner-only permissions. They contain plaintext. Possession of a device is insufficient authorization to recover someone's journey. Use documented owner consent or the organization's authorized recovery procedure. A future deployment could encrypt the data volume and release a recovery key after credential verification and recorded authorization. This implementation relies on local OS permissions. An unkeyed hash chain detects accidental changes within an export. Someone who can rewrite the records can recompute the chain, fabricate a source or supply a consistent prefix. Receiver time and location remain unauthenticated. Neither the data nor gaps establish an incident site, intent or cause.

## Verification

```sh
.venv/bin/python -m unittest discover
node test_rescue.mjs
```

The browser regression uses installed Chrome, a temporary profile, a local debugging pipe and offline network emulation. Set `CHROME_BIN` for another installation. Node and Chrome are test tools only. The dashboard runs directly from its HTML file.
