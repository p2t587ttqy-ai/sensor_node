Claude Code build brief — Tier 1 gas sensor transport migration (TCP → MQTT)
Context
This project monitors an Aillio Bullet R2 coffee roaster on a Raspberry Pi 5 (bulletproxy, user doeichel). An ESP32 sensor node (SCD41 CO2, ZE07-CO, TGS2602 VOC) sends per-second gas readings to the Pi.
The current, working architecture: ESP32 → WiFi/TCP → Pi 5 → MySQL. A Python service roast_sensor_pi.py (in /home/doeichel/sensor_node/, run by roast-sensor.service) listens on TCP port 9030, accepts JSON lines, stamps wall-clock arrival time, resolves a roast_id, and inserts into the exhaust_gas_tier1 table in the roastdata MySQL database (host 192.168.0.87).
This task migrates the transport only: ESP32 → MQTT → Pi 5 → MySQL. The Pi-side parsing, timestamping, roast_id resolution, and MySQL writes already work and should be preserved as-is. We are swapping a raw TCP socket for an MQTT subscription. MQTT is chosen because more sensor nodes are coming (an acoustic node, Tier 2 sensors) and a broker fans out cleanly.
Why MQTT, why now (architecture rationale)
The current TCP listener works but is point-to-point. Upcoming nodes (acoustic first-crack detection, Tier 2 gas sensors) would each need their own port and listener. An MQTT broker lets every node publish to topics and the Pi subscribe once. The Pi remains the single timestamp authority and the single MySQL writer — that does not change.
What is already solved — do NOT rebuild these

Timestamp authority. roast_sensor_pi.py already stamps Pi-side wall-clock arrival time. Keep this exactly.
roast_id resolution. The receiver already auto-resolves roast_id (checks for a fresh pcap, queries roast_meta for a roast within the last 30 minutes, attaches the id, or inserts roast_id = NULL for idle/baseline data). Keep this logic unchanged.
No gating needed. Idle-time samples are already handled via roast_id = NULL. Do NOT build a roaster-state gate or a pre-roll buffer. Table leanness is handled separately by a nightly cleanup (see Stage 4).

Critical constraints — read before doing anything

DO NOT modify or restart the USB/IP capture pipeline services: usbip-modules.service, usbipd.service, usbip-bind-bullet.service, usbip-capture.service. Interrupting a capture corrupts a roast.
DO NOT modify roast-dashboard.service or the dashboard code in this build, except for one documented line at cutover (its unit file has After=... roast-sensor.service). That edit is part of the Stage 5 cutover documentation, not an action you take now.
DO NOT modify the existing roast-sensor.service or roast_sensor_pi.py in place. The current TCP path stays fully functional as the safety net for the entire build. Work on copies.
The new MQTT path writes to a separate table, exhaust_gas_tier1_dev, never the real exhaust_gas_tier1, until a human-performed cutover. Two writers into one table create duplicate rows.
Cutover is performed manually by the human, not by you. Your build ends at "MQTT path validated and ready." Produce cutover documentation; do not execute it.
~/sensor_node/ has just been placed under git with a baseline commit. Commit your work in logical increments so every step is recoverable.
Default permission prompts are expected — proceed step by step, explain each action.

Environment

Pi: bulletproxy, user doeichel.
MySQL: roastdata at 192.168.0.87. Credentials come from EnvironmentFile=/etc/roast.env — the new service MUST use the same env file, not a new config mechanism.
MQTT broker: Mosquitto, already installed and enabled on the Pi (mosquitto.service). Port 1883. Topic: roaster/tier1. Note: a default Mosquitto install may only listen on localhost — configuring it to accept LAN connections from the ESP32 is Stage 0 below.
Current gas receiver: /home/doeichel/sensor_node/roast_sensor_pi.py, unit file roast-sensor.service (TCP port 9030).
ESP32 firmware: framework PlatformIO / Arduino. Firmware is in the project repo; locate it. The ESP32 currently sends JSON lines over TCP — the JSON message schema is documented in the roast_sensor_pi.py docstring (co2, temp, rh, co2_rate, co, co_age_s, co_co2, rssi, t_ms).
Existing service hardening: roast-sensor.service runs with NoNewPrivileges, ProtectSystem=strict, ProtectHome=read-only, explicit ReadWritePaths. The new dev service must match this pattern — and any local file it writes (e.g. a Stage 2 flat-file log) MUST have its directory declared in ReadWritePaths or the write will fail silently under the hardening.

