"""
REST API server for the lead-scoring dashboard.

This is the missing piece between your existing backend (db/mysql_client.py,
which already has all the DB logic) and a separate frontend (built in
Google AI Studio, React, etc.). It exposes those Python functions as HTTP
JSON endpoints that a browser-based frontend can call over the network.

Run locally:
    pip install flask flask-cors
    python server.py

The server listens on http://localhost:5000 by default.

Auth model:
    POST /api/login returns a JWT (see db/mysql_client.py: create_access_token).
    Every other /api/* route requires that token in the request header:
        Authorization: Bearer <token>
    /api/health and /api/login are the only routes that don't require it.

CORS is enabled so a frontend running on a different port (e.g. an AI
Studio app on localhost:3000) is allowed to call this API during local
development. When you deploy for real, tighten this to your actual
frontend's domain instead of allowing everything - see the CORS(app) line.
"""
import json
import os
import re
import sys
import datetime
import threading
import time
import traceback
from functools import wraps
from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
from db import mysql_client, lead_data
from main import run_daily, run_for_month, build_financial_profile
from ingest import (ingest_dates, ingest_period, ingest_recent, IngestCancelled,
                    request_cancel, get_progress)
from mis_sync import sync_mis, link_mis_to_leads
from allocation import allocate
from extraction.gemini_client import generate_bucket_explanation
import contextlib
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter

app = Flask(__name__)
CORS(app, expose_headers=["Content-Disposition"])  # TODO before going live: restrict this to your deployed frontend's URL


@app.errorhandler(Exception)
def _handle_any_error(e):
    """
    If a route crashes, send back a normal JSON error (with CORS headers)
    instead of Flask's HTML crash page. Without this the browser only says
    "Failed to fetch" and hides the real reason. The full error is also
    printed in the terminal where server.py is running.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    traceback.print_exc()
    return jsonify({"error": f"Server error: {e}"}), 500


# ------------------------------------------- per-thread output redirection
# contextlib.redirect_stdout changes sys.stdout for the WHOLE program, so
# two background jobs redirecting at the same time could leave sys.stdout
# pointing at a closed file - and then any print() anywhere crashes. This
# small router sends output to a file only for the thread that asked for
# it; every other thread keeps printing to the normal terminal.

class _ThreadRoutedStdout:
    def __init__(self, original):
        self._original = original
        self._local = threading.local()

    def _target(self):
        return getattr(self._local, "target", None) or self._original

    def write(self, text):
        return self._target().write(text)

    def flush(self):
        try:
            self._target().flush()
        except ValueError:
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)


_stdout_router = _ThreadRoutedStdout(sys.stdout)
sys.stdout = _stdout_router


@contextlib.contextmanager
def _redirect_this_thread(target):
    """Send print() output from THIS thread only to `target` (a file)."""
    previous = getattr(_stdout_router._local, "target", None)
    _stdout_router._local.target = target
    try:
        yield
    finally:
        _stdout_router._local.target = previous

# ------------------------------------------------------- background auto-sync

# The server does this BY ITSELF, from the moment it starts:
#   1. ingest.py  - stores leads (with a readable CAM) of the current month
#                   only into lead_data,
#   2. mis_sync   - right after that, reads the MIS sheets and puts the credit /
#                   ops remarks on those leads.
# Then it waits and does it again. Any other dates: pick them in the calendar (or run
# python ingest.py --from YYYY-MM-DD --to YYYY-MM-DD) - it starts only when YOU start it.
AUTO_SYNC_INTERVAL_SECONDS = 1800   # 30 minutes. Lower = fresher data, more load on OMS / Google
AUTO_INGEST_MONTHS_BACK = 0         # 0 = ONLY the current month (1 = also the previous month)
RUN_OLD_SCORING_TOO = False         # True = ALSO run the old scorer that fills lead_scores
                                    # (the dashboard bucket pages still read lead_scores)


def _auto_ingest_once():
    """One automatic pass: ingest, then MIS remarks. Uses the same lock as the /api/ingest button."""
    with _ingest_lock:
        if _ingest_state["running"]:
            return                    # a manual ingest is running - skip this round
        os.makedirs("logs", exist_ok=True)
        log_path = "logs/auto-ingest.log"
        _ingest_state.update(
            running=True, stopping=False, period="auto (this month)", error=None, counts=None,
            log_file=log_path,
            started_at=datetime.datetime.now().isoformat(timespec="seconds"), finished_at=None)
    error, counts = None, None
    try:
        with open(log_path, "a", encoding="utf-8") as f, _redirect_this_thread(f):
            print(f"\n===== auto ingest {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====")
            counts = ingest_recent(months_back=AUTO_INGEST_MONTHS_BACK)
    except IngestCancelled:
        error = "Stopped by you"
    except Exception as e:
        error = str(e)
        traceback.print_exc()
    finally:
        with _ingest_lock:
            _ingest_state.update(
                running=False, stopping=False, error=error, counts=counts,
                finished_at=datetime.datetime.now().isoformat(timespec="seconds"))


def _background_sync_loop():
    while True:
        try:
            _auto_ingest_once()
            if RUN_OLD_SCORING_TOO:
                with open(os.devnull, "w") as devnull, _redirect_this_thread(devnull):
                    run_daily(fresh=False)
        except Exception as e:
            # Errors still print - you WANT to know if a sync pass failed.
            print(f"[auto-sync] Error during sync pass: {e}")
        time.sleep(AUTO_SYNC_INTERVAL_SECONDS)


# ---------------------------------------------------------------- helpers

def _decode_json_fields(row: dict, fields=("financial_profile", "cam_fields", "ai_explanation")):
    """
    lead_scores stores these columns as MySQL JSON, which the connector
    hands back as plain strings. Without this, the frontend would receive
    a JSON string INSIDE the JSON response (double-encoded) instead of a
    usable object. Decodes each field in place if present.
    """
    if not row:
        return row
    for field in fields:
        value = row.get(field)
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", errors="replace")  # some MySQL drivers return JSON as bytes
        if isinstance(value, str):
            try:
                row[field] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass  # leave as-is if it wasn't valid JSON for some reason
    return row


def _decode_json_fields_many(rows: list):
    return [_decode_json_fields(dict(row)) for row in rows]


# ---------------------------------------------------------------- auth

def require_auth(f):
    """
    Auth requirement removed: allows all requests with a default user.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        payload = {"sub": 1, "id": 1, "username": "admin", "email": "admin@f2fintech.in"}
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
            try:
                decoded = mysql_client.decode_access_token(token)
                if decoded and isinstance(decoded, dict):
                    payload.update(decoded)
            except Exception:
                pass
        request.user = payload
        return f(*args, **kwargs)
    return wrapper


