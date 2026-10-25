"""
mis_sync.py - reads the MIS Google Sheets and copies the CREDIT remarks and
OPS remarks onto the leads we already have stored in `lead_data`
(leads that are in OMS and have a readable CAM).

Nothing else from the MIS is stored. The sheets are read in memory, matched
to our leads, and only the two remarks (plus how the row was matched) are
written into lead_data:
    mis_credit_remark, mis_ops_remark, mis_match ('ticket_id' or 'phone')
MIS rows that match none of our leads are simply thrown away.

Two sheets (links kept in .env):
    MIS_UNSECURED_SHEET_URL   -> mis_type 'unsecured'
    MIS_SECURED_SHEET_URL     -> mis_type 'secured'
    GOOGLE_SERVICE_ACCOUNT_FILE -> path of the Google service-account key file
(each sheet must be a real Google Sheet, shared - view only is enough - with
the service account's email)

How a lead is matched to a MIS row:
    a. same TICKET ID (the MIS ticket id = the OMS ticket id)
    b. otherwise, phone number - only against MIS rows that have NO ticket id;
       if several, the one with the same lender and month (month taken from
       the tab name), else the latest.

Run by hand:   python mis_sync.py
Run it AFTER ingest.py (it only fills leads that are already stored).
"""
import argparse
import hashlib
import json
import os
import re
import time

from config import config
from db import mysql_client, lead_data

# normalised header text -> our field. Normalised = lowercase letters/digits only,
# so "CASE CREATED DATE" / "Case Create Date" / "CASE CREAT DATE" all work.
HEADER_ALIASES = {
    "ticket_id": ["ticketid", "ticketno", "ticketnumber", "ticketids", "ticket", "ticketidno"],
    "phone": ["phonenumber", "phoneno", "phone", "mobilenumber", "mobileno", "mobile", "contactnumber",
              "contactno", "contact", "customerphone", "customermobile", "phonenumbers", "mobilenumbers"],
    "customer_name": ["customername", "customer", "name"],
    "lender": ["lender", "lendor", "lendername"],
    "case_created_date": ["casecreateddate", "casecreatedate", "casecreatdate", "casecreated"],
    "last_updated_date": ["lastupdateddate", "lastupdated"],
    "approval_date": ["approvaldate"],
    "disbursed_date": ["disburseddate", "disbursaldate"],
    "status": ["status"],
    "loan_type": ["loantype"],
    "loan_category": ["loancategory"],
    "loan_amount": ["loanamount"],
    "state": ["locationstate", "state"],
    "city": ["city"],
    "manager": ["manager"],
    "tl_name": ["tlname", "tl"],
    "emp_name": ["empnamechhanelpartnername", "empnamechannelpartnername", "empname", "employeename"],
    "ops_remarks": ["opsremarks", "opsremark", "operationsremarks"],
    "credit_remarks": ["creditremarks", "creditremark"],
}
_ALIAS_TO_FIELD = {a: f for f, aliases in HEADER_ALIASES.items() for a in aliases}

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
          "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "september": 9,
          "oct": 10, "nov": 11, "dec": 12}


# ---------------------------------------------------------------- pure helpers

