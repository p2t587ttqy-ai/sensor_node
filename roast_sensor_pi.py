"""
roast_sensor_pi.py
------------------
Pi 5 TCP listener for the Bullet R2 Tier 1 exhaust gas sensor node.

Architecture:
    ESP32 (sensor node)  ──WiFi/TCP──>  Pi 5 (this script)
                                            │
                                       MySQL INSERT into
                                       exhaust_gas_tier1 table
                                            │
                                       roast_id auto-resolved from
                                       latest pcap file in captures dir

Listens on a TCP port and accepts JSON lines, one per second, from the
ESP32 firmware. Each line contains:
    {
      "t_ms":      uptime ms since ESP32 boot,
      "co2":       CO2 ppm (or null),
      "temp":      sensor temperature °C (or null),
      "rh":        relative humidity % (or null),
      "co2_rate":  rolling 3-second derivative ppm/s,
      "co":        CO ppm (0 if no fresh frame yet),
      "co_age_s":  seconds since last fresh CO frame (-1 if never),
      "co_co2":    CO/CO2 ratio (-1 if invalid),
      "rssi":      WiFi RSSI dBm
    }

The Pi 5 stamps the wall-clock arrival time (its monotonic UTC clock is
the single timestamp authority for this whole pipeline), so the ESP32's
uptime t_ms is preserved as a forensic field but not used for alignment.

Roast-ID auto-detection:
    On each sample arrival, the receiver checks:
      1. Is there a pcap file in CAPTURES_DIR newer than 60 seconds?
      2. If yes, it queries MySQL for the most recent roast_meta row
         whose pcap_epoch_offset is within the last 30 minutes.
      3. If found, attaches that roast_id to the sensor row.
      4. If not, the row is inserted with roast_id = NULL (calibration
         data, baseline, or between-roast idle).

This mirrors the validate-state logic in roast_relay_pi.py.

Requires:
    pip install mysql-connector-python

Environment variables (already set on Pi 5 from prior work):
    ROAST_DB_HOST, ROAST_DB_USER, ROAST_DB_PASS, ROAST_DB_NAME

Usage:
    python roast_sensor_pi.py [--host 0.0.0.0] [--port 9030]
                              [--captures-dir /home/doeichel/captures/]
                              [--no-mysql]   # log to stdout only
                              [--schema]     # print DDL and exit

Schema (run once before first roast):
    python roast_sensor_pi.py --schema | mysql -h $ROAST_DB_HOST -u root -p
"""

import argparse
import glob
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import mysql.connector

# ============================================================
#  Configuration
# ============================================================

DB_CONFIG = {
    "host":     os.environ.get("ROAST_DB_HOST", "192.168.0.87"),
    "port":     int(os.environ.get("ROAST_DB_PORT", "3306")),
    "user":     os.environ.get("ROAST_DB_USER", "root"),
    "password": os.environ.get("ROAST_DB_PASS", ""),
    "database": os.environ.get("ROAST_DB_NAME", "roastdata"),
}

DEFAULT_HOST          = "0.0.0.0"
DEFAULT_PORT          = 9030
DEFAULT_CAPTURES_DIR  = "/home/doeichel/captures/"

# How recently must the newest pcap be modified for us to consider
# a roast "active"?  Capture service writes packets continuously while
# RoasTime is connected.
PCAP_FRESHNESS_S      = 60

# How recently must roast_meta.pcap_epoch_offset be for us to attach
# sensor data to that roast?
ROAST_LINK_WINDOW_S   = 30 * 60   # 30 minutes

# Cache the resolved roast_id for this many seconds before re-querying
# (to avoid hammering MySQL with one SELECT per sensor sample)
ROAST_ID_CACHE_S      = 5

# Buffer flushing
INSERT_BATCH_SIZE     = 10
INSERT_FLUSH_INTERVAL = 5.0

# ============================================================
#  Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sensor-pi")


# ============================================================
#  Schema DDL
# ============================================================

SCHEMA_DDL = """
-- ============================================================
-- exhaust_gas_tier1 — one row per second per sensor sample
-- ============================================================
CREATE TABLE IF NOT EXISTS exhaust_gas_tier1 (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    roast_id        INT NULL,
    pi_timestamp    DOUBLE NOT NULL
                    COMMENT 'Pi 5 monotonic UTC epoch when sample arrived (single time authority)',
    esp_uptime_ms   INT UNSIGNED NULL
                    COMMENT 'ESP32 millis() at sample — forensic only',

    -- Primary CO2 measurement
    co2_ppm         FLOAT NULL,
    co2_rate        FLOAT NULL
                    COMMENT 'Rolling 3-second derivative ppm/s — FC onset signal',
    co2_temp        FLOAT NULL
                    COMMENT 'SCD41 internal temp (sensor body, not roast exhaust)',
    co2_rh          FLOAT NULL
                    COMMENT 'SCD41 relative humidity %',

    -- CO measurement
    co_ppm          FLOAT NULL,
    co_age_s        FLOAT NULL
                    COMMENT 'Seconds since last fresh ZE07-CO frame',

    -- Derived ratio
    co_co2_ratio    FLOAT NULL
                    COMMENT 'CO/CO2 ratio. <0.05=Maillard, >0.10=pyrolysis',

    -- Network/diagnostic
    rssi_dbm        SMALLINT NULL,

    INDEX idx_roast_time   (roast_id, pi_timestamp),
    INDEX idx_pi_timestamp (pi_timestamp),
    FOREIGN KEY (roast_id) REFERENCES roast_meta(roast_id)
);
"""