@app.route("/", methods=["GET"])
@app.route("/dashboard", methods=["GET"])
def serve_dashboard():
    return send_file(os.path.join(os.path.dirname(__file__), "dashboard.html"))


@app.route("/api/health", methods=["GET"])
def health():
    """Simple check the frontend (or you) can call to confirm the API is up."""
    return jsonify({"status": "ok", "version": "2026-09-29-b"})


@app.route("/api/login", methods=["POST"])
def login():
    """Bypassed login - returns success immediately."""
    data = request.get_json(silent=True) or {}
    identifier = data.get("username") or data.get("email") or "admin"
    return jsonify({
        "token": "bypass-token",
        "user": {"id": 1, "username": identifier, "email": f"{identifier}@f2fintech.in"},
    })

def _admin_names():
    """Optional: ADMIN_USERNAMES=name1,name2 in .env. If not set, the
    FIRST user ever created is the admin."""
    raw = os.getenv("ADMIN_USERNAMES", "")
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def _is_admin(user):
    names = _admin_names()
    if names:
        return str(user["username"]).lower() in names
    first = mysql_client.get_first_user()
    return bool(first) and first["id"] == user["id"]


@app.route("/api/users/status", methods=["GET"])
def users_status():
    return jsonify({"has_users": mysql_client.count_users() > 0})


@app.route("/api/users/create", methods=["POST"])
def create_new_user():
    data = request.get_json(silent=True) or {}
    new_username = (data.get("new_username") or "").strip()
    new_email = (data.get("new_email") or "").strip()
    new_password = data.get("new_password") or ""

    if len(new_username) < 3:
        return jsonify({"error": "Username must be at least 3 characters."}), 400
    if "@" not in new_email or "." not in new_email:
        return jsonify({"error": "Enter a valid email."}), 400
    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    if mysql_client.count_users() > 0:
        admin_id = (data.get("admin_username") or "").strip()
        admin_pw = data.get("admin_password") or ""
        admin = mysql_client.get_user_by_identifier(admin_id) if admin_id else None
        if (not admin
                or not mysql_client.verify_password(admin_pw, admin["password_hash"])
                or not _is_admin(admin)):
            # 403, not 401: the dashboard treats 401 as "session expired" and signs you out
            return jsonify({"error": "Admin username or password is wrong."}), 403

    try:
        new_id = mysql_client.create_user(new_username, new_email, new_password)
    except Exception as e:
        if "Duplicate" in str(e):
            return jsonify({"error": "That username or email already exists."}), 409
        raise
    return jsonify({"status": "created", "id": new_id, "username": new_username})