def norm_header(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def map_headers(row):
    """Header row -> {field: column index}. Empty dict if it is not a lead header."""
    mapping = {}
    for idx, cell in enumerate(row):
        key = norm_header(cell)
        field = _ALIAS_TO_FIELD.get(key)
        if not field and key.startswith("empname"):   # "EMP NAME/CHANNEL PARTNER NAME" and its typos
            field = "emp_name"
        if not field and key.startswith("creditremark"):   # "CREDIT REMARKS / FEEDBACK", "Credit Remark 2" ...
            field = "credit_remarks"
        if not field and (key.startswith("opsremark") or key.startswith("operationsremark")):
            field = "ops_remarks"
        if field and field not in mapping:
            mapping[field] = idx
    return mapping


def find_header(values, max_scan=30):
    """(header_row_index, mapping) of the first row that looks like the lead header."""
    for i, row in enumerate(values[:max_scan]):
        mapping = map_headers(row)
        if "phone" in mapping and ("ticket_id" in mapping or "status" in mapping):
            return i, mapping
        # a tab whose phone column is named oddly: a ticket id column plus a
        # status or a remark column is still clearly a lead tab
        if "ticket_id" in mapping and ("status" in mapping or "credit_remarks" in mapping
                                       or "ops_remarks" in mapping):
            return i, mapping
    return None, {}


def parse_tab_month(tab_name):
    """'SEPT 2026' / 'OCT 26' / 'JUNE 2026' -> (2026, 9) etc. None if no month found."""
    m = re.search(r"([A-Za-z]{3,9})\W*(\d{2,4})", str(tab_name or ""))
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return None
    year = int(m.group(2))
    return (year + 2000 if year < 100 else year), month


def parse_ticket_id(raw):
    """'12345', '12345.0', '#12345', ' 12,345 ' -> 12345.  None if not a ticket number."""
    if raw is None:
        return None
    m = re.fullmatch(r"\D*(\d[\d,]*)(?:\.0+)?\s*", str(raw))
    if not m:
        return None
    digits = m.group(1).replace(",", "")
    return int(digits) if digits else None


def _cell(row, mapping, field):
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return None
    value = str(row[idx]).strip()
    return value or None


def parse_row(row, mapping, mis_type, tab, row_number):
    """One sheet row -> dict for mis_rows (values kept exactly as written). None to skip."""
    rec = {f: _cell(row, mapping, f) for f in mapping}
    ticket_raw = rec.get("ticket_id")
    ticket_num = parse_ticket_id(ticket_raw)
    phone_raw = rec.get("phone")
    pkey = lead_data.phone_key(phone_raw)
    if not ticket_num and not pkey:
        return None  # nothing to match on

    if ticket_num:
        row_hash = f"t:{ticket_num}"
    else:
        basis = "|".join([tab or "", pkey or "", (rec.get("case_created_date") or ""),
                          (rec.get("lender") or "").lower()])
        row_hash = "p:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:38]

    out = {k: rec.get(k) for k in (
        "customer_name", "lender", "case_created_date", "last_updated_date", "approval_date",
        "disbursed_date", "status", "loan_type", "loan_category", "loan_amount", "state",
        "city", "manager", "tl_name", "emp_name", "ops_remarks", "credit_remarks")}
    out.update(mis_type=mis_type, sheet_tab=tab, row_number=row_number, row_hash=row_hash,
               ticket_id=ticket_raw, ticket_id_num=ticket_num, phone_raw=phone_raw,
               phone_key=pkey)
    return out


def _norm_lender(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _lender_matches(a, b):
    a, b = _norm_lender(a), _norm_lender(b)
    return bool(a and b and (a == b or (len(a) >= 3 and len(b) >= 3 and (a in b or b in a))))


# True = a lead is looked up ONLY in the MIS tab of its own month (the month of
# its OMS application date, e.g. a September lead -> the 'SEPT 2026' tab).
# False = look in every tab, newest first.
MONTH_ONLY = True


def _in_month(rows, lead_month):
    if not MONTH_ONLY or not lead_month:
        return rows
    return [r for r in rows if parse_tab_month(r["sheet_tab"]) == lead_month]


def choose_mis_rows(lead, by_ticket, by_phone, claimed=None):
    """
    Finds the MIS rows of ONE stored lead. Returns (rows, how) or ([], None).

      1. TICKET ID: a row in the lead's month tab with the same ticket id.
      2. otherwise PHONE (last 10 digits): a row in the lead's month tab with
         the same phone number. A row whose ticket id belongs to ANOTHER stored
         lead (`claimed`) is never used. Rows with the same lender are preferred.
      3. otherwise nothing - the remarks stay blank.
    """
    app_date = lead.get("application_date")
    lead_month = (app_date.year, app_date.month) if app_date else None

    rows = _in_month(by_ticket.get(lead.get("ticket_id"), []), lead_month)
    if not rows:
        # A ticket id belongs to ONE lead, so if the tab of its own month has no row
        # (the case was carried into a later tab) look in every tab.
        rows = by_ticket.get(lead.get("ticket_id"), [])
    if rows:
        return sorted(rows, key=lambda r: r["id"], reverse=True), "ticket_id"

    pk = lead.get("phone_key")
    provider = lead.get("provider_name")
    candidates = []
    for r in _in_month(by_phone.get(pk, []) if pk else [], lead_month):
        if not r["ticket_id_num"]:
            candidates.append(r)
        elif claimed is not None and r["ticket_id_num"] not in claimed:
            candidates.append(r)
    if not candidates and pk and MONTH_ONLY:
        # nothing in the lead's own month tab: same phone AND same lender in any tab
        for r in by_phone.get(pk, []):
            free = (not r["ticket_id_num"]) or (claimed is not None and r["ticket_id_num"] not in claimed)
            if free and _lender_matches(r["lender"], provider):
                candidates.append(r)
    if not candidates:
        return [], None

    best = [r for r in candidates if _lender_matches(r["lender"], provider)] or candidates
    return sorted(best, key=lambda r: r["id"], reverse=True), "phone"


def merge_remarks(rows):
    """
    `rows` newest first. A ticket carried over several tabs takes the LATEST
    non-empty credit remark and the LATEST non-empty ops remark, so an empty
    cell in the newest tab does not hide a remark written in an older tab.
    """
    credit = next((r["credit_remarks"] for r in rows if r.get("credit_remarks")), None)
    ops = next((r["ops_remarks"] for r in rows if r.get("ops_remarks")), None)
    return credit, ops


def choose_mis_row(lead, by_ticket, by_phone, claimed=None):
    """Kept for older callers: the single newest matching row."""
    rows, how = choose_mis_rows(lead, by_ticket, by_phone, claimed)
    return (rows[0], how) if rows else (None, None)


# ---------------------------------------------------------------- Google Sheets

def _open_sheet(url):
    try:
        import gspread
    except ImportError:
        raise RuntimeError("Please run:  pip install gspread")
    if not config.GOOGLE_SERVICE_ACCOUNT_FILE:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_FILE is not set in .env")
    client = gspread.service_account(filename=config.GOOGLE_SERVICE_ACCOUNT_FILE)
    return client.open_by_url(url)


def _read_tab(ws, retries=3):
    """All cell values of one tab. Waits and tries again if Google says 'too many requests'."""
    for attempt in range(retries):
        try:
            return ws.get_all_values()
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                time.sleep(30 * (attempt + 1))
                continue
            raise


def _read_all_tabs(sh, worksheets, log=print):
    """
    {tab title: rows} using ONE Google request for all tabs (reading tab by tab
    can hit Google's 'too many requests' limit). Returns None if that does not
    work, and the caller then reads tab by tab.
    """
    try:
        ranges = ["'" + ws.title.replace("'", "''") + "'" for ws in worksheets]
        response = sh.values_batch_get(ranges)
        value_ranges = response.get("valueRanges", [])
        if len(value_ranges) != len(worksheets):
            return None
        return {ws.title: vr.get("values", []) for ws, vr in zip(worksheets, value_ranges)}
    except Exception as e:
        log(f"  (reading all tabs at once did not work: {e} - reading tab by tab)")
        return None


def _retry(fn, what, log=print, tries=5):
    """Runs fn(); if Google is busy or the network drops, waits and tries again."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            text = str(e)
            if "GOOGLE_SERVICE_ACCOUNT_FILE" in text or "pip install" in text:
                raise                      # a setup problem - waiting will not fix it
            last = e
            log(f"  {what} failed ({text[:120]}) - attempt {attempt}/{tries}")
            if attempt < tries:
                time.sleep((30 if "429" in text else 10) * attempt)
    raise last


# The last MIS read that worked is kept in a file. If Google is down the next
# time, that saved copy is used, so the remarks step never leaves you empty-handed.
MIS_CACHE_DIR = "cache"


def _cache_file(mis_type):
    return os.path.join(MIS_CACHE_DIR, f"mis_{mis_type}.json")


def _save_cache(mis_type, rows):
    try:
        os.makedirs(MIS_CACHE_DIR, exist_ok=True)
        with open(_cache_file(mis_type), "w", encoding="utf-8") as f:
            json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}, f, default=str)
    except Exception:
        pass                               # a cache problem must never stop the sync


def _load_cache(mis_type):
    try:
        with open(_cache_file(mis_type), encoding="utf-8") as f:
            data = json.load(f)
        return data.get("rows") or [], data.get("saved_at")
    except Exception:
        return [], None


def read_one_sheet(url, mis_type, log=print):
    """
    Reads every lead tab of one sheet. Returns a list of MIS rows (dicts) -
    nothing is saved. Tabs are listed newest first; we go oldest -> newest so
    a ticket that is carried into a later tab ends up with the LATEST tab's
    remarks.
    """
    sh = _retry(lambda: _open_sheet(url), "opening the sheet", log)
    worksheets = _retry(lambda: list(sh.worksheets()), "listing the tabs", log)
    log(f"[{mis_type}] sheet '{sh.title}': {len(worksheets)} tab(s)")
    all_tabs = _read_all_tabs(sh, worksheets, log=log)
    out = []
    for ws in reversed(worksheets):
        values = all_tabs[ws.title] if all_tabs is not None else _read_tab(ws)
        header_idx, mapping = find_header(values)
        if header_idx is None:
            first_rows = [" | ".join(str(c).strip() for c in row if str(c).strip())[:140]
                          for row in values[:6] if any(str(c).strip() for c in row)][:3]
            log(f"  skipped tab '{ws.title}' (not a lead tab)"
                + (f" - first rows: {first_rows}" if first_rows else " - the tab is empty"))
            continue
        for f, label in (("credit_remarks", "CREDIT remarks"), ("ops_remarks", "OPS remarks")):
            if f not in mapping:
                log(f"  WARNING tab '{ws.title}': no {label} column found - every lead matched "
                    f"to this tab will show no {label}. Header row seen: "
                    f"{[str(c).strip() for c in values[header_idx] if str(c).strip()][:20]}")
        count = 0
        for n, row in enumerate(values[header_idx + 1:], start=header_idx + 2):
            rec = parse_row(row, mapping, mis_type, ws.title, n)
            if rec:
                out.append(rec)
                count += 1
        log(f"  tab '{ws.title}': {count} row(s) read")
    with_ticket = sum(1 for r in out if r["ticket_id_num"])
    log(f"[{mis_type}] MIS rows read in total: {len(out)} ({with_ticket} have a ticket id)")
    return out


# ---------------------------------------------------------------- matching

def _load_leads():
    """Our stored leads that have a readable CAM (the only ones that get remarks)."""
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""SELECT id, ticket_id, phone_key, provider_name, application_date,
                             loan_class
                      FROM lead_data WHERE cam_status = 'ok'""")
    leads = cursor.fetchall()
    cursor.close()
    conn.close()
    return leads


def _attach_remarks(leads, mis_rows, log=print):
    """Writes the credit / ops remark of the matching MIS row onto each lead."""
    by_ticket, by_phone = {}, {}
    for r in mis_rows:
        if r["ticket_id_num"]:
            by_ticket.setdefault(r["ticket_id_num"], []).append(r)
        if r["phone_key"]:
            by_phone.setdefault(r["phone_key"], []).append(r)

    claimed = {l["ticket_id"] for l in leads if l.get("ticket_id")}
    counts = {"leads": len(leads), "by_ticket_id": 0, "by_phone": 0,
              "no_match": 0, "with_remarks": 0}
    updates, unmatched = [], []
    for lead in leads:
        rows, how = choose_mis_rows(lead, by_ticket, by_phone, claimed)
        if not rows:
            counts["no_match"] += 1
            if len(unmatched) < 5:
                unmatched.append(f"ticket {lead['ticket_id']} (phone {lead['phone_key']})")
            continue
        counts["by_ticket_id" if how == "ticket_id" else "by_phone"] += 1
        credit, ops = merge_remarks(rows)
        if credit or ops:
            counts["with_remarks"] += 1
        updates.append((how, credit, ops, lead["id"]))

    if updates:
        def _write():
            conn = mysql_client.get_results_connection()
            try:
                cursor = conn.cursor()
                cursor.executemany("""
                    UPDATE lead_data SET mis_match=%s,
                                         mis_credit_remark=COALESCE(%s, mis_credit_remark),
                                         mis_ops_remark=COALESCE(%s, mis_ops_remark),
                                         mis_row_id=NULL
                    WHERE id=%s
                """, updates)
                conn.commit()
                changed = cursor.rowcount
                cursor.close()
                return changed
            finally:
                conn.close()
        changed = _retry(_write, "saving the remarks", log, tries=3)
        log(f"Database UPDATE sent for {len(updates)} lead(s); rows changed: {changed}")

    log(f"Remarks attached: by ticket id={counts['by_ticket_id']}, "
        f"by phone={counts['by_phone']}, no MIS row found={counts['no_match']} "
        f"(of {counts['leads']} stored leads with a CAM); "
        f"{counts['with_remarks']} of the matched leads have a credit or ops remark")
    if unmatched:
        log("  examples of leads with no MIS row: " + ", ".join(unmatched))
    return counts


def _require_columns():
    """Stops with a clear message if lead_data lacks the MIS columns (otherwise the UPDATE fails quietly in a log)."""
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT column_name FROM information_schema.columns
                      WHERE table_schema = %s AND table_name = 'lead_data'""",
                   (config.RESULTS_DB_DATABASE,))
    have = {r[0].lower() for r in cursor.fetchall()}
    cursor.close()
    conn.close()
    missing = [c for c in ("mis_match", "mis_credit_remark", "mis_ops_remark", "mis_row_id",
                           "phone_key", "cam_status", "loan_class") if c not in have]
    if missing:
        raise RuntimeError(
            "lead_data is missing column(s): " + ", ".join(missing) + ". "
            "Your DB user could not add them. Run in MySQL:  "
            + "  ".join(f"ALTER TABLE lead_data ADD COLUMN {c} "
                        + ("TEXT" if c.endswith("remark") else "VARCHAR(20)") + ";"
                        for c in missing))


def sync_mis(log=print):
    """
    Reads the MIS sheets and fills the remarks on stored leads.
    NEVER raises - it always returns a dict with a "status":
        "ok"          read live from Google, remarks attached
        "saved_copy"  Google could not be read, the last good copy was used
        "failed"      nothing could be read (the "message" says why)
        "nothing"     no stored leads yet, nothing to do
    """
    try:
        return _sync_mis(log)
    except Exception as e:
        import traceback
        log(f"MIS sync problem: {e}")
        log(traceback.format_exc())
        return {"status": "failed", "message": str(e)}


def _sync_mis(log=print):
    lead_data.ensure_lead_data_table()
    _require_columns()
    leads = _load_leads()
    log(f"Stored leads with a CAM: {len(leads)}")
    total = {"status": "ok", "message": None}
    if not leads:
        log("No stored leads with a CAM yet - run ingest.py first. Nothing to do.")
        total["status"] = "nothing"
        return total

    all_rows, notes = [], []
    # Only the UNSECURED MIS is read. Every stored lead (secured or unsecured in
    # OMS) is looked up in it. To read the secured sheet again, add it back here.
    for mis_type, url in (("unsecured", config.MIS_UNSECURED_SHEET_URL),):
        if not url:
            log(f"[{mis_type}] no sheet link in .env - skipped")
            continue
        try:
            rows = read_one_sheet(url, mis_type, log=log)
            if not rows:
                raise RuntimeError("the sheet was read but no lead rows were found in it")
            _save_cache(mis_type, rows)
        except Exception as e:
            log(f"[{mis_type}] could not be read live: {e}")
            rows, saved_at = _load_cache(mis_type)
            if rows:
                note = f"[{mis_type}] Google could not be read ({e}); used the copy saved on {saved_at}"
                log(note)
                notes.append(note)
                total["status"] = "saved_copy"
            else:
                total[mis_type] = 0
                notes.append(f"[{mis_type}] could not be read and no saved copy exists ({e})")
                continue
        total[mis_type] = len(rows)
        all_rows.extend(rows)

    if not all_rows:
        msg = ("NO MIS rows could be read - so no remarks were attached this time (the leads are "
               "safe). Look for the lines above, or run: python mis_diagnose.py")
        log(msg)
        total["status"] = "failed"
        total["message"] = "; ".join(notes) or msg
        return total
    for i, r in enumerate(all_rows, start=1):   # later rows = newer tabs
        r["id"] = i
    total["linked"] = _attach_remarks(leads, all_rows, log=log)
    if notes:
        total["message"] = "; ".join(notes)
    return total


def link_mis_to_leads(log=print):
    """
    Kept only so older code (ingest.py, server.py) can still import it.
    MIS rows are no longer saved in our database, so there is nothing to
    link from - the remarks are attached by sync_mis() / `python mis_sync.py`.
    """
    log("(MIS remarks are attached by:  python mis_sync.py)")
    return {}


if __name__ == "__main__":
    argparse.ArgumentParser(
        description="Read the MIS Google Sheets and store the credit / ops remarks on stored leads"
    ).parse_args()
    sync_mis()