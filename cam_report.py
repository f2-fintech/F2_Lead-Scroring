"""
cam_report.py - quick check of how many tickets and CAMs we have.

Put this file in the SAME folder as server.py and main.py, then run:

    python cam_report.py                     -> summary numbers only
    python cam_report.py --month 2026-09     -> only tickets from that month
    python cam_report.py --from-month 2026-01 --to-month 2026-07
                                             -> a range of months (both included)
    python cam_report.py --csv report.csv    -> also save a full list to a file
    python cam_report.py --ticket 12345      -> check ONE ticket
    python cam_report.py --unique-application -> count each application id once

Only READS from the OMS database. It never changes anything.

"Has CAM" here means: the ticket has at least one .xlsx attachment
(the same rule your scoring code uses in find_cam_attachment).
"""
import argparse
import csv
from datetime import date

from db import mysql_client


def month_range(month_str):
    """'2026-09' -> (2026-09-01, 2026-10-01)"""
    year, month = map(int, month_str.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def get_ticket_rows(start=None, end=None):
    """
    One row per ticket, with the count of .xlsx (CAM) files.
    """
    conn = mysql_client.get_connection()
    cursor = conn.cursor(dictionary=True)

    where = ""
    params = []
    if start and end:
        where = "WHERE ca.application_date >= %s AND ca.application_date < %s"
        params = [start, end]

    query = f"""
        SELECT
            t.id AS ticket_id,
            ca.id AS application_id,
            ca.provider AS provider_name,
            c.name AS customer_name,
            t.status AS ticket_status,
            ca.application_date,
            SUM(CASE WHEN ta.attachment LIKE '%%.xlsx' THEN 1 ELSE 0 END) AS cam_files
        FROM tickets t
        JOIN customer_application ca ON ca.id = t.customer_application_id
        JOIN customer c ON c.id = ca.customer_id
        LEFT JOIN `ticket-activities` ta
               ON ta.ticket_id = t.id AND ta.attachment IS NOT NULL
        {where}
        GROUP BY t.id, ca.id, ca.provider, c.name, t.status, ca.application_date
        ORDER BY ca.application_date DESC
    """
    cursor.execute(query, params)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    for r in rows:
        r["cam_files"] = int(r["cam_files"] or 0)
        r["has_cam"] = "Yes" if r["cam_files"] > 0 else "No"
    return rows


def group_by_application(rows):
    """
    Turn ticket rows into ONE row per application_id.
    An application counts as "has CAM" if ANY of its tickets has a CAM.
    """
    apps = {}
    for r in rows:
        key = r["application_id"]
        if key not in apps:
            apps[key] = {
                "application_id": key,
                "customer_name": r["customer_name"],
                "provider_name": r["provider_name"],
                "application_date": r["application_date"],
                "ticket_ids": [],
                "ticket_count": 0,
                "cam_files": 0,
            }
        a = apps[key]
        a["ticket_ids"].append(str(r["ticket_id"]))
        a["ticket_count"] += 1
        a["cam_files"] += r["cam_files"]

    result = list(apps.values())
    for a in result:
        a["ticket_ids"] = ", ".join(a["ticket_ids"])
        a["has_cam"] = "Yes" if a["cam_files"] > 0 else "No"
    return result


def print_summary(rows, label, unit="tickets"):
    total = len(rows)
    with_cam = sum(1 for r in rows if r["cam_files"] > 0)
    without_cam = total - with_cam

    print("=" * 44)
    print(f"  CAM REPORT  ({label})")
    print("=" * 44)
    print(f"  Total {unit:<22}: {total}")
    print(f"  {unit} WITH a CAM".ljust(30) + f": {with_cam}  (ready to score)")
    print(f"  {unit} WITHOUT a CAM".ljust(30) + f": {without_cam}")
    print(f"  Total CAM files            : {sum(r['cam_files'] for r in rows)}")
    print("=" * 44)


def save_csv(rows, path, unique_application=False):
    if unique_application:
        columns = [
            "application_id", "customer_name", "provider_name",
            "application_date", "ticket_count", "ticket_ids",
            "has_cam", "cam_files",
        ]
    else:
        columns = [
            "ticket_id", "application_id", "customer_name", "provider_name",
            "ticket_status", "application_date",
            "has_cam", "cam_files",
        ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved full list ({len(rows)} rows) to {path}")


def main():
    parser = argparse.ArgumentParser(description="Count tickets and CAMs")
    parser.add_argument("--month", help="Only this month, like 2026-09")
    parser.add_argument("--from-month", help="Start month (included), like 2026-01")
    parser.add_argument("--to-month", help="End month (included), like 2026-07")
    parser.add_argument("--csv", help="Save the full ticket list to this file")
    parser.add_argument("--ticket", type=int, help="Check one ticket id")
    parser.add_argument("--unique-application", action="store_true",
                        help="Count each application id once (not each ticket)")
    args = parser.parse_args()

    start = end = None
    label = "all tickets"
    if args.month:
        start, end = month_range(args.month)
        label = args.month
    elif args.from_month or args.to_month:
        if not (args.from_month and args.to_month):
            print("Please give BOTH --from-month and --to-month.")
            return
        start, _ = month_range(args.from_month)
        _, end = month_range(args.to_month)
        if start >= end:
            print("--from-month must not be after --to-month.")
            return
        label = f"{args.from_month} to {args.to_month}"

    rows = get_ticket_rows(start, end)

    if args.ticket:
        match = [r for r in rows if r["ticket_id"] == args.ticket]
        if not match:
            print(f"Ticket {args.ticket} not found.")
            return
        r = match[0]
        print(f"Ticket {r['ticket_id']} - {r['customer_name']} ({r['provider_name']})")
        print(f"  Has CAM   : {r['has_cam']}  ({r['cam_files']} file(s))")
        for url in mysql_client.find_cam_attachment(r["ticket_id"]):
            print(f"  CAM file  : {url}")
        return

    if args.unique_application:
        rows = group_by_application(rows)
        print_summary(rows, label, unit="applications")
    else:
        print_summary(rows, label)

    if args.csv:
        save_csv(rows, args.csv, unique_application=args.unique_application)


if __name__ == "__main__":
    main()