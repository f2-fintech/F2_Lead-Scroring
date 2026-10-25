"""
clear_stored_data.py - deletes the data stored by ingest.py / mis_sync.py so
you can start again from zero.

Put this file in the SAME folder as server.py, then run:

    python clear_stored_data.py                   -> clears lead_data, lead_tradelines, mis_rows
    python clear_stored_data.py --include-scores  -> ALSO clears lead_scores (the old
                                                     table the dashboard buckets use now)

It asks you to type YES before it deletes anything.

It NEVER touches:
  - OMS (the main company database - this script does not even connect to it)
  - app_users (dashboard logins)
  - lead_remarks (remarks typed in the dashboard)
  - loan_type_map (your secured / unsecured choices)
"""
import argparse

from db import mysql_client

# Order matters: tradelines belong to leads, so they go first.
NEW_TABLES = ["lead_tradelines", "lead_data", "mis_rows"]
OLD_TABLES = ["lead_scores"]


def count_rows(cursor, table):
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]
    except Exception:
        return None  # table does not exist (yet)


def main():
    parser = argparse.ArgumentParser(description="Delete stored lead data")
    parser.add_argument("--include-scores", action="store_true",
                        help="also clear the old lead_scores table")
    args = parser.parse_args()

    tables = NEW_TABLES + (OLD_TABLES if args.include_scores else [])

    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()

    print("These tables in YOUR results database will be emptied:")
    existing = []
    for t in tables:
        n = count_rows(cursor, t)
        if n is None:
            print(f"  {t:<16} (does not exist - skipped)")
        else:
            print(f"  {t:<16} {n} row(s)")
            existing.append(t)

    if not existing:
        print("Nothing to delete.")
        cursor.close()
        conn.close()
        return

    answer = input("\nType YES to delete all of this (anything else cancels): ").strip()
    if answer != "YES":
        print("Cancelled. Nothing was deleted.")
        cursor.close()
        conn.close()
        return

    for t in existing:
        cursor.execute(f"DELETE FROM {t}")
        deleted = cursor.rowcount
        conn.commit()
        try:  # start the ids from 1 again (needs permission - fine if it fails)
            cursor.execute(f"ALTER TABLE {t} AUTO_INCREMENT = 1")
            conn.commit()
        except Exception:
            pass
        print(f"  cleared {t}: {deleted} row(s) deleted")

    cursor.close()
    conn.close()
    print("Done. You can now run:  python ingest.py --month 2026-09")


if __name__ == "__main__":
    main()