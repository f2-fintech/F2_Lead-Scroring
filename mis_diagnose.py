"""
mis_diagnose.py - finds out WHY the MIS remarks are not syncing.

Put it in the SAME folder as server.py / mis_sync.py and run:

    python mis_diagnose.py

It only READS (OMS is never touched, nothing is written anywhere). It checks,
in order, every step the sync depends on and prints what it finds:

  1. .env settings and the Google service account
  2. the results database (columns, how many leads have a CAM, how many
     already have MIS remarks, month by month)
  3. each MIS Google Sheet: can it be opened, which tabs, is the header row
     recognised, are the Ticket ID / Phone / Credit / Ops columns found,
     how many rows
  4. a DRY RUN of the matching: how many stored leads would get a remark,
     and for those that would not, the reason
  5. a short verdict

Copy everything it prints and send it back.
"""
import json
import os
import sys
import time
from collections import Counter, defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import config
from db import mysql_client
import mis_sync as ms

REQUIRED_DB_COLUMNS = ["mis_match", "mis_credit_remark", "mis_ops_remark",
                       "phone_key", "cam_status", "ticket_id", "loan_class"]
WANTED_FIELDS = ["ticket_id", "phone", "status", "lender", "credit_remarks", "ops_remarks"]

findings = []          # (level, text) used for the final verdict


def hr(title):
    print("\n" + "=" * 72 + "\n" + title + "\n" + "=" * 72)


def note(level, text):
    findings.append((level, text))


def short(v, n=28):
    v = str(v).replace("\n", " ").strip()
    return v if len(v) <= n else v[:n - 1] + "~"


# ---------------------------------------------------------------- 1. settings

def check_settings():
    hr("1. SETTINGS (.env)")
    for name in ("MIS_UNSECURED_SHEET_URL", "MIS_SECURED_SHEET_URL"):
        val = getattr(config, name, None)
        print(f"  {name:<26}: {'set' if val else 'NOT SET'}")
        if not val:
            note("warn", f"{name} is not set - that sheet is skipped by the sync.")
    path = config.GOOGLE_SERVICE_ACCOUNT_FILE
    print(f"  GOOGLE_SERVICE_ACCOUNT_FILE: {path}")
    if not path or not os.path.exists(path):
        print("    -> FILE NOT FOUND (path is relative to the folder you run the server from)")
        note("bad", "Google service account key file was not found.")
        return
    try:
        email = json.load(open(path, encoding="utf-8")).get("client_email")
        print(f"  service account email      : {email}")
        print("    -> BOTH sheets must be shared with exactly this email (Viewer is enough).")
    except Exception as e:
        print(f"    -> could not read the key file: {e}")
        note("bad", "Service account key file is unreadable.")
    try:
        import gspread
        print(f"  gspread version            : {gspread.__version__}")
    except ImportError:
        print("  gspread is NOT installed -> run: pip install gspread")
        note("bad", "gspread is not installed.")


# ---------------------------------------------------------------- 2. database