# ---------------------------------------------------------------- leads

@app.route("/api/leads/search", methods=["GET"])
@require_auth
def search_leads():
    """?phone=...  - partial match on customer phone number, newest first."""
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "phone query param is required"}), 400
    rows = mysql_client.search_leads_by_phone(phone)
    return jsonify(_decode_json_fields_many(rows))


@app.route("/api/leads/bucket-counts", methods=["GET"])
@require_auth
def bucket_counts():
    """
    Page 2 (overview): ?type=secured or ?type=unsecured
    Returns: {"Very Low": 3, "Low": 7, "Medium": 12, "Good": 5, "Excellent": 2}
    """
    bucket_type = request.args.get("type", "secured")
    if bucket_type not in ("secured", "unsecured"):
        return jsonify({"error": "type must be 'secured' or 'unsecured'"}), 400
    return jsonify(mysql_client.get_bucket_counts(bucket_type))


@app.route("/api/leads/by-bucket", methods=["GET"])
@require_auth
def leads_by_bucket():
    """
    Page 2 (drill-in): ?type=secured&bucket=Excellent
    Returns the list of leads in that specific bucket.
    """
    bucket_type = request.args.get("type", "secured")
    bucket_name = request.args.get("bucket")
    if bucket_type not in ("secured", "unsecured"):
        return jsonify({"error": "type must be 'secured' or 'unsecured'"}), 400
    if not bucket_name:
        return jsonify({"error": "bucket query param is required"}), 400
    rows = mysql_client.get_leads_by_bucket(bucket_type, bucket_name)
    return jsonify(_decode_json_fields_many(rows))


def _as_dict(value):
    """Always gives back a dict, so .get() can never crash on odd data."""
    return value if isinstance(value, dict) else {}


def _excel_safe(value):
    """Excel refuses some hidden control characters - strip them so one
    odd name or link can never break the whole download."""
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


