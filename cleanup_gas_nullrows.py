"""
cleanup_gas_nullrows.py
-----------------------
Nightly cleanup: delete rows WHERE roast_id IS NULL older than
--retention-days from a gas sensor table.

Idle-time and calibration samples are intentionally written with
roast_id = NULL.  Without periodic pruning they accumulate at ~86 400
rows/day and bloat the table.  This script is the Stage 4 cleanup;
the Stage 5 cutover doc notes switching --table from the dev table to
the live table.

Deletes in batches to avoid a single long table lock.

Usage:
    # Systemd service reads /etc/roast.env via EnvironmentFile.
    # When running manually:
    python cleanup_gas_nullrows.py --env-file /etc/roast.env

    # Dry-run (shows count, deletes nothing):
    python cleanup_gas_nullrows.py --env-file /etc/roast.env --dry-run

    # Tune retention or target table:
    python cleanup_gas_nullrows.py --env-file /etc/roast.env \
        --retention-days 14 \
        --table exhaust_gas_tier1_dev
"""

import argparse
import logging
import os
import time

import mysql.connector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gas-cleanup")

DEFAULT_TABLE          = "exhaust_gas_tier1_dev"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_BATCH_SIZE     = 1000


def load_env_file(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()


def db_connect():
    return mysql.connector.connect(
        host=os.environ.get("ROAST_DB_HOST", "192.168.0.87"),
        port=int(os.environ.get("ROAST_DB_PORT", "3306")),
        user=os.environ.get("ROAST_DB_USER", "root"),
        password=os.environ.get("ROAST_DB_PASS", ""),
        database=os.environ.get("ROAST_DB_NAME", "roastdata"),
    )


def run_cleanup(table, retention_days, batch_size, dry_run):
    cutoff = time.time() - retention_days * 86400

    conn = db_connect()
    cur  = conn.cursor()

    cur.execute(
        f"SELECT COUNT(*) FROM {table} "
        "WHERE roast_id IS NULL AND pi_timestamp < %s",
        (cutoff,),
    )
    eligible = cur.fetchone()[0]

    log.info(
        f"Table: {table}  retention: {retention_days}d  "
        f"cutoff: {cutoff:.0f}  eligible: {eligible}"
    )

    if dry_run:
        log.info(f"[dry-run] would delete {eligible} rows — no changes made")
        cur.close()
        conn.close()
        return

    if eligible == 0:
        log.info("Nothing to delete")
        cur.close()
        conn.close()
        return

    total_deleted = 0
    while True:
        cur.execute(
            f"DELETE FROM {table} "
            "WHERE roast_id IS NULL AND pi_timestamp < %s "
            "LIMIT %s",
            (cutoff, batch_size),
        )
        deleted = cur.rowcount
        conn.commit()
        total_deleted += deleted
        log.info(f"Deleted batch of {deleted} rows (total so far: {total_deleted})")
        if deleted < batch_size:
            break

    log.info(f"Done: deleted {total_deleted} rows from {table}")
    cur.close()
    conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Delete old NULL-roast_id rows from gas sensor table"
    )
    parser.add_argument("--env-file", default=None,
                        help="Systemd-style env file (reads without shell expansion)")
    parser.add_argument("--table", default=DEFAULT_TABLE,
                        help=f"Table to clean (default: {DEFAULT_TABLE})")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help=f"Keep NULL rows younger than this many days "
                             f"(default: {DEFAULT_RETENTION_DAYS})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Rows per DELETE batch (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count eligible rows and exit without deleting")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    run_cleanup(args.table, args.retention_days, args.batch_size, args.dry_run)


if __name__ == "__main__":
    main()
