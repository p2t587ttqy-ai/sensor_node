# TCP → MQTT Cutover Runbook

**Do not execute these steps until the gate criteria below are met.**  
This document is produced at Stage 5 of the transport migration build.  
All preceding stages (0–4) must be complete and both services must be running.

---

## Gate criteria

Run `validate_mqtt_path.py --roast-id N` after every instrumented roast while both
`roast-sensor.service` (TCP) and `roast-sensor-mqtt.service` (MQTT) are active.

Cutover is permitted only after **three consecutive PASS verdicts**, meeting all of:

| Criterion | How to verify |
|---|---|
| Sensor values identical | `Value mismatches: 0` in validator output |
| Timestamps within tolerance | `over_tolerance=0` (default ±2 s) |
| roast_id resolution agrees | `roast_id disagreements: 0` |
| At least one roast with preheat data | `Q2 coverage: preheat gap_pct < 5%` |
| At least one roast with a firmware-glitch gap | Gap visible in `Q2` and replicated in both tables (both show `gas_missing` for the same elapsed_sec values) |

```bash
# Run after each roast (replace N with the roast_id):
python3 /home/doeichel/sensor_node/validate_mqtt_path.py \
    --env-file /etc/roast.env --roast-id N
```

Record the three PASS roast IDs here before proceeding:

| # | roast_id | Date | Notes |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |

---

## Pre-cutover checklist

- [ ] Three consecutive PASS roasts recorded above
- [ ] Both sensor services currently active:
  ```bash
  sudo systemctl is-active roast-sensor.service roast-sensor-mqtt.service
  ```
- [ ] No roast in progress (check `roast-dashboard.service` or confirm Bullet is cold)
- [ ] MySQL accessible and `exhaust_gas_tier1` not locked
- [ ] PlatformIO toolchain available on the workstation for firmware flash

---

## Step 1 — Tag the rollback point

Before touching anything, tag the last commit where the TCP path is known good.

```bash
cd /home/doeichel/sensor_node
git tag tcp-path-last-known-good
git push origin tcp-path-last-known-good
```

This tag is the single-command rollback target for every subsequent step.

---

## Step 2 — Remove TCP from firmware

The firmware currently publishes to both TCP and MQTT. Remove the TCP-send path,
keeping MQTT-only. Do this on the **workstation** (Surface), not the Pi.

### 2a. Edit `firmware/include/config.h`

Remove these two lines — they have no function after cutover:

```c
// DELETE these two lines:
#define PI5_HOST          "192.168.0.94"
#define PI5_PORT          9030
```

### 2b. Edit `firmware/src/main.cpp`

Make the following removals/replacements in order:

**Remove** the `tcpClient` variable (line ~40):
```c
// DELETE:
WiFiClient tcpClient;
```

**Remove** the `tcpConnect()` function (lines ~181–190):
```c
// DELETE the entire function:
bool tcpConnect() {
    if (tcpClient.connected()) return true;
    ...
    tcpClient.setNoDelay(true);
    ...
}
```

**In `setup()`** — remove TCP init, update LED to reflect MQTT state:
```c
// DELETE:
tcpConnect();
...
if (tcpClient.connected()) ledMode = LED_SOLID;

// REPLACE WITH:
if (mqttClient.connected()) ledMode = LED_SOLID;
```

**In `loop()`** — remove the TCP reconnect block (lines ~363–371):
```c
// DELETE the entire block:
if (!tcpClient.connected()) {
    ledMode = LED_SLOW_BLINK;
    if (WiFi.status() != WL_CONNECTED) {
        wifiConnect();
    }
    tcpConnect();
}
```
And update the LED to track MQTT instead:
```c
// ADD after the existing MQTT reconnect logic:
if (!mqttClient.connected()) ledMode = LED_SLOW_BLINK;
else                         ledMode = LED_SOLID;
```

**In `loop()`** — remove the TCP write block (lines ~400–409):
```c
// DELETE:
if (tcpClient.connected()) {
    size_t written = tcpClient.write((const uint8_t*)line, n);
    if (written != n) {
        Serial.println("[TCP] write incomplete, dropping connection");
        tcpClient.stop();
        ledMode = LED_SLOW_BLINK;
    } else {
        ledMode = LED_SOLID;
    }
}
```

### 2c. Build and flash

```bash
pio run --target upload
```

### 2d. Verify MQTT still flows

On the Pi, confirm messages still arrive at ~1 Hz with sane values:

```bash
mosquitto_sub -h localhost -t roaster/tier1 -C 5
```

Five clean JSON lines = firmware cutover confirmed.

---

## Step 3 — Repoint `roast_sensor_mqtt.py` to the live table

Edit **one line** in `/home/doeichel/sensor_node/roast_sensor_mqtt.py`:

```python
# Change this line (in the Configuration section, ~line 55):
TABLE_NAME = "exhaust_gas_tier1_dev"
# to:
TABLE_NAME = "exhaust_gas_tier1"
```

Restart the service to pick up the change:

```bash
sudo systemctl restart roast-sensor-mqtt.service
sudo journalctl -u roast-sensor-mqtt -n 10 --no-pager
```

Confirm the log shows `Insert table: exhaust_gas_tier1`.

---

## Step 4 — Disable and stop the old TCP service

```bash
sudo systemctl disable --now roast-sensor.service
```

Verify it is no longer running:

```bash
sudo systemctl is-active roast-sensor.service   # should print "inactive"
sudo systemctl is-active roast-sensor-mqtt.service  # should print "active"
```

The TCP port 9030 is now unbound. The ESP32 will log `[TCP] connect failed` and
continue publishing MQTT — this is expected and benign.

---

## Step 5 — Update `roast-dashboard.service` dependency

The dashboard's `After=` line references `roast-sensor.service`. Change it to
`roast-sensor-mqtt.service` so boot ordering remains correct.

Edit `/etc/systemd/system/roast-dashboard.service`:

```ini
# Change:
After=network-online.target mysql.service usbip-capture.service roast-sensor.service

# To:
After=network-online.target mysql.service usbip-capture.service roast-sensor-mqtt.service
```

Reload:

```bash
sudo systemctl daemon-reload
```

The dashboard does not need a restart — this only affects boot ordering.

---

## Step 6 — Point nightly cleanup at the live table

Edit `/etc/systemd/system/roast-gas-cleanup.service`:

```ini
# Change:
ExecStart=... --table exhaust_gas_tier1_dev ...

# To:
ExecStart=... --table exhaust_gas_tier1 ...
```

Reload:

```bash
sudo systemctl daemon-reload
```

Trigger one manual run to confirm it can connect and counts idle rows correctly:

```bash
sudo systemctl start roast-gas-cleanup.service
sudo journalctl -u roast-gas-cleanup -n 20 --no-pager
```

---

## Step 7 — Drop `exhaust_gas_tier1_dev`

Only after all six steps above are verified and at least one successful roast has
been recorded into `exhaust_gas_tier1` via the MQTT path:

```python
# Run as: python3 drop_dev_table.py  (or paste into mysql session)
# Credentials via /etc/roast.env — use the load_env_file pattern.

cur.execute("DROP TABLE exhaust_gas_tier1_dev")
```

Or interactively:

```bash
# Via a Python one-liner using load_env_file (same pattern as other scripts):
python3 -c "
import mysql.connector, os
def lef(p):
    [os.environ.update({k.strip(): v.strip()}) for k in open(p)
     for k,_,v in [k.partition('=')] if k.strip() and not k.startswith('#')]
lef('/etc/roast.env')
c=mysql.connector.connect(host=os.environ['ROAST_DB_HOST'],
  user=os.environ['ROAST_DB_USER'],password=os.environ['ROAST_DB_PASS'],
  database=os.environ['ROAST_DB_NAME'])
c.cursor().execute('DROP TABLE exhaust_gas_tier1_dev')
c.commit(); print('Dropped')
"
```

---

## Post-cutover verification

```bash
# 1. MQTT service writing to real table
sudo journalctl -u roast-sensor-mqtt -f

# 2. Row count growing in the live table
# (run as the service would — use load_env_file pattern)

# 3. TCP service is gone
sudo systemctl is-active roast-sensor.service   # inactive

# 4. Dashboard still works
curl -s http://192.168.0.94:8000/api/health

# 5. Run the validator one last time over a recent window
python3 /home/doeichel/sensor_node/validate_mqtt_path.py \
    --env-file /etc/roast.env --window 60
# Expected: FAIL with "Only TCP: N" for pre-cutover history,
# but 0 Only-MQTT and 0 mismatches for the post-cutover window.
```

---

## Rollback

If anything fails before Step 7 (table still exists):

```bash
# 1. Restore firmware
cd /home/doeichel/sensor_node
git checkout tcp-path-last-known-good -- ../roast_dashboard/firmware/src/main.cpp
git checkout tcp-path-last-known-good -- ../roast_dashboard/firmware/include/config.h
# Flash via PlatformIO

# 2. Revert TABLE_NAME in roast_sensor_mqtt.py
# Change TABLE_NAME back to "exhaust_gas_tier1_dev"

# 3. Re-enable TCP service
sudo systemctl enable --now roast-sensor.service

# 4. Revert dashboard After= and cleanup --table edits
sudo systemctl daemon-reload
```

If Step 7 has already run and the table is dropped, restore from the most recent
MySQL backup before rollback.
