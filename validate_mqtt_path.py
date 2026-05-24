"""
validate_mqtt_path.py
---------------------
Stage 3 validation harness: diffs the MQTT path (exhaust_gas_tier1_dev)
against the TCP path (exhaust_gas_tier1) for a given roast or time window.

This is the cutover gate.  Run it after each instrumented roast while both
paths are active.  Criterion for cutover (Stage 5): three consecutive clean
roasts with PASS verdicts, including at least one with preheat data and one
with a firmware-glitch gap.

What is checked:
  1. Row parity        — same set of esp_uptime_ms values in both tables
  2. Sensor values     — co2_ppm, co_ppm, co2_rate, co2_rh, co2_temp,
                         co_age_s, co_co2_ratio, rssi_dbm must match within
                         --value-tolerance (default 0.0 — exact equality).
                         Both paths receive the same ESP32 JSON payload so
                         differences here indicate a parsing bug.
  3. Arrival timestamp — |tcp.pi_timestamp - mqtt.pi_timestamp| must be
                         within --ts-tolerance seconds (default 2.0).
                         The two transports take slightly different paths so
                         sub-second differences are expected and normal.
  4. roast_id          — both paths must resolve to the same roast_id for
                         each sample.  Disagreements indicate the resolver's
                         pcap-freshness or window logic diverged between runs.

Join key: esp_uptime_ms (ESP32 millis() at sample time — same value in both
tables for the same physical sample, regardless of transport).

Usage:
    # Last 60 minutes (default):
    python validate_mqtt_path.py --env-file /etc/roast.env

    # Specific roast (queries TCP rows for that roast_id; matches MQTT by
    # esp_uptime_ms across the same time window):
    python validate_mqtt_path.py --env-file /etc/roast.env --roast-id 8

    # Explicit time window:
    python validate_mqtt_path.py --env-file /etc/roast.env --window 120

    # Show every matched row, not just discrepancies:
    python validate_mqtt_path.py --env-file /etc/roast.env --verbose

    # Relax timestamp tolerance (e.g. if broker was under load):
    python validate_mqtt_path.py --env-file /etc/roast.env --ts-tolerance 5.0
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import mysql.connector

# ============================================================
#  Tables
# ============================================================

TABLE_TCP  = "exhaust_gas_tier1"
TABLE_MQTT = "exhaust_gas_tier1_dev"

# Fields compared for value equality
VALUE_FIELDS = [
    "co2_ppm",
    "co2_rate",
    "co2_temp",
    "co2_rh",
    "co_ppm",
    "co_age_s",
    "co_co2_ratio",
    "rssi_dbm",
]


# ============================================================
#  Env-file loader (no shell expansion — same as systemd)
# ============================================================

def load_env_file(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


# ============================================================
#  DB helpers
# ============================================================

def db_connect():
    return mysql.connector.connect(
        host=os.environ.get("ROAST_DB_HOST", "192.168.0.87"),
        port=int(os.environ.get("ROAST_DB_PORT", "3306")),
        user=os.environ.get("ROAST_DB_USER", "root"),
        password=os.environ.get("ROAST_DB_PASS", ""),
        database=os.environ.get("ROAST_DB_NAME", "roastdata"),
    )


def fetch_rows(cur, table, where_clause, params):
    """Return a dict keyed by esp_uptime_ms → row dict."""
    cols = ["esp_uptime_ms", "roast_id", "pi_timestamp"] + VALUE_FIELDS
    col_list = ", ".join(cols)
    cur.execute(
        f"SELECT {col_list} FROM {table} WHERE {where_clause} "
        f"AND esp_uptime_ms IS NOT NULL",
        params,
    )
    result = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        key = d["esp_uptime_ms"]
        if key in result:
            # Duplicate esp_uptime_ms in one table (shouldn't happen, but handle it)
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(d)
        else:
            result[key] = d
    return result


# ============================================================
#  Comparison
# ============================================================

def floats_equal(a, b, tol):
    """Compare two possibly-None float values within tolerance."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def compare(tcp_rows, mqtt_rows, ts_tolerance, value_tolerance, verbose):
    """
    Returns (passed: bool, report_lines: list[str]).
    """
    lines = []
    failures = []

    all_keys = sorted(set(tcp_rows) | set(mqtt_rows))

    only_tcp  = sorted(k for k in all_keys if k not in mqtt_rows)
    only_mqtt = sorted(k for k in all_keys if k not in tcp_rows)
    both      = sorted(k for k in all_keys if k in tcp_rows and k in mqtt_rows)

    ts_diffs = []
    value_mismatches = []
    roast_id_disagree = []

    for key in both:
        t = tcp_rows[key]
        m = mqtt_rows[key]

        # Timestamp check
        ts_diff = abs(t["pi_timestamp"] - m["pi_timestamp"])
        ts_diffs.append(ts_diff)

        row_ok = True

        if ts_diff > ts_tolerance:
            failures.append(
                f"  esp_uptime_ms={key}: timestamp diff {ts_diff:.3f}s > "
                f"tolerance {ts_tolerance}s  "
                f"(tcp={t['pi_timestamp']:.3f} mqtt={m['pi_timestamp']:.3f})"
            )
            row_ok = False

        # Value checks
        for field in VALUE_FIELDS:
            tv = t[field]
            mv = m[field]
            if not floats_equal(tv, mv, value_tolerance):
                value_mismatches.append(
                    f"  esp_uptime_ms={key} field={field}: "
                    f"tcp={tv} mqtt={mv} diff={_diff_str(tv, mv)}"
                )
                row_ok = False

        # roast_id check
        if t["roast_id"] != m["roast_id"]:
            roast_id_disagree.append(
                f"  esp_uptime_ms={key}: "
                f"tcp.roast_id={t['roast_id']} mqtt.roast_id={m['roast_id']}"
            )
            row_ok = False

        if verbose:
            status = "OK" if row_ok else "MISMATCH"
            ts_d = t["pi_timestamp"]
            lines.append(
                f"  [{status}] uptime={key}  "
                f"co2={t['co2_ppm']}  co={t['co_ppm']}  "
                f"roast={t['roast_id']}  ts_diff={ts_diff*1000:.1f}ms"
            )

    # ---- Summary ----
    n_tcp  = len(tcp_rows)
    n_mqtt = len(mqtt_rows)
    n_both = len(both)

    lines += [
        f"TCP  rows : {n_tcp}",
        f"MQTT rows : {n_mqtt}",
        f"Matched   : {n_both}  (joined on esp_uptime_ms)",
        f"Only TCP  : {len(only_tcp)}  "
        + ("(sample: " + str(only_tcp[:3]) + ")" if only_tcp else ""),
        f"Only MQTT : {len(only_mqtt)}  "
        + ("(sample: " + str(only_mqtt[:3]) + ")" if only_mqtt else ""),
    ]

    if ts_diffs:
        mean_ms = 1000 * sum(ts_diffs) / len(ts_diffs)
        max_ms  = 1000 * max(ts_diffs)
        over    = sum(1 for d in ts_diffs if d > ts_tolerance)
        lines.append(
            f"Timestamps: mean={mean_ms:.1f}ms  max={max_ms:.1f}ms  "
            f"over_tolerance={over}"
        )
    else:
        lines.append("Timestamps: no matched rows to compare")

    lines.append(f"Value mismatches     : {len(value_mismatches)}")
    lines.append(f"roast_id disagreements: {len(roast_id_disagree)}")

    if value_mismatches:
        lines.append("\nValue mismatch detail:")
        lines += value_mismatches

    if roast_id_disagree:
        lines.append("\nroast_id disagreement detail:")
        lines += roast_id_disagree

    if failures:
        lines.append("\nTimestamp failures:")
        lines += failures

    if only_tcp:
        lines.append(f"\nMissing from MQTT ({len(only_tcp)} rows):")
        lines += [f"  esp_uptime_ms={k}" for k in only_tcp[:20]]
        if len(only_tcp) > 20:
            lines.append(f"  ... and {len(only_tcp)-20} more")

    if only_mqtt:
        lines.append(f"\nExtra in MQTT not in TCP ({len(only_mqtt)} rows):")
        lines += [f"  esp_uptime_ms={k}" for k in only_mqtt[:20]]
        if len(only_mqtt) > 20:
            lines.append(f"  ... and {len(only_mqtt)-20} more")

    passed = (
        len(only_tcp) == 0
        and len(only_mqtt) == 0
        and len(value_mismatches) == 0
        and len(roast_id_disagree) == 0
        and len(failures) == 0
    )
    return passed, lines


