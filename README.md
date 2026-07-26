# WDGoWars Wardriver — Hak5 WiFi Pineapple Pager payload

Native payload for the [Hak5 WiFi Pineapple Pager](https://docs.hak5.org/wifi-pineapple-pager/)
that turns it into an offline-first **WiFi + BLE wardriver** feeding the
[wdgwars.pl](https://wdgwars.pl) ARG / wardriving game.

<p align="center">
  <img src="docs/screenshots/main-menu.jpg" alt="Main menu" width="48%">
  <img src="docs/screenshots/live-hud.jpg" alt="Live scan HUD" width="48%">
</p>
<p align="center">
  <img src="docs/screenshots/config.jpg" alt="Config screen" width="48%">
  <img src="docs/screenshots/test-connection.jpg" alt="TEST CONNECTION response" width="48%">
</p>

- **Two WiFi capture backends** — passive monitor-mode beacon capture when a
  monitor interface is available, `iw dev … scan` with rotating per-band
  passes otherwise. The interface is configurable and auto-detected.
- BLE LE capture via `bluetoothctl` under a pty (real async `[CHG] RSSI` events)
- GPS from a **u-blox 7** USB stick via `gpsd` — 3D fix required before scan starts,
  and every observation is geo-tagged with the position that was true **when it
  was heard**, not when it was written
- **Movement-aware deduplication** — a row is written for a new BSSID, after
  you have moved far enough for the sighting to add information, or when the
  signal got materially stronger
- Stores everything as standard **WigleWifi-1.6** CSV in `/mmc/root/loot/wdgwars/sessions/`
- Manual **SYNC NOW** uploads pending CSVs to `POST /api/upload-csv`, automatically
  switching to the async `POST /api/v2/upload-csv` queue (gzipped) for large files
- **UPLOAD LOG** screen reads `GET /api/upload-history` — see the server's own
  `imported` / `captured` / `no_gps` / `bad_rows` counts on the device
- "NEW BADGE" flash after sync, including the 🍍 `hak5_pager_user` *Hak5 Pager Op*
- On-pager UI in cyan cyberpunk style — no laptop, no web dashboard
- **App handoff** to Loki / PagerGotchi / WiFMan / Bjorn without round-tripping through
  the system pager service (uses the `exit 42 + data/.next_payload` protocol)
- **Idle screen dim** at 20 s (configurable) to keep the device from cooking itself

```
wdgwars/
├── payload.sh          # pager manifest + launcher (RINGTONE/LOG/WAIT_FOR_INPUT)
├── bootstrap.sh        # one-time: fetches pagerctl from wifman, opkg deps
├── config.json         # api_key, gps, scan/dedup tuning, upload mode, idle settings
├── handoff.py          # APP_HANDOFF launcher discovery / exit(42) trigger
├── wdgwars.py          # entry point + menu loop (App class)
├── launch_*.sh         # jump-to launchers for the 4 peer payloads
├── lib/                # pagerctl.py + libpagerctl.so (fetched by bootstrap)
├── ui/                 # theme, splash, menu, status HUD, dialog, hex keyboard, idle
├── scanners/
│   ├── iface.py        # `iw dev` enumeration + capture-source selection
│   ├── wifi.py         # `iw scan` backend: band rotation, cache-age filtering
│   ├── monitor.py      # monitor-mode backend: radiotap + 802.11 IE decoding
│   ├── wigle_auth.py   # shared AuthMode bracket-string builder
│   ├── ble.py          # bluetoothctl over pty
│   └── gps.py          # gpsd client + rolling position history
├── storage/            # WigleWifi-1.6 CSV writer + movement-aware deduper
└── uploader/           # multipart POST (v1 sync / v2 async queue) + history
```

## Install on the pager

**From your laptop** (not from an SSH session on the pager):

1. Clone + copy the `wdgwars/` folder into the payloads tree:

   ```sh
   git clone https://github.com/LOCOSP/pineapple_pager_wdgwars
   cd pineapple_pager_wdgwars
   scp -r wdgwars root@172.16.52.1:/mmc/root/payloads/user/reconnaissance/wdgwars
   ```

2. SSH in and run the bootstrap **once**:

   ```sh
   ssh root@172.16.52.1
   cd /mmc/root/payloads/user/reconnaissance/wdgwars
   sh bootstrap.sh
   ```

   `bootstrap.sh` does everything in one shot: copies `pagerctl.py` +
   `libpagerctl.so` from the bundled `wifman` payload, installs `iw` /
   `bluez-utils` / `kmod-usb-acm` / `gpsd` / `tcpdump-mini` via `opkg`
   (best-effort — `tcpdump-mini` is only needed for the monitor-mode
   backend), creates the loot dir, **and pushes the reverse JUMP TO launcher
   (`launch_wdgwars.sh`) into every peer payload it finds installed**
   (Loki / PagerGotchi / WiFMan / Bjorn). No manual scp loop needed.

   > **Windows / CRLF note.** If bootstrap (or `payload.sh`) fails with
   > `set: Illegal option -` or `: not foundh`, the shell scripts picked
   > up `\r\n` line endings somewhere in transit (usual cause: unpacking
   > the zip on Windows, or an SCP client running ASCII-mode translation).
   > The repo's `.gitattributes` forces LF on text files so a fresh
   > `git clone` is immune — but if you already hit it, on the pager run
   > once:
   >
   > ```sh
   > sed -i 's/\r$//' /mmc/root/payloads/user/reconnaissance/wdgwars/*.sh
   > ```
   >
   > …and re-run the bootstrap. Pager firmware 1.0.8+ ships `dos2unix`,
   > which works equally well: `dos2unix wdgwars/*.sh`.

3. Grab an API key at <https://wdgwars.pl/profile> → "Generate API key", then
   either edit `config.json` on the pager (`api_key` field) or use
   **CONFIG → EDIT API KEY** on the device (hex on-screen keyboard).

4. Plug in the u-blox 7 GPS stick. The pager auto-detects
   `/dev/ttyACM*` / `/dev/ttyUSB*` and locks onto the first one emitting
   valid NMEA. If it lands on the wrong one, override via
   **CONFIG → GPS DEVICE**.

5. From the pager dashboard pick **Payloads → User → Reconnaissance → WDGoWars Wardriver**.

## UI map

```
SPLASH + GREEN-gate
  │
  ▼
MAIN MENU
  ├── WARDRIVE BOTH     // WiFi + BLE concurrently (separate radios)
  ├── WARDRIVE WIFI     // iw scan only
  ├── WARDRIVE BT       // BLE only
  │    │
  │    ▼ (any scan waits here if no GPS fix)
  │   GPS WAIT   dev:/dev/ttyACM2  sats:N  B=cancel
  │    │
  │    ▼
  │   LIVE HUD 2×2   WiFi / BLE counters, GPS state, queue rows
  │                  A=pause  B=end  ↑↓=brightness
  │
  ├── SYNC NOW       // multipart upload, NEW BADGE flash
  ├── SESSIONS       // list files with v / ^ / x icons
  ├── UPLOAD LOG     // GET /api/upload-history — server-side import counts
  ├── CONFIG
  │    ├── API KEY          // masked view
  │    ├── EDIT API KEY     // hex on-screen keyboard
  │    ├── TEST CONNECTION  // GET /api/me, shows user/wifi/ble/gang
  │    ├── SCAN SETUP
  │    │    ├── WIFI SOURCE      // auto / force monitor / force iw / pick iface
  │    │    ├── BAND PLAN        // rotate / 2.4 only / 2.4+5 / full sweep
  │    │    ├── MONITOR HOP      // let us hop channels, or leave it to a
  │    │    │                    // setup payload that already does
  │    │    ├── MOVE FILTER +/-  // metres before an AP is logged again
  │    │    ├── REFRESH TTL +/-  // slow re-log even when standing still
  │    │    └── REQUIRE GPS FIX  // refuse to write rows without a fix
  │    ├── BRIGHTNESS +/-   // 70% default, stays on position
  │    ├── IDLE TIMEOUT +/- // 20 s default, 5-600 s
  │    ├── DIM LEVEL +/-    // 10% default (hardware off-floor)
  │    └── BACK
  ├── JUMP TO ...    // 4 peers — Loki / PagerGotchi / WiFMan / Bjorn
  └── POWER OFF
```

**Live HUD numbers.** The big number in each cell is *rows written to the CSV*.
The small "seen" number is raw sightings, which climbs several times faster
because every AP is re-seen on every pass. The `ROWS` cell also shows the rate
the file is actually growing at, in rows/min.

## Capture backends

| | Monitor mode | `iw scan` |
|---|---|---|
| Needs | a monitor interface + `tcpdump` | `iw` only |
| Sees | every beacon (~10/s per AP) | one snapshot per pass |
| Position samples | continuous | one per band pass (~2 s) |
| Channel coverage | hops itself (or leave it to a setup payload) | rotating band passes |

### Measured on the device

Same spot, same minute, on a Pager running OpenWrt 24.10.1 / iw 6.9:

| | old default (`iw scan` on `wlan0`) | new default (`wlan1mon`, monitor) |
|---|---|---|
| Unique BSSIDs | 5 | **18** |
| 2.4 GHz | 5 | 12 |
| 5 GHz | 0 | **6** |

The gap is not subtle, and most of it is structural. The Pager has two radios:
`phy0` is **2.4 GHz only**, `phy1` is tri-band. `wlan0` — the interface the
payload used to hardcode — lives on `phy0`, so it could not see a 5 or 6 GHz
network at all, ever. `auto` now picks `wlan1mon` off `phy1`.

The whole pipeline (capture → decode → dedup → CSV) costs about **7% of one
580 MHz MIPS core** with the hopper running, so the headroom is in the radio,
not the software.

`scan.wifi_iface` picks the source:

| Value | Meaning |
|---|---|
| `auto` (default) | prefer a live monitor interface, else the best managed one that is not the pager's own management radio |
| `monitor` / `scan` | force a backend, auto-pick the interface |
| `wlan2mon`, `wlan1`, … | use exactly that interface |

If you stage an external adapter — e.g. an Alfa AWUS036ACM via
[AWUS036ACM_Setup](https://github.com/FusedStamen/AWUS036ACM_Setup) — bring up
`wlan2mon` first and WDGoWars picks it up on `auto` with no further config.
When the monitor backend cannot start, it says why and degrades to `iw scan`
rather than logging nothing for the whole drive.

### Why band rotation

The Pager's primary radio is tri-band. A full `iw scan` sweep touches ~90
channels, and 6 GHz plus the DFS channels must be scanned passively, so one
sweep is several seconds — during which a car at 50 km/h covers a few hundred
metres. Rotating short per-band passes (2.4 GHz every other slot, since that is
where most of what a wardrive logs lives) puts a position sample down every
couple of seconds instead.

### Why observations are back-dated

`iw scan` returns the *kernel's BSS cache*, not what the current sweep heard —
cfg80211 keeps entries for about 30 seconds. Each block carries a
`last seen: N ms ago` field; WDGoWars parses it, back-dates the observation,
drops anything older than the sweep, and asks the kernel to flush the cache
before each pass. Monitor mode sidesteps the problem entirely: every frame
carries its own capture timestamp.

## Deduplication

Time-only deduplication is the wrong rule for wardriving in both directions.
Parked, a 60 s TTL writes every visible AP once a minute forever — forty APs in
range is 2400 rows an hour, all from one coordinate. Driving, it starves: pass
an AP in 40 seconds and you get a single row, when trilateration wants several
from different places.

A row is written when **any** of these holds:

| Condition | Default | Config key |
|---|---|---|
| BSSID not seen this session | always | — |
| moved at least N metres since its last row | 30 m | `scan.min_move_m` |
| signal at least N dB stronger than last time | 6 dB | `scan.rssi_delta_db` |
| this long has passed regardless | 300 s | `scan.refresh_ttl_s` |

Set `min_move_m` to `0` to fall back to pure time-based behaviour.

**No fix, no row.** GPS state keeps the last known position for the HUD after a
dropout, but rows are refused while the fix is gone (`scan.require_fix`) — the
alternative is pinning every AP in a tunnel to the coordinate where the fix
died. The end-of-session dialog reports how many were held back.

**Buttons:** UP/DOWN = navigate, A = select, B = back. In the HUD: ↑↓ adjust
brightness live, A pauses scanning, B ends and saves the session.

## Idle screen dim

After 20 s without input the backlight drops to **10%** (matches the
pagergotchi convention — below that the LCD doesn't actually go any darker
on this hardware). Any button press restores the user brightness.

Configure via `CONFIG → IDLE TIMEOUT +/-` and `DIM LEVEL +/-`, or edit
`config.json`:

```json
"ui": {
  "brightness": 70,
  "idle_timeout_s": 20,
  "auto_dim_level": 10
}
```

Set `idle_timeout_s` to a very large number to effectively disable auto-dim.

## App handoff — JUMP TO …

The main menu shows a **JUMP TO …** entry that lets you hop straight into
another pager payload without returning to the Hak5 dashboard first. Hak5's
system pager UI takes ~30 s to restart; this skips it entirely, so you can
swap between scanner, companion, wardriver and helper apps in a second.

JUMP TO only appears if at least one peer is installed. Each supported app
gets its own row:

| Payload | What it does | Repo |
|---|---|---|
| **Loki** | Autonomous network reconnaissance — ARP/ICMP host discovery, nmap port + NSE vuln scans, SSH/FTP/Telnet/SMB/MySQL/RDP brute force, file exfiltration | [pineapple-pager-projects/pineapple_pager_loki](https://github.com/pineapple-pager-projects/pineapple_pager_loki) |
| **PagerGotchi** | Pwnagotchi port — automated WiFi handshake capture with the classic pwnagotchi "face" + mood animations | [pineapple-pager-projects/pineapple_pager_pagergotchi](https://github.com/pineapple-pager-projects/pineapple_pager_pagergotchi) |
| **Bjorn** | Offensive recon companion with a Viking aesthetic — hosts/attacks/credentials stats, targets list, loot browser | [pineapple-pager-projects/pineapple_pager_bjorn](https://github.com/pineapple-pager-projects/pineapple_pager_bjorn) |
| **WiFMan** | WiFi profile manager — saved SSID list, one-click reconnect, on-device credential keyboard | [LOCOSP/pineapple_pager_wifman](https://github.com/LOCOSP/pineapple_pager_wifman) |

To make the other direction work (so Loki/PagerGotchi/Bjorn/WiFMan can jump
*back* to WDGoWars), push `launchers/launch_wdgwars.sh` into each peer's
directory — the install instructions above include a loop that does this.

Under the hood WDGoWars implements the `exit 42 + data/.next_payload`
protocol ([APP_HANDOFF spec](https://github.com/pineapple-pager-projects/pineapple_pager_loki/blob/main/APP_HANDOFF.md))
that brAinphreAk's projects established. Any other pager app that follows
the same convention will slot in automatically — just drop a matching
`launch_<name>.sh` into `wdgwars/`.

## Output format

Sessions are written as standard [WiGLE WiFi 1.6](https://wigle.net/uploads.html)
CSV files (`wd-<UTC>-<index>.csv`). Successful uploads get a `<name>.csv.uploaded`
sibling marker, failed ones `<name>.csv.error` — the **SESSIONS** screen colour-codes
accordingly. Uploading a pager-generated file earns the 🍍 **Hak5 Pager Op**
badge on wdgwars.pl automatically.

`AuthMode` follows Android's `ScanResult.capabilities` layout, which is what
the WiGLE app itself uploads — WPA1 group, then RSN, then `[WPS]`, then the BSS
type. Enterprise networks are `[WPA2-EAP-…]` rather than being mislabelled as
PSK, OWE is `[WPA3-OWE-…]`, and a WPA3 transition-mode AP reports both
`[WPA3-SAE-…]` and `[WPA2-PSK-…]`. Both capture backends go through the same
builder so an AP looks identical whichever one saw it.

## Uploading

| | v1 | v2 |
|---|---|---|
| Endpoint | `POST /api/upload-csv` | `POST /api/v2/upload-csv` |
| Shape | synchronous, result in the response | `202` + `job_id`, then poll `GET /api/v2/upload-job/<id>` |
| Used for | normal sessions | files ≥ 20 MB, gzipped |

`upload.mode` is `auto` (route by size), `v1`, or `v2`. On `auto`, a sync upload
that dies with a gateway timeout escalates to the queue on retry rather than
failing the file. Request bodies are streamed from a temp file staged next to
the CSV — never `/tmp`, which is tmpfs and would cost the device's RAM.

**UPLOAD LOG** on the main menu reads `GET /api/upload-history`, so you can see
on the pager how many rows the server actually took, and how many it rejected
for `no_gps` or `bad_rows`.

## Security

- The real API key lives **only on the pager** in `config.json` and in your
  local `.key` file (both gitignored).
- `bootstrap.sh` never writes an api key. You must either paste one into
  `config.json` on the pager via SSH or type it via **CONFIG → EDIT API KEY**.
- The payload only collects **publicly broadcast** data — the same information
  every WiFi / BLE device puts out over the air. No packet interception, no
  traffic analysis.

## Local development

Parsers (WiFi, BLE, NMEA) + CSV writer + deduper are decoupled from the pager:

```sh
python3 -m unittest discover -t . -s tests
```

218 tests covering everything that can be exercised without the LCD: both WiFi
parsers, the shared AuthMode builder, GPS position history and fix-dropout
handling, movement-aware dedup, the CSV writer and rotation, interface
selection, the monitor-mode pcap decoder (fed synthetic frames over a pipe),
and the uploader's v1/v2 routing and job polling.

`tests/fixtures/iw_scan_pager_iw69.txt` is a real capture off a Pager. It
pins three format details that differ from older `iw` and each of which broke
an assumption: every BSS block carries a second `last seen: <boottime>` line
before the relative age, `freq:` is printed as a float, and the capability
line is terse — `ESS (0x0431)` with the `Privacy` word omitted even though
bit 4 is set, which had every WEP network classified as open.

`wdgwars.py` needs `pagerctl`, which only exists on the device;
`tests/test_app_smoke.py` stubs it so import errors and menu-wiring typos are
caught off-device too.

## Non-goals

- No web dashboard — everything on the pager LCD.
- No Aircraft (ADS-B) or LoRa mesh — pager has no SDR / LoRa.
- No multi-theme switching — one coherent cyberpunk look.
- No packet capture / handshake grabbing — we only log broadcasts.

## Credits

- pagerctl bindings + payload pattern: [LOCOSP/pineapple_pager_wifman](https://github.com/LOCOSP/pineapple_pager_wifman)
- APP_HANDOFF protocol + auto-dim convention: [pineapple-pager-projects/pineapple_pager_loki](https://github.com/pineapple-pager-projects/pineapple_pager_loki), [pineapple_pager_pagergotchi](https://github.com/pineapple-pager-projects/pineapple_pager_pagergotchi), [pineapple_pager_bjorn](https://github.com/pineapple-pager-projects/pineapple_pager_bjorn) by brAinphreAk
- Portal + API: [wdgwars.pl](https://wdgwars.pl) by LOCOSP
- WigleWifi-1.6 format: [WiGLE](https://wigle.net)
