# Tier 1 Gas Sensor — TCP → MQTT Transport Migration

**Status:** Stages 0–4 complete; cutover pending gate criteria (see `cutover-mqtt.md`).  
**Date:** May 2026

---

## Why MQTT, why now

The original architecture was a point-to-point TCP socket:

```
ESP32 ──WiFi/TCP port 9030──▶ roast_sensor_pi.py ──▶ MySQL exhaust_gas_tier1
```

This works for one node but does not scale. The near-term roadmap adds:

- **Acoustic node** — microphone on the drum for first-crack audio detection
- **Tier 2 gas sensors** — selective VOC (PID, formaldehyde) for diacetyl/guaiacol fingerprinting
- Possibly a **second SCD41** for differential ambient/exhaust measurement

Each additional node on the TCP model would require its own port and its own listener
process on the Pi. An MQTT broker inverts this: every sensor node publishes to a named
topic; the Pi subscribes once per topic. Adding a node means adding a topic — no new
port, no new listener process.

The Mosquitto broker was already installed on the Pi. The migration cost was low; the
fan-out benefit compounds with every new node added.

---

## What was deliberately NOT changed

### Timestamp authority

The Pi 5 stamps wall-clock arrival time (`pi_timestamp`) on every sample.
This is unchanged. The ESP32's `t_ms` (uptime milliseconds) is preserved as a
forensic field but is never used for data alignment — only `pi_timestamp` is.

This matters because:
- The ESP32's clock drifts and resets on reboot; it has no RTC
- The Pi's NTP-disciplined clock is the single time authority for all data streams
- `pcap_epoch_offset` in `roast_meta` anchors the USB/IP pcap to wall-clock time via
  the same Pi clock, so the gas and pcap streams align without any additional
  synchronisation logic

### roast_id resolution

The auto-resolver logic — checking pcap freshness, querying `roast_meta` for a row
whose `pcap_epoch_offset` is within the last 30 minutes, caching for 5 seconds — is
copied verbatim from `roast_sensor_pi.py` into `roast_sensor_mqtt.py`. Not a single
line of that logic changed. The Stage 3 validator (`validate_mqtt_path.py`) checks
roast_id agreement between the two paths on every sample as part of the cutover gate.

### JSON parsing and MySQL insert logic

The `normalize_sample()` function and `SampleInserter` class are copied unchanged.
The only functional difference between the two scripts is the transport layer:
TCP `socket.recv()` → MQTT `on_message` callback.

---

## Why no roaster-state gate

An earlier draft of this design gated MySQL inserts on an "active roast" condition —
only write rows when a roast is in progress, discarding idle/calibration samples.

This was rejected for two reasons:

1. **False gate risk.** The pcap-freshness check is the only available signal for
   "roast in progress." If the capture service hiccups during a roast, the gate would
   silently discard real roast data. A silent drop is worse than an idle row.

2. **Idle data has value.** Ambient CO₂/CO baselines before and after a roast
   contextualise the roast chemistry. A cold ESP32 startup after a long idle period
   lets the SCD41's rolling derivative stabilise before beans drop.

Instead, idle samples are written with `roast_id = NULL` and pruned by the nightly
cleanup job (`roast-gas-cleanup.timer`, 7-day retention). This keeps the table lean
without discarding any data that might be useful.

---

## Parallel-paths strategy and dev-table isolation

Running both transports simultaneously during the build serves two purposes:

1. **Zero-risk validation.** The TCP path remains the authoritative data source
   throughout the build. If the MQTT path has a bug, no roast data is lost.

2. **Functional comparison.** Both paths receive the same physical samples from the
   same ESP32. Any difference in values, timestamps, or roast_id resolution between
   `exhaust_gas_tier1` (TCP) and `exhaust_gas_tier1_dev` (MQTT) is a bug in the
   MQTT path, not noise.

The dev-table isolation rule — **the MQTT path writes only to `exhaust_gas_tier1_dev`
until a human performs the cutover** — is what makes parallel operation safe. Two
writers into the same table would produce duplicate rows with identical `esp_uptime_ms`
values, breaking every downstream query that assumes one row per second per roast.

The table is identical in schema to the live table (same columns, indexes, foreign key)
so the MQTT path exercises real production code paths throughout the build phase.

---

## Stage summary

| Stage | What it does | Output |
|---|---|---|
| 0 | Configure Mosquitto to accept LAN connections | `/etc/mosquitto/conf.d/` config |
| 1 | Add MQTT publish to ESP32 firmware alongside TCP | `firmware/src/main.cpp` (dual-transport) |
| 2 | Pi MQTT subscriber writing to dev table | `roast_sensor_mqtt.py`, `exhaust_gas_tier1_dev` |
| 3 | Validation harness | `validate_mqtt_path.py` |
| 4 | Dev systemd service + nightly NULL-row cleanup | `roast-sensor-mqtt.service`, `roast-gas-cleanup.timer` |
| 5 | Cutover runbook + this design note | `docs/cutover-mqtt.md`, this file |

---

## Files produced by this migration

```
sensor_node/
├── roast_sensor_mqtt.py          MQTT subscriber (Pi-side)
├── validate_mqtt_path.py         Stage 3 validator / cutover gate
├── cleanup_gas_nullrows.py       Nightly NULL-row pruner
├── roast-sensor-mqtt.service     Deployed to /etc/systemd/system/
├── roast-gas-cleanup.service     Deployed to /etc/systemd/system/
├── roast-gas-cleanup.timer       Deployed to /etc/systemd/system/
└── docs/
    ├── cutover-mqtt.md           Step-by-step cutover runbook
    └── tier1-mqtt-migration.md   This file
```

MySQL:

```
exhaust_gas_tier1_dev    dev table (identical schema to live table)
                         Dropped as the final step of cutover.
```

---

## Join key reference

For alignment queries between the pcap and gas streams:

```sql
ROUND(g.pi_timestamp - rm.pcap_epoch_offset) = p.elapsed_sec
```

`pcap_epoch_offset` is the Unix timestamp of `elapsed_sec = 0` (the IBTS-drop charge
anchor). Both `pi_timestamp` and `pcap_epoch_offset` are Pi-wall-clock seconds, so
this expression gives the gas sample's position in roast-elapsed time.

See `roast_dashboard/sql/analysis_pcap_sensor_alignment.sql` for six ready-to-run
alignment queries including phase annotation, FC window chemistry, and cross-roast
FC-relative profiles.
