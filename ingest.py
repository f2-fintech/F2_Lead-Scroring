"""
ingest.py - "Add leads to database".

For every lead logged in the chosen month / year it stores in `lead_data`:
  * the OMS fields (status, amount, loan type, dates, phone, email, city ...),
  * the CAM fields (income, FOIR, age, experience, profession,
    degree, CIBIL, loans, credit remark ...) plus one row per tradeline.
ONLY leads that have a CAM which can be read are stored. A lead with no CAM
attached (or whose CAM cannot be read) is NOT stored - it is only counted in
the log as "no CAM" / "CAM unreadable", and it is looked at again on the next run.
This applies to secured and unsecured leads alike.
It does NOT decide any bucket - allocation works from what is stored here.

Whether a lead is secured or unsecured (saved as loan_class): your
loan_type_map first (Home Loan, LAP, Business Loan, Auto Loan, Gold Loan ...
are secured), then OMS's own loan_category.

Existing loans found in the CAM are marked secured / unsecured by loan_rules.py
and saved with a clean bank name, a clean loan type and a size slab, so the
dashboard filters can use them.

Running again is cheap and safe: a CAM whose link has not changed is not
downloaded again (only OMS fields are refreshed); leads with no CAM yet are
retried; --force re-reads every CAM.

    python ingest.py --month 2026-09
    python ingest.py --year 2026
    python ingest.py --month 2026-09 --force
    python ingest.py --backfill     -> copy existing lead_scores into lead_data
                                       (no CAM downloads)
    python ingest.py --recompute-loans
                                    -> after changing loan_rules.py: re-work the loan
                                       columns of what is already stored (no CAM downloads)
"""
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from db import mysql_client, lead_data
from main import _import_first, extract_cam
import loan_rules

_FA_PATHS = ["analysis.financial_analysis", "financial_analysis",
             "extraction.financial_analysis", "scoring.financial_analysis",
             "db.financial_analysis"]
parse_date_of_birth = _import_first("parse_date_of_birth", _FA_PATHS)
compute_age = _import_first("compute_age", _FA_PATHS)

CHUNK = 500


# ---------------------------------------------------------------- progress + stop

class IngestCancelled(Exception):
    """Raised inside an ingest when the user pressed Stop."""


_cancel = threading.Event()
_progress_lock = threading.Lock()
_progress = {}


def request_cancel():
    """Ask the running ingest to stop (it stops after the CAM files being read right now)."""
    _cancel.set()


def clear_cancel():
    _cancel.clear()


def is_cancel_requested():
    return _cancel.is_set()


def reset_progress():
    with _progress_lock:
        _progress.clear()
        _progress.update(phase="starting", percent=0.0, chunk_index=0, chunk_total=0,
                         chunk_label=None, cams_done=0, cams_total=0, leads_total=0, live=None)


def update_progress(**fields):
    with _progress_lock:
        _progress.update(fields)


def get_progress():
    """What the dashboard shows while an ingest runs (a copy - safe to send as JSON)."""
    with _progress_lock:
        return dict(_progress)



# ---------------------------------------------------------------- CAM -> fields

def _is_secured_loan(loan_type, type_map=None):
    """Secured = Home Loan, LAP, Business Loan, Auto Loan, Gold Loan, Loan Against
    Securities, Secured Credit Card, Loan Against FD (see loan_rules.py)."""
    return loan_rules.is_secured_loan_type(loan_type, type_map)