# ============================================================
#  Roast-ID auto-detection
# ============================================================

class RoastIdResolver:
    """Resolves the active roast_id by checking pcap freshness and
    querying MySQL for the most recent roast_meta row.

    Caches the result for ROAST_ID_CACHE_S seconds to limit DB load.
    """

    def __init__(self, captures_dir, db_config, no_mysql=False):
        self.captures_dir = captures_dir
        self.db_config    = db_config
        self.no_mysql     = no_mysql
        self._cache_id    = None
        self._cache_at    = 0.0
        self._lock        = threading.Lock()

    def newest_pcap_age(self):
        """Return age in seconds of newest pcap file, or None if none."""
        try:
            files = glob.glob(os.path.join(self.captures_dir, "*.pcap"))
            if not files:
                return None
            newest_mtime = max(os.path.getmtime(f) for f in files)
            return time.time() - newest_mtime
        except OSError:
            return None

    def resolve(self):
        """Return (roast_id_or_None, reason_string)."""
        with self._lock:
            now = time.time()
            if now - self._cache_at < ROAST_ID_CACHE_S:
                return self._cache_id, "cached"

            # Check pcap freshness
            age = self.newest_pcap_age()
            if age is None:
                self._cache_id, self._cache_at = None, now
                return None, "no pcap files"
            if age > PCAP_FRESHNESS_S:
                self._cache_id, self._cache_at = None, now
                return None, f"newest pcap is {age:.0f}s old (idle)"

            # Query MySQL for most recent roast_meta in window
            if self.no_mysql:
                self._cache_id, self._cache_at = None, now
                return None, "mysql disabled"

            try:
                conn = mysql.connector.connect(**self.db_config)
                cur  = conn.cursor()
                cutoff = now - ROAST_LINK_WINDOW_S
                cur.execute(
                    "SELECT roast_id FROM roast_meta "
                    "WHERE pcap_epoch_offset IS NOT NULL "
                    "  AND pcap_epoch_offset >= %s "
                    "ORDER BY pcap_epoch_offset DESC LIMIT 1",
                    (cutoff,),
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    self._cache_id, self._cache_at = row[0], now
                    return row[0], f"linked (pcap {age:.0f}s fresh)"
                else:
                    self._cache_id, self._cache_at = None, now
                    return None, f"pcap fresh ({age:.0f}s) but no recent roast_meta row"
            except mysql.connector.Error as e:
                log.warning(f"MySQL query failed: {e}")
                self._cache_id, self._cache_at = None, now
                return None, "mysql error"


# ============================================================
#  Sample inserter
# ============================================================

class SampleInserter:
    """Buffers incoming sensor samples and flushes to MySQL in batches.

    Decoupled from the network thread so a slow DB doesn't backpressure
    the TCP socket. Uses a thread-safe queue.
    """

    def __init__(self, db_config, no_mysql=False):
        self.db_config = db_config
        self.no_mysql  = no_mysql
        self._buf      = []
        self._lock     = threading.Lock()
        self._stop     = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="inserter", daemon=True
        )
        self._flush_thread.start()

    def add(self, row):
        with self._lock:
            self._buf.append(row)
            should_flush = len(self._buf) >= INSERT_BATCH_SIZE
        if should_flush:
            self._flush()

    def _flush(self):
        with self._lock:
            if not self._buf:
                return
            batch, self._buf = self._buf, []

        if self.no_mysql:
            for row in batch:
                log.info(
                    f"[no-mysql] roast={row['roast_id']} "
                    f"co2={row['co2_ppm']} co={row['co_ppm']} "
                    f"rate={row['co2_rate']} ratio={row['co_co2_ratio']}"
                )
            return

        try:
            conn = mysql.connector.connect(**self.db_config)
            cur  = conn.cursor()
            cur.executemany(
                "INSERT INTO exhaust_gas_tier1 ("
                "  roast_id, pi_timestamp, esp_uptime_ms,"
                "  co2_ppm, co2_rate, co2_temp, co2_rh,"
                "  co_ppm, co_age_s, co_co2_ratio,"
                "  rssi_dbm"
                ") VALUES ("
                "  %(roast_id)s, %(pi_timestamp)s, %(esp_uptime_ms)s,"
                "  %(co2_ppm)s, %(co2_rate)s, %(co2_temp)s, %(co2_rh)s,"
                "  %(co_ppm)s, %(co_age_s)s, %(co_co2_ratio)s,"
                "  %(rssi_dbm)s"
                ")",
                batch,
            )
            conn.commit()
            cur.close()
            conn.close()
        except mysql.connector.Error as e:
            log.error(f"MySQL insert failed ({len(batch)} rows lost): {e}")

    def _flush_loop(self):
        while not self._stop.is_set():
            if self._stop.wait(INSERT_FLUSH_INTERVAL):
                break
            self._flush()
        # Final flush on shutdown
        self._flush()

    def stop(self):
        self._stop.set()
        self._flush_thread.join(timeout=10.0)


