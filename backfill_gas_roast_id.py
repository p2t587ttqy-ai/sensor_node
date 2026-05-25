#!/usr/bin/env python3
"""
backfill_gas_roast_id.py — link exhaust_gas_tier1 rows to a roast_id after pcap import.

During a roast the resolver writes roast_id=NULL because roast_meta doesn't have a row
yet (the pcap hasn't been imported). Run this script after import to backfill the link.

Usage:
    python3 backfill_gas_roast_id.py --roast-id 9
    python3 backfill_gas_roast_id.py          # defaults to most recent roast with pcap_epoch_offset

Dry-run is always shown first; script asks for confirmation before writing.
"""

import argparse
import os
import sys
import mysql.connector


# Gas data captured this far before charge belongs to this roast (preheat baseline).
PREHEAT_BUFFER_S  = 25 * 60   # 25 minutes before charge
# Gas data captured this far after drop still belongs to this roast (cooling tray exhaust).
COOLDOWN_BUFFER_S = 15 * 60   # 15 minutes after drop

GAS_TABLES = ["exhaust_gas_tier1", "exhaust_gas_tier1_dev"]


def db_config():
    return {
        "host":     os.environ.get("ROAST_DB_HOST", "localhost"),
        "port":     int(os.environ.get("ROAST_DB_PORT", 3306)),
        "user":     os.environ.get("ROAST_DB_USER", "root"),
        "password": os.environ.get("ROAST_DB_PASS", ""),
        "database": os.environ.get("ROAST_DB_NAME", "roastdata"),
    }


def connect():
    return mysql.connector.connect(**db_config())


def get_roast(conn, roast_id):
    cur = conn.cursor(dictionary=True)
    if roast_id is not None:
        cur.execute(
            "SELECT roast_id, roast_name, pcap_epoch_offset "
            "FROM roast_meta WHERE roast_id = %s",
            (roast_id,),
        )
    else:
        cur.execute(
            "SELECT roast_id, roast_name, pcap_epoch_offset "
            "FROM roast_meta "
            "WHERE pcap_epoch_offset IS NOT NULL "
            "ORDER BY pcap_epoch_offset DESC LIMIT 1"
        )
    row = cur.fetchone()
    cur.close()
    return row


def get_drop_sec(conn, roast_id):
    """Return max elapsed_sec from roast_data (seconds from charge to drop)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(elapsed_sec) FROM roast_data WHERE roast_id = %s",
        (roast_id,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row and row[0] is not None else 0


def table_exists(conn, table):
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    exists = cur.fetchone()[0] > 0
    cur.close()
    return exists


def dry_run(conn, table, roast_id, t_start, t_end):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        f"SELECT COUNT(*) AS cnt, "
        f"FROM_UNIXTIME(MIN(pi_timestamp)) AS first_ts, "
        f"FROM_UNIXTIME(MAX(pi_timestamp)) AS last_ts "
        f"FROM `{table}` "
        f"WHERE roast_id IS NULL AND pi_timestamp BETWEEN %s AND %s",
        (t_start, t_end),
    )
    result = cur.fetchone()
    cur.close()
    return result


def check_conflicts(conn, table, roast_id, t_start, t_end):
    """Return count of rows in window already owned by a different roast_id."""
    cur = conn.cursor()
    cur.execute(
        f"SELECT COUNT(*) FROM `{table}` "
        f"WHERE roast_id IS NOT NULL AND roast_id != %s "
        f"  AND pi_timestamp BETWEEN %s AND %s",
        (roast_id, t_start, t_end),
    )
    count = cur.fetchone()[0]
    cur.close()
    return count


def do_update(conn, table, roast_id, t_start, t_end):
    cur = conn.cursor()
    cur.execute(
        f"UPDATE `{table}` SET roast_id = %s "
        f"WHERE roast_id IS NULL AND pi_timestamp BETWEEN %s AND %s",
        (roast_id, t_start, t_end),
    )
    updated = cur.rowcount
    conn.commit()
    cur.close()
    return updated


def main():
    parser = argparse.ArgumentParser(description="Backfill exhaust_gas roast_id after pcap import.")
    parser.add_argument("--roast-id", type=int, default=None,
                        help="roast_id to backfill (default: most recent imported roast)")
    parser.add_argument("--preheat-buffer", type=int, default=PREHEAT_BUFFER_S,
                        help=f"seconds before charge to include (default {PREHEAT_BUFFER_S})")
    parser.add_argument("--cooldown-buffer", type=int, default=COOLDOWN_BUFFER_S,
                        help=f"seconds after drop to include (default {COOLDOWN_BUFFER_S})")
    parser.add_argument("--yes", action="store_true",
                        help="skip confirmation prompt")
    args = parser.parse_args()

    conn = connect()

    roast = get_roast(conn, args.roast_id)
    if not roast:
        print("ERROR: no matching roast found in roast_meta")
        sys.exit(1)
    if roast["pcap_epoch_offset"] is None:
        print(f"ERROR: roast_id={roast['roast_id']} has no pcap_epoch_offset — import the pcap first")
        sys.exit(1)

    charge_ts = float(roast["pcap_epoch_offset"])
    drop_sec  = get_drop_sec(conn, roast["roast_id"])
    t_start   = charge_ts - args.preheat_buffer
    t_end     = charge_ts + drop_sec + args.cooldown_buffer

    print(f"\nRoast:   [{roast['roast_id']}] {roast['roast_name']}")
    print(f"Charge:  {charge_ts:.3f}  (+{args.preheat_buffer//60} min preheat buffer)")
    print(f"Drop:    {charge_ts + drop_sec:.3f}  ({drop_sec}s after charge)")
    print(f"Window:  {t_start:.3f} → {t_end:.3f}  (+{args.cooldown_buffer//60} min cooldown)")
    print()

    any_work = False

    for table in GAS_TABLES:
        if not table_exists(conn, table):
            print(f"  {table}: table not found, skipping")
            continue

        result    = dry_run(conn, table, roast["roast_id"], t_start, t_end)
        conflicts = check_conflicts(conn, table, roast["roast_id"], t_start, t_end)

        print(f"  {table}:")
        print(f"    NULL rows in window : {result['cnt']}")
        if result["cnt"] > 0:
            print(f"    Time range          : {result['first_ts']} → {result['last_ts']}")
        if conflicts > 0:
            print(f"    WARNING: {conflicts} rows in window already belong to a different roast_id")
        print()

        if result["cnt"] > 0:
            any_work = True

    if not any_work:
        print("Nothing to backfill.")
        conn.close()
        return

    if not args.yes:
        answer = input("Apply backfill? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            conn.close()
            return

    print()
    for table in GAS_TABLES:
        if not table_exists(conn, table):
            continue
        updated = do_update(conn, table, roast["roast_id"], t_start, t_end)
        print(f"  {table}: updated {updated} rows → roast_id={roast['roast_id']}")

    print("\nDone.")
    conn.close()


if __name__ == "__main__":
    main()