def _num(value):
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def flatten_cam(cam: dict, type_map=None) -> dict:
    """The CAM values we keep as their own columns (for filtering / display)."""
    cam = cam or {}
    # Loans re-checked against TODAY: an ended loan (end date passed) is not running.
    loans = loan_rules.refresh_loans(cam.get("loans") or [])
    ongoing = [l for l in loans if not l.get("loan_completed")]

    age = cam.get("age_direct")
    if age is None:
        age = compute_age(parse_date_of_birth(cam.get("date_of_birth")))

    designation = " ".join(str(cam.get("profession") or "").split()).upper() or None
    # EMI of the loans that are still running TODAY (ended loans count as 0)
    emi_used = (sum((l.get("emi_amount") or 0) for l in loans) if loans
                else cam.get("existing_emi"))

    def text(key):
        v = cam.get(key)
        return str(v).strip() if v not in (None, "") else None

    return {
        "state": cam.get("state") or None,
        "cam_city": cam.get("city") or None,
        "designation": designation,
        "experience_years": cam.get("experience_years"),
        "age": age,
        "ongoing_loans": len(ongoing),
        "secured_loans": sum(1 for l in ongoing if _is_secured_loan(l.get("loan_type"), type_map)),
        "unsecured_loans": sum(1 for l in ongoing if not _is_secured_loan(l.get("loan_type"), type_map)),
        "monthly_income": cam.get("total_monthly_income"),
        "foir_percent": cam.get("foir_percent"),
        "existing_emi": emi_used,
        "credit_remark": text("credit_remark"),
        "highest_degree": text("highest_degree"),
        "cibil_score": cam.get("cibil_score"),
        "marital_status": text("marital_status"),
        "house_type": text("house_type"),
        "employment_type": text("employment_type"),
        "employment_source": text("employment_source"),   # "cam" or "derived"
        "registration_year": text("registration_year"),
        "enquiries_3m": _num(cam.get("enquiries_3m")),
        "enquiries_6m": _num(cam.get("enquiries_6m")),
        "family_profile": text("family_profile"),
        "abb": cam.get("abb"),
    }


def tradelines_from_cam(cam: dict, type_map=None) -> list:
    return [{
        "member_name": l.get("lender"),
        "bank_clean": loan_rules.clean_bank_name(l.get("lender")),
        "loan_type": l.get("loan_type"),
        "loan_type_clean": loan_rules.clean_loan_type(l.get("loan_type")),
        "slab": loan_rules.loan_slab(l.get("sanction_amount"), l.get("current_balance")),
        "is_secured": 1 if _is_secured_loan(l.get("loan_type"), type_map) else 0,
        "ownership": l.get("ownership"),
        "sanction_amount": l.get("sanction_amount"),
        "current_balance": l.get("current_balance"),
        "emi_amount": l.get("emi_amount"),
        "tenure_months": l.get("tenure_months"),
        "end_date": str(l.get("end_date")) if l.get("end_date") not in (None, "") else None,
        "loan_completed": 1 if l.get("loan_completed") else 0,
    } for l in loan_rules.refresh_loans(cam.get("loans") or [])]


def _cam_record(cam, oms_city, type_map=None):
    flat = flatten_cam(cam, type_map)
    cam_city = flat.pop("cam_city")
    flat["city"] = oms_city or cam_city          # OMS city first, CAM only as a backup
    return {**flat, "tradelines": tradelines_from_cam(cam, type_map)}


# ---------------------------------------------------------------- OMS reads

