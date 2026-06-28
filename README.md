# sensor_node

Pi-side **exhaust-gas ingest runtime** for the Aillio Bullet R2 roast stack,
running on the Raspberry Pi 5 `bulletproxy`. This repo owns the services that
receive per-second gas readings from the ESP32 sensor node and write them to
MySQL.

This is one of two repos in the system. For the full architecture, data flow,
and how this fits with the USB/IP capture + dashboard side, see the canonical
**[System overview](https://github.com/p2t587ttqy-ai/roast_dashboard/blob/main/SYSTEM_OVERVIEW.md)**
in the `roast_dashboard` repo.

## What runs here

| Unit | Script | Role |
|------|--------|------|
| `roast-sensor-mqtt.service` | `roast_sensor_mqtt.py` | **Live path** — subscribes to MQTT `roaster/tier1`, stamps arrival time, resolves `roast_id`, inserts into `exhaust_gas_tier1_dev`. |
| `roast-sensor.service` | `roast_sensor_pi.py` | **Legacy/safety-net** — original TCP receiver on `:9030`. Kept functional during the MQTT migration. |
| `roast-gas-cleanup.timer` → `.service` | `cleanup_gas_nullrows.py` | Nightly (02:00) deletion of stale `roast_id IS NULL` baseline rows. |

Supporting scripts: `backfill_gas_roast_id.py` (link gas rows to a roast after
its pcap is imported), `validate_mqtt_path.py` (end-to-end MQTT path check).

The Python runs **in place** from this directory (the units' `WorkingDirectory`
and `ExecStart` point at `/home/doeichel/sensor_node/`), so updating code is just
`git pull` — nothing is copied to `/usr/local/bin`.

## Deploy

```bash
sudo ./install.sh        # syncs *.service / *.timer to /etc/systemd/system
```

Idempotent: only writes changed unit files and only `daemon-reload`s when one
changed. MySQL/MQTT credentials come from root-only `/etc/roast.env` (managed
outside this repo). First-time enable:

```bash
sudo systemctl enable --now roast-sensor.service roast-sensor-mqtt.service
sudo systemctl enable --now roast-gas-cleanup.timer
```

## Notes

- `roast-sensor.service` also exists, byte-identical, in `roast_dashboard/pi5/`.
  Treat **this repo** as authoritative for it. See the System overview's
  "Known overlap / gotcha" section.
- More background: `BUILD_BRIEF.md`, `docs/tier1-mqtt-migration.md`,
  `docs/cutover-mqtt.md`.