Build stages
Work in order. Each stage must be independently testable and left working before the next.
Stage 0 — Mosquitto LAN configuration (Pi).
Mosquitto is installed and running. Configure it to accept LAN connections on port 1883 (a config file in /etc/mosquitto/conf.d/ with a listener 1883 directive and an explicit allow_anonymous decision — document the choice). Reload Mosquitto. Verify with mosquitto_sub/mosquitto_pub that a message published from another host would be received. Do not disturb any other service.
Stage 1 — ESP32 publishes to MQTT (PlatformIO/Arduino).
First read the current firmware to see how it builds and sends the JSON-over-TCP message. Add an MQTT publish path (e.g. PubSubClient or equivalent) that publishes the same JSON payload to topic roaster/tier1 at the same 1 Hz cadence. Keep the existing TCP-send path in place and active — the firmware temporarily does both. Verify with mosquitto_sub on the Pi that messages arrive at ~1 Hz with sane values.
Stage 2 — MQTT subscriber as a modified copy of the receiver (Pi).
Copy roast_sensor_pi.py to a new file (e.g. roast_sensor_mqtt.py) in ~/sensor_node/. Replace the TCP socket listener with an MQTT client subscribed to roaster/tier1. Preserve unchanged: the JSON parsing, the Pi-side timestamp stamping, the roast_id resolution logic, and the MySQL insert logic. Repoint the insert to a new table exhaust_gas_tier1_dev. First inspect the real table — DESCRIBE exhaust_gas_tier1 — and create exhaust_gas_tier1_dev with an identical schema. Run the new subscriber by hand (not yet a service); verify rows land in exhaust_gas_tier1_dev and that roast_id resolution still behaves (NULL when idle, a real id during/near a roast).
Stage 3 — Validation harness.
Write a script that, for a given roast (or time window), diffs the MQTT path (exhaust_gas_tier1_dev) against the TCP path (exhaust_gas_tier1): sensor values should be identical (same physical ESP32, same readings — only transport differs); arrival timestamps should match within a small tolerance; roast_id resolution should agree. Report discrepancies clearly. This is the cutover gate.
Stage 4 — dev systemd service + nightly cleanup.
Package the MQTT subscriber as a new systemd service (e.g. roast-sensor-mqtt.service), boot-started, modeled on roast-sensor.service — same User, same EnvironmentFile=/etc/roast.env, same hardening directives, ReadWritePaths covering anything it writes. It runs alongside the existing roast-sensor.service during the build. Also write a cleanup script (a nightly cron job or systemd timer) that deletes or archives rows WHERE roast_id IS NULL older than a configurable age — this keeps the gas table lean now that idle samples are persisted with NULL roast_id. Apply the cleanup to exhaust_gas_tier1_dev during the build; the cutover doc will note switching it to the real table.
Stage 5 — Cutover documentation (no execution).
Write a markdown runbook for the manual cutover. Criterion: three consecutive clean roasts where the MQTT and TCP paths agree (sensor values, timestamps within tolerance, roast_id resolution), including at least one roast where preheat data was captured and one containing a firmware-glitch gap. Steps: update the firmware to remove the TCP-send path; repoint roast_sensor_mqtt.py from exhaust_gas_tier1_dev to the real exhaust_gas_tier1; disable and stop the old roast-sensor.service; update roast-dashboard.service's After= line to reference roast-sensor-mqtt.service instead of roast-sensor.service; point the nightly cleanup at the real table; drop exhaust_gas_tier1_dev; tag the last known-good TCP-path commit as a rollback point.
Deliverable: design note
Write docs/tier1-mqtt-migration.md capturing the rationale: why MQTT (multi-node fan-out), what was deliberately NOT changed (timestamp authority, roast_id resolution — already correct), why no gating (NULL-roast_id + nightly cleanup handles idle data), and the parallel-paths-then-cutover strategy with the dev-table isolation rule.
