"""
disbursed_report.py - Excel list of customers whose TOTAL disbursed amount
is 60 lakh or more.

Put this file in the SAME folder as server.py and main.py, then run:

    python disbursed_report.py                    -> 1 Jan 2025 till 31 Dec 2025
    python disbursed_report.py --min-lakh 50      -> use 50 lakh instead of 60
    python disbursed_report.py --from-date 2026-01-01 --to-date 2026-06-30
    python disbursed_report.py --out my_report.xlsx

How it works:
  1. Takes every ticket whose status is "Disbursed" and that has a
     disbursed_amount, for applications made in the date range.
  2. Groups the tickets by PHONE NUMBER (last 10 digits, so "+91 98765 43210"
     and "9876543210" are the same person). The same person may have 5
     tickets with 5 different lenders - their disbursed amounts are ADDED.
  3. Keeps the phone numbers whose total is >= the limit (default 60 lakh).
  4. Writes an Excel file:
        Sheet "Customers" - Name, Phone, City, Total Disbursed, No. of loans, Lenders
        Sheet "Tickets"   - every ticket behind those totals (to double-check)

Only READS from the databases. It never changes anything.

The date range uses the application date (customer_application.application_date),
the same date the other reports use.
"""
import argparse
import re
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from db import mysql_client

LAKH = 100000

# Names we look for in the OMS "customer" table if it has a city column.
CITY_COLUMN_CANDIDATES = ("city", "current_city", "city_name", "district", "location")


# ---------------------------------------------------------------- helpers

def phone_key(phone):
    """Digits only, last 10 digits. None if there is no phone."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-10:] if digits else None


def to_number(value):
    """disbursed_amount -> float (handles None, '45,00,000', Decimal)."""
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def find_city_column(cursor):
    """Returns the name of a city-like column in the customer table, or None."""
    try:
        cursor.execute("SHOW COLUMNS FROM customer")
        columns = {row[0].lower(): row[0] for row in cursor.fetchall()}
    except Exception:
        return None
    for candidate in CITY_COLUMN_CANDIDATES:
        if candidate in columns:
            return columns[candidate]
    return None


# ---------------------------------------------------------------- reading data

def fetch_disbursed_tickets(start, end):
    """
    One row per DISBURSED ticket with an amount, for applications from
    `start` (included) up to `end` (NOT included).
    """
    conn = mysql_client.get_connection()
    try:

        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"""
            SELECT
                t.id AS ticket_id,
                t.status AS ticket_status,
                t.disbursed_amount,
                ca.id AS application_id,
                ca.provider AS lender,
                ca.application_date,
                c.id AS customer_id,
                c.name AS customer_name,
                c.contact AS customer_phone,
                (SELECT ci.city FROM customer_info ci WHERE ci.customer_id = c.id LIMIT 1) AS city
            FROM tickets t
            JOIN customer_application ca ON ca.id = t.customer_application_id
            JOIN customer c ON c.id = ca.customer_id
            WHERE LOWER(TRIM(t.status)) = 'disbursed'
              AND t.disbursed_amount IS NOT NULL
              AND LOWER(TRIM(ca.loan_category)) = 'secured'
              AND ca.application_date >= %s
              AND ca.application_date < %s
            ORDER BY ca.application_date DESC, t.id DESC
            """,
            [start, end],
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()

    for r in rows:
        r["amount"] = to_number(r["disbursed_amount"])
    return [r for r in rows if r["amount"] > 0]


def fetch_cam_cities():
    """
    Backup source for the city: the city read from each customer's CAM,
    saved in our results database. Returns {phone_key: city}, newest first.
    Returns {} if the results database is not reachable.
    """
    cities = {}
    try:
        conn = mysql_client.get_results_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT customer_phone,
                   JSON_UNQUOTE(JSON_EXTRACT(cam_fields, '$.city')) AS city
            FROM lead_scores
            WHERE cam_fields IS NOT NULL
            ORDER BY id DESC
            """
        )
        for row in cursor.fetchall():
            key = phone_key(row["customer_phone"])
            city = (row["city"] or "").strip()
            if key and city and city.lower() != "null" and key not in cities:
                cities[key] = city
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"(Could not read cities from the results database: {e})")
    return cities
def find_cam_link(tickets):
    """First CAM (.xlsx) link found on any of the customer's tickets. Blank if none."""
    for t in tickets:
        try:
            urls = mysql_client.find_cam_attachment(t["ticket_id"])
        except Exception:
            continue
        if urls:
            return urls[0]
    return ""

# ---------------------------------------------------------------- combining