# ============================================================
#  TCP server
# ============================================================

def normalize_sample(raw, pi_timestamp, roast_id):
    """Convert ESP32 JSON dict into a MySQL row dict.

    Handles None/null fields and the ESP32's convention of using
    -1 / 0 / null for various 'no data yet' states.
    """
    co_ppm = raw.get("co")
    if co_ppm == 0 and raw.get("co_age_s", -1) < 0:
        co_ppm = None  # ZE07 hasn't sent its first frame yet

    co_co2 = raw.get("co_co2")
    if co_co2 is not None and co_co2 < 0:
        co_co2 = None

    co_age = raw.get("co_age_s")
    if co_age is not None and co_age < 0:
        co_age = None

    return {
        "roast_id":      roast_id,
        "pi_timestamp":  pi_timestamp,
        "esp_uptime_ms": raw.get("t_ms"),
        "co2_ppm":       raw.get("co2"),
        "co2_rate":      raw.get("co2_rate"),
        "co2_temp":      raw.get("temp"),
        "co2_rh":        raw.get("rh"),
        "co_ppm":        co_ppm,
        "co_age_s":      co_age,
        "co_co2_ratio":  co_co2,
        "rssi_dbm":      raw.get("rssi"),
    }


def handle_client(conn, addr, resolver, inserter):
    log.info(f"ESP32 connected: {addr}")
    buf = b""
    last_log = 0.0
    sample_count = 0
    last_roast_id = -2
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                log.info(f"ESP32 disconnected: {addr}")
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    log.warning(f"bad JSON from {addr}: {e}")
                    continue

                pi_ts = time.time()
                roast_id, reason = resolver.resolve()
                row = normalize_sample(raw, pi_ts, roast_id)
                inserter.add(row)
                sample_count += 1

                # Log roast_id transitions
                if roast_id != last_roast_id:
                    log.info(f"roast_id resolution changed: {last_roast_id} → "
                             f"{roast_id}  ({reason})")
                    last_roast_id = roast_id

                # Periodic status line
                if pi_ts - last_log > 30.0:
                    log.info(
                        f"sample #{sample_count}  "
                        f"roast_id={roast_id}  "
                        f"co2={row['co2_ppm']} ppm  "
                        f"co={row['co_ppm']} ppm  "
                        f"rate={row['co2_rate']} ppm/s  "
                        f"ratio={row['co_co2_ratio']}  "
                        f"rssi={row['rssi_dbm']} dBm"
                    )
                    last_log = pi_ts
    except (ConnectionResetError, BrokenPipeError) as e:
        log.info(f"ESP32 connection error {addr}: {e}")
    except Exception as e:
        log.exception(f"unexpected error handling {addr}: {e}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(host, port, captures_dir, no_mysql):
    resolver = RoastIdResolver(captures_dir, DB_CONFIG, no_mysql=no_mysql)
    inserter = SampleInserter(DB_CONFIG, no_mysql=no_mysql)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(2)   # Allow at most 2 concurrent ESP32 connections
    log.info(f"Listening on {host}:{port}")
    log.info(f"Captures dir: {captures_dir}")
    log.info(f"MySQL: {'DISABLED (stdout only)' if no_mysql else DB_CONFIG['host']}")

    try:
        while True:
            conn, addr = srv.accept()
            t = threading.Thread(
                target=handle_client,
                args=(conn, addr, resolver, inserter),
                name=f"client-{addr[0]}",
                daemon=True,
            )
            t.start()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        srv.close()
        inserter.stop()


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Pi 5 receiver for Bullet R2 Tier 1 exhaust gas sensor"
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--captures-dir", default=DEFAULT_CAPTURES_DIR)
    parser.add_argument("--no-mysql", action="store_true",
                        help="Skip MySQL inserts; log samples to stdout")
    parser.add_argument("--schema", action="store_true",
                        help="Print schema DDL to stdout and exit")
    args = parser.parse_args()

    if args.schema:
        print(SCHEMA_DDL)
        return

    serve(args.host, args.port, args.captures_dir, args.no_mysql)


if __name__ == "__main__":
    main()