def fetch_oms_leads(start, end):
    """Leads applied in [start, end): one per (application, provider), newest first."""
    conn = mysql_client.get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT ca.id AS application_id, ca.customer_id, ca.provider AS provider_name,
               ca.loan_type, ca.loan_category, ca.amount, ca.application_date,
               c.name AS customer_name, c.contact AS customer_phone, c.email AS customer_email,
               (SELECT ci.city FROM customer_info ci WHERE ci.customer_id = c.id LIMIT 1) AS oms_city,
               t.id AS ticket_id, t.status AS ticket_status
        FROM customer_application ca
        JOIN customer c ON c.id = ca.customer_id
        JOIN tickets t ON t.customer_application_id = ca.id
        WHERE ca.application_date >= %s AND ca.application_date < %s
        ORDER BY ca.last_updated DESC
    """, (start, end))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    deduped = {}
    for r in rows:
        deduped.setdefault((r["application_id"], r["provider_name"]), r)
    return list(deduped.values())


def _oms_fields(lead, prior_disbursed, type_map):
    loan_type = (lead.get("loan_type") or "").strip() or None
    category = (lead.get("loan_category") or "").strip() or None
    city = " ".join(str(lead.get("oms_city") or "").split()) or None
    return {
        "application_id": lead["application_id"],
        "provider_name": lead["provider_name"],
        "ticket_id": lead.get("ticket_id"),
        "customer_id": lead.get("customer_id"),
        "customer_name": lead.get("customer_name"),
        "customer_phone": lead.get("customer_phone"),
        "customer_email": lead.get("customer_email"),
        "ticket_status": (lead.get("ticket_status") or "").strip() or None,   # as OMS gives it
        "loan_type": loan_type,
        "loan_category": category,
        "loan_class": lead_data.resolve_loan_class(loan_type, category, type_map),
        "amount": lead.get("amount"),
        "application_date": lead.get("application_date"),
        "prior_disbursed_amount": prior_disbursed,
        "city": city,
    }


def _prior_disbursed_bulk(leads):
    """
    {ticket_id: amount already disbursed to the same customer on OTHER tickets}.
    Counts tickets whose status is 'Disbursed' - same rule as
    mysql_client.fetch_customer_disbursed_amount, in a few queries.
    """
    customer_ids = sorted({l["customer_id"] for l in leads if l.get("customer_id") is not None})
    ticket_ids = sorted({l["ticket_id"] for l in leads if l.get("ticket_id") is not None})
    totals, own = {}, {}
    conn = mysql_client.get_connection()
    cursor = conn.cursor()
    try:
        for i in range(0, len(customer_ids), CHUNK):
            part = customer_ids[i:i + CHUNK]
            cursor.execute(f"""
                SELECT ca.customer_id, COALESCE(SUM(t.disbursed_amount), 0)
                FROM tickets t JOIN customer_application ca ON ca.id = t.customer_application_id
                WHERE ca.customer_id IN ({','.join(['%s'] * len(part))})
                  AND LOWER(t.status) = 'disbursed'
                GROUP BY ca.customer_id
            """, part)
            for cid, total in cursor.fetchall():
                totals[cid] = float(total or 0)
        for i in range(0, len(ticket_ids), CHUNK):
            part = ticket_ids[i:i + CHUNK]
            cursor.execute(f"""
                SELECT t.id, COALESCE(t.disbursed_amount, 0) FROM tickets t
                WHERE t.id IN ({','.join(['%s'] * len(part))}) AND LOWER(t.status) = 'disbursed'
            """, part)
            for tid, amt in cursor.fetchall():
                own[tid] = float(amt or 0)
    finally:
        cursor.close()
        conn.close()
    return {l["ticket_id"]: max(0.0, totals.get(l["customer_id"], 0.0) - own.get(l["ticket_id"], 0.0))
            for l in leads if l.get("ticket_id") is not None}


# ---------------------------------------------------------------- reading CAMs fast

# How many CAM files are downloaded and read at the same time.
# Higher = faster. If you ever see the CAM links failing or "429 / too many
# requests" in the log, lower it (1 = one at a time, like before).
CAM_WORKERS = 8


def _cam_urls_bulk(ticket_ids):
    """
    {ticket_id: [CAM urls, newest first]} for MANY tickets in a few queries
    (before, there was one database query for every single lead).
    """
    ids = sorted({t for t in ticket_ids if t is not None})
    out = {}
    if not ids:
        return out
    conn = mysql_client.get_connection()
    cursor = conn.cursor()
    try:
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            cursor.execute(f"""
                SELECT ticket_id, attachment
                FROM `ticket-activities`
                WHERE ticket_id IN ({','.join(['%s'] * len(part))})
                  AND attachment IS NOT NULL
                  AND attachment LIKE '%%.xlsx'
                ORDER BY created_at DESC
            """, part)
            for tid, url in cursor.fetchall():
                out.setdefault(tid, []).append(url)
    finally:
        cursor.close()
        conn.close()
    return out


def _read_first_cam(urls):
    """Runs inside a worker thread. Tries the CAM files newest first.
    Returns (cam, url, last_error). Never raises."""
    last_error = None
    for url in urls:
        try:
            return extract_cam(url), url, None
        except Exception as e:          # bad / blank / changed CAM - try the next file
            last_error = e
    return None, None, last_error


# ---------------------------------------------------------------- ingest

def ingest_range(start, end, force=False, log=print, leads=None, progress=None):
    """Stores every lead applied in [start, end) (end NOT included). Returns a counts dict.

    Only leads with a readable CAM are stored. A lead with no CAM (or an
    unreadable one) is only counted in the log - nothing is saved for it.
    """
    progress = progress or (lambda *a, **k: None)
    if leads is None:
        leads = fetch_oms_leads(start, end)
    log(f"Ingesting {start} .. {end}: {len(leads)} lead(s) found in OMS")
    counts = {"total": len(leads), "cam_read": 0, "refreshed": 0,
              "no_cam": 0, "cam_error": 0, "error": 0}
    if not leads:
        progress(1.0, counts=counts, cams_done=0, cams_total=0)
        return counts

    stored = lead_data.get_existing()
    prior = _prior_disbursed_bulk(leads)
    type_map = lead_data.load_loan_type_map()
    cam_urls = _cam_urls_bulk([l.get("ticket_id") for l in leads])   # ONE pass for all tickets

    # Step 1 - sort the leads: "only refresh the OMS fields" or "read the CAM".
    refresh, to_read = [], []
    for lead in leads:
        try:
            key = (lead["application_id"], lead["provider_name"])
            base = _oms_fields(lead, prior.get(lead["ticket_id"], 0.0), type_map)
            old = stored.get(key)
            old_ok = bool(old) and old["cam_status"] == "ok"
            urls = cam_urls.get(lead.get("ticket_id"), [])

            if old_ok and not force and (not urls or urls[0] == old["cam_url"]):
                refresh.append(base)                       # CAM unchanged
            elif not urls:
                if old_ok:
                    refresh.append(base)                   # keep what is already stored
                else:
                    counts["no_cam"] += 1                  # no CAM -> NOT stored
            else:
                to_read.append((base, urls, old_ok))
        except Exception as e:
            log(f"  ticket {lead.get('ticket_id')}: unexpected error: {e}")
            counts["error"] += 1

    # Step 2 - refresh the OMS fields of all unchanged leads in a few big updates.
    if refresh:
        try:
            lead_data.refresh_oms_fields_bulk(refresh)
            counts["refreshed"] += len(refresh)
        except Exception as e:
            log(f"  could not refresh {len(refresh)} lead(s): {e}")
            counts["error"] += len(refresh)
    log(f"  unchanged (OMS fields refreshed only): {len(refresh)}, "
        f"no CAM (skipped): {counts['no_cam']}, CAMs to read: {len(to_read)}")
    progress(0.15, counts=counts, cams_done=0, cams_total=len(to_read))

    # Step 3 - read the new / changed CAMs several at a time. Only the reading
    # is done in parallel; every database write happens here, one at a time.
    if to_read:
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, CAM_WORKERS)) as pool:
            futures = {pool.submit(_read_first_cam, urls): (base, old_ok)
                       for base, urls, old_ok in to_read}
            for fut in as_completed(futures):
                if _cancel.is_set():                        # Stop was pressed
                    for pending in futures:
                        pending.cancel()
                    raise IngestCancelled("Stopped by you")
                base, old_ok = futures[fut]
                done += 1
                try:
                    cam, cam_url, last_error = fut.result()
                    if cam is None:
                        if old_ok:
                            lead_data.refresh_oms_fields(base)
                            counts["refreshed"] += 1
                        else:
                            counts["cam_error"] += 1       # CAM unreadable -> NOT stored
                            log(f"  ticket {base.get('ticket_id')}: CAM could not be read "
                                f"({str(last_error)[:150]})")
                    else:
                        lead_data.upsert_lead({**base, **_cam_record(cam, base["city"], type_map),
                                               "cam_url": cam_url, "cam_status": "ok",
                                               "cam_error": None, "cam_fields": cam})
                        counts["cam_read"] += 1
                except Exception as e:
                    log(f"  ticket {base.get('ticket_id')}: unexpected error: {e}")
                    counts["error"] += 1
                progress(0.15 + 0.85 * done / len(to_read), counts=counts,
                         cams_done=done, cams_total=len(to_read))
                if done % 25 == 0:
                    log(f"  ...{done}/{len(to_read)} CAMs done")

    progress(1.0, counts=counts)

    log("Finished. "
        f"CAMs read and stored={counts['cam_read']}, refreshed only={counts['refreshed']}, "
        f"skipped - no CAM={counts['no_cam']}, skipped - CAM unreadable={counts['cam_error']}, "
        f"errors={counts['error']}")
    return counts


def _attach_mis_remarks(log=print):
    """
    After storing leads: read the MIS sheets and put the credit / ops remarks on them.
    NEVER raises. Returns {"status": "ok" | "saved_copy" | "failed", "message": ...}
    """
    log("--- MIS remarks step starting ---")
    try:
        from mis_sync import sync_mis
        result = sync_mis(log=log) or {}
        status, message = result.get("status", "ok"), result.get("message")
    except Exception as e:                 # a MIS problem must never undo the ingest
        import traceback
        log(f"(MIS remarks skipped: {e})")
        log(traceback.format_exc())
        status, message = "failed", str(e)
    log(f"--- MIS remarks step: {status}" + (f" ({message})" if message else "") + " ---")
    return {"status": status, "message": message}


# ---------------------------------------------------------------- dates

def _to_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _month_chunks(date_from, date_to):
    """Splits [date_from, date_to] (both days included) into pieces that each stay
    inside one month. Each piece is saved as its own record, so if a long run
    stops half-way you can see exactly which part finished."""
    chunks, cur = [], date_from
    while cur <= date_to:
        nxt = date(cur.year + (1 if cur.month == 12 else 0), cur.month % 12 + 1, 1)
        chunks.append((cur, min(date_to, nxt - timedelta(days=1))))
        cur = nxt
    return chunks


def ingest_dates(date_from, date_to, force=False, kind="manual", log=print, with_mis=True):
    """
    THE one way to ingest: every lead whose application date is from `date_from`
    to `date_to` (both days included; a date after today is cut back to today).

    Every month-sized piece is written to the `ingest_runs` table, so the
    dashboard can show from which date till which date data has been fetched.
    The MIS remarks are attached ONCE at the end.
    """
    date_from, date_to = _to_date(date_from), _to_date(date_to)
    date_to = min(date_to, date.today())
    if date_from > date_to:
        raise ValueError("'From' date must not be after 'To' date (and 'From' cannot be in the future).")
    lead_data.ensure_lead_data_table()
    clear_cancel()
    reset_progress()
    log(f"Ingest ({kind}): {date_from} to {date_to}")

    # Read the lead list of every month piece first, so the progress bar knows
    # how much work there is in total.
    chunks = _month_chunks(date_from, date_to)
    update_progress(phase="reading OMS", chunk_total=len(chunks))
    fetched = []
    for c_from, c_to in chunks:
        if _cancel.is_set():
            raise IngestCancelled("Stopped by you")
        fetched.append(fetch_oms_leads(c_from, c_to + timedelta(days=1)))
    total_units = sum(len(x) for x in fetched) or 1
    update_progress(leads_total=sum(len(x) for x in fetched))

    total, run_ids = {}, []
    done_units = 0
    for idx, (c_from, c_to) in enumerate(chunks):
        if _cancel.is_set():
            raise IngestCancelled("Stopped by you")
        chunk_leads = fetched[idx]
        units = len(chunk_leads)
        label = f"{c_from:%d %b %Y} to {c_to:%d %b %Y}"
        update_progress(phase="reading CAMs", chunk_index=idx + 1, chunk_label=label,
                        cams_done=0, cams_total=0)

        def _cb(fraction, counts=None, cams_done=None, cams_total=None,
                _before=done_units, _units=units):
            live = dict(total)
            for k, v in (counts or {}).items():
                live[k] = live.get(k, 0) + v
            fields = {"percent": round(97.0 * (_before + fraction * _units) / total_units, 1),
                      "live": live}
            if cams_done is not None:
                fields.update(cams_done=cams_done, cams_total=cams_total)
            update_progress(**fields)

        run_id = lead_data.start_ingest_run(kind, c_from, c_to)
        run_ids.append(run_id)
        try:
            counts = ingest_range(c_from, c_to + timedelta(days=1), force=force, log=log,
                                  leads=chunk_leads, progress=_cb)
        except IngestCancelled:
            lead_data.finish_ingest_run(run_id, "stopped", None, error="stopped by the user")
            log("Stopped by the user. Leads stored so far are kept.")
            raise
        except Exception as e:
            lead_data.finish_ingest_run(run_id, "failed", None, error=str(e))
            raise
        done_units += units
        # "done" only if no lead hit an unexpected error. A partly-done piece is
        # NOT shown as fetched, so you will see the gap and can run it again.
        lead_data.finish_ingest_run(run_id, "done" if counts["error"] == 0 else "partial", counts)
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v

    if with_mis:
        if _cancel.is_set():
            raise IngestCancelled("Stopped by you")
        update_progress(phase="attaching remarks", percent=97.0, live=dict(total))
        mis = _attach_mis_remarks(log)
        lead_data.set_run_mis(run_ids, mis["status"], mis["message"])
        total["mis_status"] = mis["status"]
    update_progress(phase="done", percent=100.0, live=dict(total))
    return total


def ingest_recent(months_back=0, force=False, log=print):
    """
    What the server runs by itself: the CURRENT month only (1st of the month up
    to today) when months_back=0. Older dates are fetched on purpose, from the
    calendar in the dashboard (or `python ingest.py --from ... --to ...`).
    """
    today = date.today()
    y, m = today.year, today.month
    for _ in range(months_back):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return ingest_dates(date(y, m, 1), today, force=force, kind="auto", log=log)


def ingest_period(month=None, year=None, force=False, log=print):
    """Ingest one month ("2026-09") or a whole year ("2026"), then link MIS remarks."""
    if month:
        start, end = lead_data.month_range(month)          # end is NOT included
        return ingest_dates(start, end - timedelta(days=1), force=force, log=log)
    if year:
        return ingest_dates(date(int(year), 1, 1), date(int(year), 12, 31), force=force, log=log)
    raise ValueError("Give a month (YYYY-MM) or a year (YYYY).")


# ---------------------------------------------------------------- backfill

def backfill_from_lead_scores(log=print):
    """
    Copies leads already in lead_scores (with their saved CAM data) into lead_data.
    No CAM downloads - the OMS fields come from a few batched queries.
    """
    import json
    lead_data.ensure_lead_data_table()

    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""SELECT application_id, provider_name, ticket_id, customer_name,
                             customer_phone, cam_url, cam_fields FROM lead_scores""")
    scored = cursor.fetchall()
    cursor.close()
    conn.close()
    log(f"lead_scores rows to copy: {len(scored)}")

    app_ids = sorted({r["application_id"] for r in scored})
    oms = {}
    conn = mysql_client.get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        for i in range(0, len(app_ids), CHUNK):
            part = app_ids[i:i + CHUNK]
            cursor.execute(f"""
                SELECT ca.id AS application_id, ca.customer_id, ca.provider AS provider_name,
                       ca.loan_type, ca.loan_category, ca.amount, ca.application_date,
                       c.name AS customer_name, c.contact AS customer_phone, c.email AS customer_email,
                       (SELECT ci.city FROM customer_info ci WHERE ci.customer_id = c.id LIMIT 1) AS oms_city,
                       t.id AS ticket_id, t.status AS ticket_status
                FROM customer_application ca
                JOIN customer c ON c.id = ca.customer_id
                JOIN tickets t ON t.customer_application_id = ca.id
                WHERE ca.id IN ({','.join(['%s'] * len(part))})
            """, part)
            for r in cursor.fetchall():
                oms[(r["application_id"], r["provider_name"], r["ticket_id"])] = r
    finally:
        cursor.close()
        conn.close()

    leads = []
    for s in scored:
        o = oms.get((s["application_id"], s["provider_name"], s["ticket_id"]))
        leads.append(o or {
            "application_id": s["application_id"], "provider_name": s["provider_name"],
            "ticket_id": s["ticket_id"], "customer_id": None,
            "customer_name": s["customer_name"], "customer_phone": s["customer_phone"]})
    prior = _prior_disbursed_bulk(leads)
    type_map = lead_data.load_loan_type_map()

    for s, lead in zip(scored, leads):
        cam = s["cam_fields"]
        if isinstance(cam, (str, bytes)):
            try:
                cam = json.loads(cam)
            except ValueError:
                cam = None
        base = _oms_fields(lead, prior.get(lead.get("ticket_id"), 0.0), type_map)
        if cam:
            rec = {**base, **_cam_record(cam, base["city"], type_map), "cam_url": s["cam_url"],
                   "cam_status": "ok", "cam_fields": cam}
        else:
            rec = {**base, "cam_url": s["cam_url"], "cam_status": "cam_error",
                   "cam_error": "no saved CAM data in lead_scores"}
        lead_data.upsert_lead(rec)
    log(f"Copied {len(scored)} lead(s) into lead_data.")
    return len(scored)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add leads (OMS + CAM data) to the database")
    parser.add_argument("--from", dest="date_from", help="first application date, like 2026-01-15")
    parser.add_argument("--to", dest="date_to", help="last application date (included), like 2026-03-10")
    parser.add_argument("--month", help="like 2026-09")
    parser.add_argument("--year", help="like 2026")
    parser.add_argument("--force", action="store_true", help="re-read every CAM")
    parser.add_argument("--coverage", action="store_true",
                        help="show from which date till which date data is already fetched")
    parser.add_argument("--backfill", action="store_true",
                        help="copy existing lead_scores into lead_data (no CAM downloads)")
    parser.add_argument("--recompute-loans", action="store_true",
                        help="re-work secured/unsecured, bank names, loan types and slabs "
                             "of everything already stored (no CAM downloads)")
    args = parser.parse_args()
    if args.coverage:
        lead_data.ensure_lead_data_table()
        cov = lead_data.get_ingest_coverage()
        print("Data already fetched (application dates):")
        for r in cov["covered"] or [{"from": "-", "to": "nothing fetched yet"}]:
            print(f"   {r['from']}  to  {r['to']}")
        print(f"Last finished: {cov['last_finished_at'] or '-'}")
    elif args.recompute_loans:
        lead_data.ensure_lead_data_table()
        lead_data.recompute_loan_columns()
    elif args.backfill:
        backfill_from_lead_scores()
    elif args.date_from or args.date_to:
        if not (args.date_from and args.date_to):
            parser.error("Give BOTH --from and --to.")
        ingest_dates(args.date_from, args.date_to, force=args.force)
    elif args.month or args.year:
        ingest_period(month=args.month, year=args.year, force=args.force)
    else:
        parser.error("Give --from and --to, --month, --year, --coverage or --backfill")