@app.route("/api/leads/by-bucket/export", methods=["GET"])
@require_auth
def export_leads_by_bucket():
    """
    Download an Excel (.xlsx) sheet of every lead in one bucket.
    ?type=secured&bucket=Good

    Columns: Name, Eligibility, Phone, City, Degree, CAM Link

    get_leads_by_bucket already returns ONE row per phone number (newest
    first), so this sheet always matches what the dashboard shows.
    Eligibility is the amount AFTER any already-disbursed amount was taken off.
    """
    bucket_type = request.args.get("type", "secured")
    bucket_name = request.args.get("bucket")
    if bucket_type not in ("secured", "unsecured"):
        return jsonify({"error": "type must be 'secured' or 'unsecured'"}), 400
    if not bucket_name:
        return jsonify({"error": "bucket query param is required"}), 400

    rows = _decode_json_fields_many(mysql_client.get_leads_by_bucket(bucket_type, bucket_name))

    wb = Workbook()
    ws = wb.active
    ws.title = f"{bucket_type}-{bucket_name}"[:31]  # Excel sheet name limit

    ws.append(["Name", "Eligibility (Rs.)", "Phone", "City", "Highest Qualification", "CAM Link"])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    eligibility_key = "secured_cap_eligibility" if bucket_type == "secured" else "unsecured_cap_eligibility"

    for row in rows:
        financial_profile = _as_dict(row.get("financial_profile"))
        cam_fields = _as_dict(row.get("cam_fields"))
        cap = _as_dict(financial_profile.get("financial_capacity"))

        ws.append([_excel_safe(v) for v in (
            row.get("customer_name") or "",
            cap.get(eligibility_key),
            row.get("customer_phone") or "",
            cam_fields.get("city") or "",
            cam_fields.get("highest_degree") or "",
            row.get("cam_url") or "",
        )])

    for i, width in enumerate([22, 18, 16, 16, 16, 45], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    buffer = BytesIO()
    wb.save(buffer)

    filename = f"leads_{bucket_type}_{bucket_name}.xlsx".replace(" ", "_")
    return Response(
        buffer.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/leads/<int:lead_id>", methods=["GET"])
@require_auth
def lead_detail(lead_id):
    """Page 3: full detail for one lead - scores, financial profile, AI explanation."""
    lead = mysql_client.get_lead_by_id(lead_id)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    return jsonify(_decode_json_fields(dict(lead)))

@app.route("/api/leads/<int:lead_id>/history", methods=["GET"])
@require_auth
def lead_history(lead_id):
    """
    Other applications by the same customer: which lender (provider) each
    went to, the loan type/amount, and what status the ticket ended up in.
    Each item has "is_earlier": true if it was applied BEFORE this lead.
    """
    lead = mysql_client.get_lead_by_id(lead_id)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    try:
        history = mysql_client.get_customer_application_history(lead["application_id"])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(history)


@app.route("/api/leads/<int:lead_id>/explanation", methods=["GET"])
@require_auth
def get_lead_explanation(lead_id):
    lead = mysql_client.get_lead_by_id(lead_id)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    lead = _decode_json_fields(dict(lead))
    
    financial_profile = lead.get("financial_profile") or {}
    cam_fields = lead.get("cam_fields") or {}
    credit_remark = cam_fields.get("credit_remark", "")
    
    try:
        explanation = generate_bucket_explanation(financial_profile, credit_remark)
        return jsonify(explanation)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/leads/<int:lead_id>/remarks", methods=["GET"])
@require_auth
def get_lead_remarks(lead_id):
    lead = mysql_client.get_lead_by_id(lead_id)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    ticket_id = lead["ticket_id"]
    remarks = mysql_client.get_remarks(ticket_id)
    return jsonify(remarks)

@app.route("/api/leads/<int:lead_id>/remarks", methods=["POST"])
@require_auth
def post_lead_remark(lead_id):
    lead = mysql_client.get_lead_by_id(lead_id)
    if not lead:
        return jsonify({"error": "Lead not found"}), 404
    
    data = request.get_json() or {}
    remark = data.get("remark")
    if not remark:
        return jsonify({"error": "Remark text is required"}), 400
        
    ticket_id = lead["ticket_id"]
    user_id = request.user["sub"]
    username = request.user["username"]
    
    mysql_client.add_remark(ticket_id, remark, user_id, username)
    return jsonify({"status": "ok"})

# ------------------------------------------------------- monthly scoring job

_monthly_lock = threading.Lock()
_monthly_state = {
    "running": False, "month": None,
    "started_at": None, "finished_at": None,
    "error": None, "log_file": None,
}


@app.route("/api/run-monthly", methods=["POST"])
@require_auth
def run_monthly():
    """Body: {"month": "2026-09"}. Starts scoring that month in the background."""
    data = request.get_json(silent=True) or {}
    month_str = (data.get("month") or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month_str):
        return jsonify({"error": "month must look like 2026-09 (YYYY-MM)"}), 400

    with _monthly_lock:
        if _monthly_state["running"]:
            return jsonify({
                "error": f"A monthly run for {_monthly_state['month']} is already running. Please wait for it to finish."
            }), 409
        os.makedirs("logs", exist_ok=True)
        log_path = f"logs/run-monthly-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
        _monthly_state.update(
            running=True, month=month_str, error=None, log_file=log_path,
            started_at=datetime.datetime.now().isoformat(timespec="seconds"),
            finished_at=None,
        )

    def _run_job():
        error = None
        try:
            with open(log_path, "w") as f, _redirect_this_thread(f):
                run_for_month(month_str, fresh=False)
        except Exception as e:
            error = str(e)
            traceback.print_exc()
            try:
                with open(log_path, "a") as f:
                    f.write(f"[run-monthly] Error during run: {e}\n")
            except OSError:
                pass
        finally:
            with _monthly_lock:
                _monthly_state.update(
                    running=False, error=error,
                    finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
                )

    threading.Thread(target=_run_job, daemon=True).start()
    return jsonify({"status": "started", "month": month_str})


@app.route("/api/run-monthly/status", methods=["GET"])
@require_auth
def run_monthly_status():
    """The dashboard checks this every few seconds to show progress."""
    with _monthly_lock:
        return jsonify(dict(_monthly_state))


# ------------------------------------------------- lead data (extract once) + allocate

_ingest_lock = threading.Lock()
_ingest_state = {
    "running": False, "period": None, "started_at": None, "finished_at": None,
    "error": None, "log_file": None, "counts": None, "stopping": False,
}

_MONTH_RE = re.compile(r"\d{4}-(0[1-9]|1[0-2])")
_YEAR_RE = re.compile(r"\d{4}")


def _period_from(source):
    """Reads month / year from a dict (JSON body or query args). Returns (month, year, error)."""
    month = (source.get("month") or "").strip()
    year = str(source.get("year") or "").strip()
    if month and not _MONTH_RE.fullmatch(month):
        return None, None, "month must look like 2026-09 (YYYY-MM)"
    if year and not _YEAR_RE.fullmatch(year):
        return None, None, "year must look like 2026 (YYYY)"
    return month or None, year or None, None


@app.route("/api/ingest", methods=["POST"])
@require_auth
def start_ingest():
    """Body: {"from_date": "2026-01-15", "to_date": "2026-03-10"}  (calendar - both days included)
         or {"month": "2026-09"} or {"year": "2026"}.  Optional "force": true.
    Reads OMS + CAMs for that period and stores them in lead_data (no buckets).
    Nothing starts until this is called."""
    data = request.get_json(silent=True) or {}
    from_date = (data.get("from_date") or "").strip()
    to_date = (data.get("to_date") or "").strip()
    month, year, err = _period_from(data)
    if err:
        return jsonify({"error": err}), 400
    if from_date or to_date:
        try:
            if not (from_date and to_date):
                raise ValueError("Pick BOTH a from date and a to date.")
            d_from, d_to = datetime.date.fromisoformat(from_date), datetime.date.fromisoformat(to_date)
            if d_from > d_to:
                raise ValueError("The from date must not be after the to date.")
            if d_from > datetime.date.today():
                raise ValueError("The from date is in the future.")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        period = f"{from_date} to {to_date}"
    elif month or year:
        period = month or year
    else:
        return jsonify({"error": "Pick a from date and a to date (or give a month / year)."}), 400
    force = bool(data.get("force"))

    with _ingest_lock:
        if _ingest_state["running"]:
            return jsonify({"error": f"An ingest for {_ingest_state['period']} is already running. "
                                     "Stop it first, then start a new one."}), 409
        os.makedirs("logs", exist_ok=True)
        log_path = f"logs/ingest-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
        _ingest_state.update(
            running=True, stopping=False, period=period, error=None, counts=None, log_file=log_path,
            started_at=datetime.datetime.now().isoformat(timespec="seconds"), finished_at=None)

    def _job():
        error, counts = None, None
        try:
            with open(log_path, "w") as f, _redirect_this_thread(f):
                if from_date:
                    counts = ingest_dates(from_date, to_date, force=force)
                else:
                    counts = ingest_period(month=month, year=year, force=force)
        except IngestCancelled:
            error = "Stopped by you"
        except Exception as e:
            error = str(e)
            traceback.print_exc()
        finally:
            with _ingest_lock:
                _ingest_state.update(
                    running=False, stopping=False, error=error, counts=counts,
                    finished_at=datetime.datetime.now().isoformat(timespec="seconds"))

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({"status": "started", "period": period})


@app.route("/api/ingest/coverage", methods=["GET"])
@require_auth
def ingest_coverage():
    """From which date till which date data is already fetched, plus the latest runs
    (each with its status, counts and MIS result)."""
    return jsonify(lead_data.get_ingest_coverage())


@app.route("/api/ingest/status", methods=["GET"])
@require_auth
def ingest_status():
    """Run state plus live progress (phase, percent, CAMs done / total, current month piece)."""
    with _ingest_lock:
        state = dict(_ingest_state)
    state["stopped"] = (not state["running"]) and state.get("error") == "Stopped by you"
    state["progress"] = get_progress() if state["running"] or state.get("finished_at") else None
    return jsonify(state)


@app.route("/api/ingest/stop", methods=["POST"])
@require_auth
def stop_ingest():
    """Ask the running ingest to stop. It stops after the CAM files being read right now;
    leads already stored are kept. Only after it has stopped can a new ingest be started."""
    with _ingest_lock:
        if not _ingest_state["running"]:
            return jsonify({"error": "No ingest is running."}), 409
        _ingest_state["stopping"] = True
    request_cancel()
    return jsonify({"status": "stopping"})


@app.route("/api/ingest/summary", methods=["GET"])
@require_auth
def ingest_summary():
    """?month=2026-09 or ?year=2026 - how many leads are stored, by CAM status."""
    month, year, err = _period_from(request.args)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(lead_data.get_ingest_summary({"month": month, "year": year}))


@app.route("/api/data-coverage", methods=["GET"])
@require_auth
def data_coverage():
    """Newest / oldest application date we hold, last sync time, and leads per month."""
    return jsonify(lead_data.get_data_coverage())


@app.route("/api/filters/options", methods=["GET"])
@require_auth
def filter_options():
    """?month=2026-09 or ?year=2026 (optional) - values for the filter dropdowns."""
    month, year, err = _period_from(request.args)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(lead_data.get_filter_options({"month": month, "year": year}))


# Every filter /api/allocate accepts (the screen sends only the ones the person chose).
_ALLOCATE_FILTER_KEYS = (
    "statuses", "states", "cities", "loan_types", "designations", "loan_classes",
    "min_amount", "max_amount", "from_date", "to_date",
    # about the person
    "min_income", "max_income", "employment_types", "min_cibil", "max_cibil",
    "min_experience", "max_experience", "marital_statuses", "house_types",
    # about the loans they already have (running today)
    "min_active_loans", "max_active_loans", "min_secured_loans", "max_secured_loans",
    "min_unsecured_loans", "max_unsecured_loans", "slabs", "many_small_loans",
    "loan_matches", "loan_match_mode",
)


@app.route("/api/allocate", methods=["POST"])
@require_auth
def allocate_leads():
    """
    Body (all optional): {"month": "2026-09" | "year": "2026",
      "statuses": [], "states": [], "cities": [], "loan_types": [],
      "designations": [], "min_amount": 500000, "max_amount": 5000000,

      "min_income": 200000, "max_income": 800000,
      "employment_types": ["Salaried", "Self-employed", "Other", "Unknown"],
      "min_cibil": 700, "max_cibil": 900,
      "min_experience": 3, "max_experience": 20,
      "marital_statuses": ["Married"], "house_types": ["Owned"],

      "min_active_loans": 0, "max_active_loans": 5,
      "min_secured_loans": 0, "max_secured_loans": 2,
      "min_unsecured_loans": 0, "max_unsecured_loans": 4,
      "slabs": [{"slab": "Tiny", "min": 0, "max": 2}],
      "many_small_loans": false,
      "loan_matches": [{"bank": "HDFC Bank", "loan_type": "Personal Loan",
                        "min_amount": 500000, "max_amount": 1000000}]}
    Returns bucket counts (secured + unsecured) and the matching leads.
    """
    data = request.get_json(silent=True) or {}
    month, year, err = _period_from(data)
    if err:
        return jsonify({"error": err}), 400
    filters = {k: data.get(k) for k in _ALLOCATE_FILTER_KEYS}
    filters["month"], filters["year"] = month, year
    return jsonify(allocate(filters))


# ------------------------------------------------- stored leads, MIS sync, loan types

_LIST_FILTERS = ("statuses", "states", "cities", "loan_types", "designations", "loan_classes")


_PERSON_LIST_FILTERS = ("employment_types", "marital_statuses", "house_types")
_NUMBER_FILTERS = (
    "min_amount", "max_amount", "month", "year", "from_date", "to_date",
    "min_income", "max_income", "min_cibil", "max_cibil",
    "min_experience", "max_experience",
    "min_active_loans", "max_active_loans", "min_secured_loans", "max_secured_loans",
    "min_unsecured_loans", "max_unsecured_loans",
)


def _filters_from_args(args):
    """Query-string filters: lists are comma separated (?states=Delhi,Punjab).

    Loan-size slabs:   ?slab_tiny_min=3&slab_large_max=0   (tiny / small / medium / large)
    Linked loan filter ?loan_bank=HDFC Bank&loan_type=Personal Loan&loan_min=500000&loan_max=1000000
                       (bank, type and amount must all be true for the SAME loan)
    Many small loans:  ?many_small_loans=true
    """
    filters = {}
    for key in _LIST_FILTERS + _PERSON_LIST_FILTERS:
        raw = args.get(key, "")
        filters[key] = [v.strip() for v in raw.split(",") if v.strip()]
    for key in _NUMBER_FILTERS:
        filters[key] = (args.get(key) or "").strip() or None

    slabs = []
    for name in ("tiny", "small", "medium", "large"):
        lo = (args.get(f"slab_{name}_min") or "").strip() or None
        hi = (args.get(f"slab_{name}_max") or "").strip() or None
        if lo is not None or hi is not None:
            slabs.append({"slab": name, "min": lo, "max": hi})
    filters["slabs"] = slabs
    filters["many_small_loans"] = (args.get("many_small_loans") or "").strip().lower() == "true"

    match = {"bank": args.get("loan_bank", ""), "loan_type": args.get("loan_type_existing", ""),
             "min_amount": args.get("loan_min"), "max_amount": args.get("loan_max")}
    if any((str(v or "").strip()) for v in match.values()):
        filters["loan_matches"] = [match]
    return filters


@app.route("/api/stored-leads", methods=["GET"])
@require_auth
def stored_leads():
    """Stored leads with any combination of filters, one page at a time.
    ?month=2026-09 &states=Delhi &statuses=Approved,Disbursed &min_amount=500000
    &limit=50 &offset=0"""
    filters = _filters_from_args(request.args)
    _, _, err = _period_from(filters)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(lead_data.list_leads(
        filters, limit=request.args.get("limit", 50), offset=request.args.get("offset", 0)))


@app.route("/api/stored-leads/search", methods=["GET"])
@require_auth
def search_stored_leads():
    """?phone=...  - phone search on the stored leads (lead_data), newest first.
    Each row also carries its secured / unsecured bucket, worked out right now."""
    phone = request.args.get("phone", "").strip()
    if not re.sub(r"\D", "", phone):
        return jsonify({"error": "Type the phone number (or a few digits of it)."}), 400
    out = []
    for r in lead_data.search_leads_by_phone(phone):
        cam = r.pop("cam_fields", None)
        if isinstance(cam, (str, bytes)):
            try:
                cam = json.loads(cam)
            except ValueError:
                cam = None
        secured = unsecured = None
        if r.get("cam_status") == "ok" and isinstance(cam, dict) and cam:
            try:
                cap = build_financial_profile(
                    cam, prior_disbursed_amount=float(r.get("prior_disbursed_amount") or 0)
                )["financial_capacity"]
                secured, unsecured = cap["secured_bucket"], cap["unsecured_bucket"]
            except Exception:
                pass
        r["secured_bucket"], r["unsecured_bucket"] = secured, unsecured
        out.append(r)
    return jsonify(json.loads(json.dumps(out, default=str)))


@app.route("/api/stored-leads/<int:lead_id>", methods=["GET"])
@require_auth
def stored_lead_detail(lead_id):
    row = lead_data.get_lead_detail(lead_id)
    if not row:
        return jsonify({"error": "lead not found"}), 404
    return jsonify(json.loads(json.dumps(row, default=str)))


# --------------------------------------- detail of a lead found by /api/allocate
# These work on lead_data ids (the ids /api/allocate gives back). Everything is
# worked out LIVE from the stored CAM data, with today's date.

def _stored_lead_with_cam(lead_id):
    row = lead_data.get_lead_detail(lead_id)
    if not row:
        return None, None, None
    cam = row.get("cam_fields") if isinstance(row.get("cam_fields"), dict) else {}
    profile = None
    if row.get("cam_status") == "ok" and cam:
        profile = build_financial_profile(
            cam, prior_disbursed_amount=float(row.get("prior_disbursed_amount") or 0))
    return row, cam, profile


@app.route("/api/allocated-leads/<int:lead_id>", methods=["GET"])
@require_auth
def allocated_lead_detail(lead_id):
    """One stored lead: its details, its loans, and the buckets worked out right now."""
    row, cam, profile = _stored_lead_with_cam(lead_id)
    if not row:
        return jsonify({"error": "lead not found"}), 404
    row.pop("cam_fields", None)
    out = json.loads(json.dumps(row, default=str))
    out["financial_profile"] = json.loads(json.dumps(profile, default=str)) if profile else None
    return jsonify(out)


@app.route("/api/allocated-leads/<int:lead_id>/explanation", methods=["GET"])
@require_auth
def allocated_lead_explanation(lead_id):
    row, cam, profile = _stored_lead_with_cam(lead_id)
    if not row or not profile:
        return jsonify({"error": "lead not found, or it has no readable CAM"}), 404
    try:
        return jsonify(generate_bucket_explanation(profile, cam.get("credit_remark", "")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/allocated-leads/<int:lead_id>/remarks", methods=["GET", "POST"])
@require_auth
def allocated_lead_remarks(lead_id):
    """Team remarks are kept per ticket (same table as before)."""
    row = lead_data.get_lead_detail(lead_id)
    if not row:
        return jsonify({"error": "lead not found"}), 404
    if request.method == "GET":
        return jsonify(json.loads(json.dumps(mysql_client.get_remarks(row["ticket_id"]), default=str)))
    remark = (request.get_json(silent=True) or {}).get("remark")
    if not remark:
        return jsonify({"error": "Remark text is required"}), 400
    mysql_client.add_remark(row["ticket_id"], remark, request.user["sub"], request.user["username"])
    return jsonify({"status": "ok"})


def _money_or_none(value, label):
    """Rupees from the request: None if empty, else a whole number >= 0."""
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if n < 0 or n > 10**12:
        raise ValueError(f"{label} is out of range")
    return n


@app.route("/api/allocated-leads/<int:lead_id>/assessments", methods=["GET", "POST"])
@require_auth
def allocated_lead_assessments(lead_id):
    """What the credit analyst found eligible vs what the banker gave, and why they differ."""
    row = lead_data.get_lead_detail(lead_id)
    if not row:
        return jsonify({"error": "lead not found"}), 404
    if request.method == "GET":
        return jsonify(mysql_client.get_assessments(row["ticket_id"]))
    data = request.get_json(silent=True) or {}
    try:
        analyst = _money_or_none(data.get("analyst_eligibility"), "Analyst eligibility")
        banker = _money_or_none(data.get("banker_eligibility"), "Banker eligibility")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    reason = (data.get("variation_reason") or "").strip()[:5000] or None
    if analyst is None and banker is None and not reason:
        return jsonify({"error": "Fill at least one field."}), 400
    if analyst is not None and banker is not None and analyst != banker and not reason:
        return jsonify({"error": "The two eligibilities differ - please write the reason for the difference."}), 400
    mysql_client.add_assessment(row["ticket_id"], analyst, banker, reason,
                                request.user["sub"], request.user["username"])
    return jsonify({"status": "ok"})


_mis_lock = threading.Lock()
_mis_state = {"running": False, "started_at": None, "finished_at": None,
              "error": None, "log_file": None, "result": None}


@app.route("/api/mis/sync", methods=["POST"])
@require_auth
def start_mis_sync():
    """Reads the MIS Google Sheets (links in .env) and links their remarks to stored leads."""
    with _mis_lock:
        if _mis_state["running"]:
            return jsonify({"error": "A MIS sync is already running."}), 409
        os.makedirs("logs", exist_ok=True)
        log_path = f"logs/mis-sync-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
        _mis_state.update(running=True, error=None, result=None, log_file=log_path,
                          started_at=datetime.datetime.now().isoformat(timespec="seconds"),
                          finished_at=None)

    def _job():
        error, result = None, None
        try:
            with open(log_path, "w") as f, _redirect_this_thread(f):
                result = sync_mis()
        except Exception as e:
            error = str(e)
            traceback.print_exc()
        finally:
            with _mis_lock:
                _mis_state.update(running=False, error=error, result=result,
                                  finished_at=datetime.datetime.now().isoformat(timespec="seconds"))

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/mis/status", methods=["GET"])
@require_auth
def mis_status():
    with _mis_lock:
        return jsonify(dict(_mis_state))


@app.route("/api/loan-types", methods=["GET"])
@require_auth
def loan_types():
    """Every loan type seen in stored leads, with its secured / unsecured class."""
    return jsonify(lead_data.list_loan_types())


@app.route("/api/loan-types", methods=["POST"])
@require_auth
def set_loan_type():
    """Body: {"loan_type": "Personal Loan", "loan_class": "unsecured"} - saves the
    choice and updates every stored lead of that type."""
    data = request.get_json(silent=True) or {}
    try:
        lead_data.set_loan_type_class(data.get("loan_type"), data.get("loan_class"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "saved"})


if __name__ == "__main__":
    # Make sure every table/column this API depends on exists before serving.
    mysql_client.ensure_scores_table()
    mysql_client.ensure_remarks_table()
    mysql_client.ensure_assessments_table()
    lead_data.ensure_lead_data_table()
    mysql_client.ensure_users_table()

    # Start the auto-sync loop in the background, once, the moment the
    # server starts. daemon=True means this thread never blocks the server
    # from shutting down when you stop it (Ctrl+C).
    #
    # use_reloader=False matters here: Flask's debug reloader normally
    # starts your script twice under the hood, which would otherwise spin
    # up TWO background sync loops running in parallel.
    sync_thread = threading.Thread(target=_background_sync_loop, daemon=True)
    sync_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)