def _diff_str(a, b):
    if a is None or b is None:
        return "None vs value"
    try:
        return f"{abs(float(a)-float(b)):.6g}"
    except (TypeError, ValueError):
        return "?"


# ============================================================
#  Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Stage 3 validation harness: diff MQTT vs TCP gas sensor paths"
    )
    parser.add_argument("--env-file", default=None,
                        help="Path to systemd-style env file (e.g. /etc/roast.env)")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--roast-id", type=int, default=None,
                       help="Compare rows for a specific roast_id (queries TCP table, "
                            "matches MQTT by esp_uptime_ms in same time window)")
    group.add_argument("--window", type=float, default=60.0, metavar="MINUTES",
                       help="Compare rows from the last N minutes (default: 60)")

    parser.add_argument("--ts-tolerance", type=float, default=2.0, metavar="SECONDS",
                        help="Max acceptable |tcp_ts - mqtt_ts| in seconds (default: 2.0)")
    parser.add_argument("--value-tolerance", type=float, default=0.0, metavar="DELTA",
                        help="Max acceptable absolute difference for float fields "
                             "(default: 0.0 — exact equality)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print every matched row, not just discrepancies")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    conn = db_connect()
    cur  = conn.cursor()

    now = time.time()

    # ---- Determine query window ----
    if args.roast_id is not None:
        # Find time range of TCP rows for this roast, then query both tables
        cur.execute(
            f"SELECT MIN(pi_timestamp), MAX(pi_timestamp) "
            f"FROM {TABLE_TCP} WHERE roast_id = %s",
            (args.roast_id,),
        )
        row = cur.fetchone()
        if row is None or row[0] is None:
            print(f"No TCP rows found for roast_id={args.roast_id}")
            sys.exit(1)
        t_min, t_max = row
        # Pad ±30 s to catch MQTT rows that arrived slightly outside
        t_min -= 30
        t_max += 30
        window_desc = f"roast_id={args.roast_id}  " \
                      f"({_ts(t_min+30)} — {_ts(t_max-30)})"
        tcp_where  = "roast_id = %s"
        tcp_params = (args.roast_id,)
        mqtt_where = "pi_timestamp BETWEEN %s AND %s"
        mqtt_params = (t_min, t_max)
    else:
        window_s = args.window * 60.0
        t_min    = now - window_s
        t_max    = now
        window_desc = f"last {args.window:.0f} min  ({_ts(t_min)} — {_ts(t_max)})"
        tcp_where  = "pi_timestamp BETWEEN %s AND %s"
        tcp_params = (t_min, t_max)
        mqtt_where = tcp_where
        mqtt_params = tcp_params

    # ---- Fetch ----
    tcp_rows  = fetch_rows(cur, TABLE_TCP,  tcp_where,  tcp_params)
    mqtt_rows = fetch_rows(cur, TABLE_MQTT, mqtt_where, mqtt_params)

    cur.close()
    conn.close()

    # ---- Compare ----
    passed, report = compare(
        tcp_rows, mqtt_rows,
        ts_tolerance=args.ts_tolerance,
        value_tolerance=args.value_tolerance,
        verbose=args.verbose,
    )

    # ---- Print report ----
    width = 60
    print("=" * width)
    print("  Stage 3 Validation Report")
    print(f"  Window  : {window_desc}")
    print(f"  TCP     : {TABLE_TCP}")
    print(f"  MQTT    : {TABLE_MQTT}")
    print(f"  ts_tol  : {args.ts_tolerance}s   val_tol: {args.value_tolerance}")
    print("=" * width)
    for line in report:
        print(line)
    print("=" * width)
    verdict = "PASS" if passed else "FAIL"
    print(f"  VERDICT : {verdict}")
    print("=" * width)

    sys.exit(0 if passed else 1)


def _ts(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


if __name__ == "__main__":
    main()