def check_database():
    hr("2. RESULTS DATABASE (lead_data)")
    state = {"cam_ok": 0, "with_mis": 0, "columns_ok": False}
    try:
        conn = mysql_client.get_results_connection()
    except Exception as e:
        print(f"  cannot connect to the results database: {e}")
        note("bad", "Cannot connect to the results database.")
        return state
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT column_name AS c FROM information_schema.columns
                       WHERE table_schema=%s AND table_name='lead_data'""",
                    (config.RESULTS_DB_DATABASE,))
        have = {r["c"].lower() for r in cur.fetchall()}
        if not have:
            print("  table lead_data does not exist -> run ingest first")
            note("bad", "lead_data table does not exist.")
            return state
        missing = [c for c in REQUIRED_DB_COLUMNS if c not in have]
        if missing:
            print(f"  MISSING COLUMNS in lead_data: {', '.join(missing)}")
            note("bad", "lead_data lacks MIS columns (" + ", ".join(missing) + ") - the UPDATE "
                 "that stores remarks fails. Your DB user probably cannot ALTER TABLE.")
            return state
        state["columns_ok"] = True
        print("  required columns             : all present")

        cur.execute("""
            SELECT DATE_FORMAT(application_date, '%Y-%m') AS month,
                   COUNT(*) AS total,
                   SUM(cam_status = 'ok') AS cam_ok,
                   SUM(mis_match IS NOT NULL) AS matched,
                   SUM(mis_credit_remark IS NOT NULL AND TRIM(mis_credit_remark) <> '') AS credit,
                   SUM(mis_ops_remark IS NOT NULL AND TRIM(mis_ops_remark) <> '') AS ops
            FROM lead_data GROUP BY month ORDER BY month DESC LIMIT 8""")
        rows = cur.fetchall()
        print(f"\n  {'month':<9}{'leads':>7}{'CAM ok':>8}{'MIS matched':>13}{'credit rmk':>12}{'ops rmk':>9}")
        for r in rows:
            print(f"  {str(r['month']):<9}{int(r['total']):>7}{int(r['cam_ok'] or 0):>8}"
                  f"{int(r['matched'] or 0):>13}{int(r['credit'] or 0):>12}{int(r['ops'] or 0):>9}")
            state["cam_ok"] += int(r["cam_ok"] or 0)
            state["with_mis"] += int(r["matched"] or 0)
        if not rows:
            note("bad", "lead_data is empty - run ingest.py first.")
        elif state["cam_ok"] == 0:
            note("bad", "No stored lead has cam_status='ok' - the MIS sync only fills leads with a readable CAM.")
    except Exception as e:
        print(f"  database check failed: {e}")
        note("bad", f"Database check failed: {e}")
    finally:
        cur.close()
        conn.close()
    return state


# ---------------------------------------------------------------- 3. sheets

def analyse_tab(title, values):
    """Prints what we see in one tab; returns the parsed MIS rows (list)."""
    month = ms.parse_tab_month(title)
    header_idx, mapping = ms.find_header(values)
    if header_idx is None:
        print(f"  [X] tab '{title}': header NOT recognised -> this tab is SKIPPED by the sync")
        best = max(((len(ms.map_headers(r)), i) for i, r in enumerate(values[:10])), default=(0, 0))
        print(f"      (needs a 'Phone' column AND a 'Ticket ID' or 'Status' column in the first 10 rows)")
        for i, r in enumerate(values[:5]):
            print(f"      row {i + 1}: " + " | ".join(short(c, 18) for c in r[:14]))
        note("warn", f"Tab '{title}' was skipped: header not recognised.")
        return []
    missing = [f for f in WANTED_FIELDS if f not in mapping]
    rows = []
    for n, row in enumerate(values[header_idx + 1:], start=header_idx + 2):
        rec = ms.parse_row(row, mapping, "?", title, n)
        if rec:
            rows.append(rec)
    with_ticket = sum(1 for r in rows if r["ticket_id_num"])
    with_credit = sum(1 for r in rows if r.get("credit_remarks"))
    with_ops = sum(1 for r in rows if r.get("ops_remarks"))
    flag = "OK " if not missing else "!! "
    print(f"  [{flag}] tab '{title}' (month {month}): header on row {header_idx + 1}, "
          f"{len(rows)} usable rows, {with_ticket} with ticket id, "
          f"{with_credit} with credit remark, {with_ops} with ops remark")
    if missing:
        print(f"        columns NOT found: {', '.join(missing)}")
        print("        header cells: " + " | ".join(short(c, 18) for c in values[header_idx][:30]))
        if "credit_remarks" in missing or "ops_remarks" in missing:
            note("bad", f"Tab '{title}': credit/ops remark column not recognised - header text changed?")
    elif rows and not (with_credit or with_ops):
        note("warn", f"Tab '{title}' has rows but NO credit/ops remark text in them.")
    return rows


def read_sheets():
    hr("3. MIS GOOGLE SHEETS")
    all_rows = []
    for mis_type, url in (("unsecured", config.MIS_UNSECURED_SHEET_URL),
                          ("secured", config.MIS_SECURED_SHEET_URL)):
        print(f"\n--- {mis_type} sheet ---")
        if not url:
            print("  no link in .env - skipped")
            continue
        t0 = time.time()
        try:
            sh = ms._open_sheet(url)
            worksheets = list(sh.worksheets())
        except Exception as e:
            print(f"  [X] CANNOT OPEN THE SHEET: {type(e).__name__}: {e}")
            text = str(e).lower()
            if "403" in text or "permission" in text:
                print("      -> share the sheet with the service account email (see section 1)")
            elif "not supported for this document" in text or "failedprecondition" in text:
                print("      -> the file is an uploaded Excel, not a real Google Sheet. "
                      "Use File > Save as Google Sheets")
            elif "404" in text or "not found" in text:
                print("      -> the link in .env is wrong")
            note("bad", f"{mis_type} sheet cannot be opened: {type(e).__name__}: {str(e)[:150]}")
            continue
        print(f"  opened '{sh.title}' in {time.time() - t0:.1f}s: {len(worksheets)} tab(s): "
              + ", ".join(f"'{w.title}'" for w in worksheets))
        tabs = ms._read_all_tabs(sh, worksheets, log=print)
        if tabs is None:
            print("  (batch read failed - reading tab by tab)")
        count_before = len(all_rows)
        for ws in reversed(worksheets):             # same order as the real sync
            try:
                values = tabs[ws.title] if tabs is not None else ms._read_tab(ws)
            except Exception as e:
                print(f"  [X] tab '{ws.title}' could not be read: {e}")
                note("bad", f"Tab '{ws.title}' could not be read: {str(e)[:120]}")
                continue
            for r in analyse_tab(ws.title, values):
                r["mis_type"] = mis_type
                all_rows.append(r)
        if len(all_rows) == count_before:
            note("bad", f"{mis_type} sheet: not a single usable row was read.")
    for i, r in enumerate(all_rows, start=1):
        r["id"] = i
    return all_rows


# ---------------------------------------------------------------- 4. dry run

def dry_run(mis_rows):
    hr("4. MATCHING DRY RUN (nothing is written)")
    try:
        leads = ms._load_leads()
    except Exception as e:
        print(f"  cannot load leads: {e}")
        note("bad", f"Cannot load leads: {e}")
        return 0
    print(f"  stored leads with a readable CAM: {len(leads)}")
    print(f"  MIS rows read from the sheets   : {len(mis_rows)}")
    if not leads or not mis_rows:
        return 0

    by_ticket, by_phone = {}, {}
    for r in mis_rows:
        if r["ticket_id_num"]:
            by_ticket.setdefault(r["ticket_id_num"], []).append(r)
        if r["phone_key"]:
            by_phone.setdefault(r["phone_key"], []).append(r)
    stored_tickets = {l["ticket_id"] for l in leads if l.get("ticket_id")}

    chooser = getattr(ms, "choose_mis_rows", None)

    def choose(lead):
        if chooser:
            return chooser(lead, by_ticket, by_phone, stored_tickets)
        row, how = ms.choose_mis_row(lead, by_ticket, by_phone)
        return ([row] if row else []), how

    per_month = defaultdict(Counter)
    reasons = Counter()
    examples = defaultdict(list)
    matched_total = 0
    for lead in leads:
        d = lead.get("application_date")
        month = f"{d.year}-{d.month:02d}" if d else "unknown"
        rows, how = choose(lead)
        if rows:
            matched_total += 1
            per_month[month][how] += 1
            has_remark = any(r.get("credit_remarks") or r.get("ops_remarks") for r in rows)
            per_month[month]["with_remark"] += 1 if has_remark else 0
            continue
        per_month[month]["none"] += 1
        pk = lead.get("phone_key")
        phone_rows = by_phone.get(pk, []) if pk else []
        if not pk:
            why = "lead has no phone number stored"
        elif not phone_rows:
            why = "neither ticket id nor phone found anywhere in the MIS"
        elif all(r["ticket_id_num"] for r in phone_rows):
            foreign = [r for r in phone_rows if r["ticket_id_num"] not in stored_tickets]
            why = ("phone IS in the MIS, but only on rows carrying a DIFFERENT ticket id "
                   + ("(not one of our stored tickets either)" if foreign else "(another stored lead's ticket)"))
        else:
            why = "phone in MIS but sheet type (secured/unsecured) differs from the lead"
        reasons[why] += 1
        if len(examples[why]) < 3:
            examples[why].append(f"ticket {lead['ticket_id']}, phone {pk}, {month}, "
                                 f"{lead.get('loan_class')}, {short(lead.get('provider_name'), 20)}")

    print(f"\n  {'month':<9}{'by ticket':>10}{'by phone':>10}{'no match':>10}{'with remark':>13}")
    for month in sorted(per_month, reverse=True):
        c = per_month[month]
        print(f"  {month:<9}{c['ticket_id']:>10}{c['phone']:>10}{c['none']:>10}{c['with_remark']:>13}")
    print(f"\n  WOULD BE MATCHED: {matched_total} of {len(leads)} leads")
    if reasons:
        print("\n  why the others have no match:")
        for why, n in reasons.most_common():
            print(f"   - {n:>5} x {why}")
            for ex in examples[why]:
                print(f"            e.g. {ex}")

    sample = sorted(stored_tickets)[:3] + sorted(stored_tickets)[-3:]
    mis_sample = sorted(by_ticket)[:3] + sorted(by_ticket)[-3:]
    print(f"\n  sample of OUR ticket ids : {sample}")
    print(f"  sample of MIS ticket ids : {mis_sample}")
    print(f"  ticket ids in BOTH       : {len(stored_tickets & set(by_ticket))}")

    if matched_total == 0:
        diff = [r for r in reasons if "DIFFERENT ticket" in r]
        if diff:
            note("bad", "Zero matches: the MIS ticket ids are NOT the same numbers as our OMS ticket ids, "
                 "and the phone fallback is blocked for rows that carry a ticket id.")
        else:
            note("bad", "Zero matches: neither ticket ids nor phone numbers line up.")
    return matched_total


# ---------------------------------------------------------------- 5. verdict

def verdict(db_state, would_match):
    hr("5. VERDICT")
    if not findings:
        print("  Nothing wrong found in reading / matching.")
    for level, text in findings:
        print(f"  [{'PROBLEM' if level == 'bad' else 'warning'}] {text}")
    if db_state.get("columns_ok") and would_match and not db_state.get("with_mis"):
        print("\n  Matching WORKS in this dry run, but the database has NO matched leads.")
        print("  -> the sync is not writing. Run:  python mis_sync.py   and read its output,")
        print("     or open logs/auto-ingest.log and search for 'MIS remarks skipped'.")
    elif db_state.get("with_mis"):
        print(f"\n  The database already holds MIS remarks for {db_state['with_mis']} lead(s).")
        print("  -> if you do not SEE them, the problem is the screen: dashboard.html reads the old")
        print("     lead_scores table (/api/leads/<id>), which has no MIS columns.")


def main():
    check_settings()
    db_state = check_database()
    rows = read_sheets()
    matched = dry_run(rows)
    verdict(db_state, matched)


if __name__ == "__main__":
    main()