def combine_by_phone(tickets, min_amount, cam_cities=None):
    """
    Adds up disbursed amounts per phone number and keeps the people whose
    total is >= min_amount. Tickets must be newest first (the newest name /
    city is used). Returns (customers, tickets_kept).
    """
    cam_cities = cam_cities or {}
    groups = {}
    for t in tickets:
        key = phone_key(t["customer_phone"])
        if not key:
            continue  # no phone number - cannot combine, so it is left out
        g = groups.setdefault(key, {
            "phone": key, "name": None, "city": None,
            "total": 0.0, "count": 0, "lenders": [], "tickets": [],
        })
        g["total"] += t["amount"]
        g["count"] += 1
        g["tickets"].append(t)
        if not g["name"] and t.get("customer_name"):
            g["name"] = t["customer_name"]
        if not g["city"] and t.get("city"):
            g["city"] = str(t["city"]).strip()
        lender = (t.get("lender") or "").strip()
        if lender and lender not in g["lenders"]:
            g["lenders"].append(lender)

    customers = []
    for key, g in groups.items():
        if g["total"] < min_amount:
            continue
        if not g["city"]:
            g["city"] = cam_cities.get(key, "")
        customers.append(g)

    customers.sort(key=lambda g: g["total"], reverse=True)
    kept = [t for g in customers for t in g["tickets"]]
    return customers, kept


# ---------------------------------------------------------------- Excel

def write_excel(customers, path, start, end_inclusive, min_amount):
    wb = Workbook()

    ws = wb.active
    ws.title = "Customers"
    ws.append(["Customer Name", "Phone Number", "City",
               "Total Disbursed (Rs.)", "No. of Loans", "Lenders", "CAM Link"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for g in customers:
        ws.append([g["name"] or "", g["phone"], g["city"] or "",
                   g["total"], g["count"], ", ".join(g["lenders"]),
                   g.get("cam_link") or ""])
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
        row[0].number_format = "#,##0"
    for i, width in enumerate([26, 16, 18, 22, 13, 40], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Tickets")
    ws2.append(["Phone Number", "Customer Name", "Ticket ID", "Application ID",
                "Lender", "Application Date", "Disbursed Amount (Rs.)"])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
    for g in customers:
        for t in g["tickets"]:
            ws2.append([g["phone"], t.get("customer_name") or "", t["ticket_id"],
                        t["application_id"], t.get("lender") or "",
                        str(t["application_date"])[:10], t["amount"]])
    for row in ws2.iter_rows(min_row=2, min_col=7, max_col=7):
        row[0].number_format = "#,##0"
    for i, width in enumerate([16, 26, 11, 15, 22, 16, 22], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = width
    ws2.freeze_panes = "A2"

    wb.save(path)


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Customers with total disbursed >= limit")
    parser.add_argument("--min-lakh", type=float, default=60,
                        help="Minimum TOTAL disbursed, in lakh (default 60)")
    parser.add_argument("--from-date", help="Start date YYYY-MM-DD (default: 2025-01-01)")
    parser.add_argument("--to-date", help="End date YYYY-MM-DD, included (default: 2025-12-31)")
    parser.add_argument("--out", help="Excel file name to create")
    args = parser.parse_args()

    today = date.today()
    start = date.fromisoformat(args.from_date) if args.from_date else date(2025, 1, 1)
    end_inclusive = date.fromisoformat(args.to_date) if args.to_date else date(2025, 12, 31)
    if start > end_inclusive:
        print("--from-date must not be after --to-date.")
        return
    end_exclusive = end_inclusive + timedelta(days=1)
    min_amount = args.min_lakh * LAKH

    print(f"Looking at disbursed tickets from {start} to {end_inclusive} ...")
    tickets = fetch_disbursed_tickets(start, end_exclusive)
    print(f"Disbursed tickets found: {len(tickets)}")

    no_phone = sum(1 for t in tickets if not phone_key(t["customer_phone"]))
    if no_phone:
        print(f"Note: {no_phone} ticket(s) have no phone number and were left out.")

    cam_cities = fetch_cam_cities()
    customers, _ = combine_by_phone(tickets, min_amount, cam_cities)
    for g in customers:
        g["cam_link"] = find_cam_link(g["tickets"])
    out = args.out or f"disbursed_{args.min_lakh:g}L_{start:%Y%m%d}_to_{end_inclusive:%Y%m%d}.xlsx"
    write_excel(customers, out, start, end_inclusive, min_amount)

    print(f"Customers with total disbursed >= {args.min_lakh:g} lakh: {len(customers)}")
    print(f"Saved to {out}")


if __name__ == "__main__":
    main()