"""
lead_data.py - storage for the "extraction layer".

Tables (all in OUR OWN results database, never OMS):
  lead_data       one row per lead (application_id + provider): OMS fields,
                  CAM fields, MIS credit / ops remarks (filled by mis_sync.py). NO buckets.
  lead_tradelines one row per tradeline (loan) found in a lead's CAM.
  loan_type_map   which OMS loan types are secured / unsecured.

Dates are stored exactly as the source gave them (OMS dates stay dates,
MIS dates stay the text written in the sheet) - no format conversion.
"""
import json
import re
from datetime import date, timedelta

import mysql.connector

from config import config
from db import mysql_client
import loan_rules

# Loan types you named as secured. Everything else follows OMS's own
# loan_category until you set it in loan_type_map.
SEED_SECURED_TYPES = [
    "home loan", "lap", "business loan", "auto loan", "gold loan",
    "loan against property", "loan against securities", "loan against fd",
    "secured credit card",
]

LEAD_DATA_EXTRA_COLUMNS = {
    "customer_email": "VARCHAR(150)",
    "loan_class": "VARCHAR(20)",
    "highest_degree": "VARCHAR(200)",
    "registration_year": "VARCHAR(30)",
    "cibil_score": "INT",
    "enquiries_3m": "INT",
    "enquiries_6m": "INT",
    "family_profile": "TEXT",
    "abb": "DECIMAL(16,2)",
    "secured_loans": "INT",
    "unsecured_loans": "INT",
    "marital_status": "VARCHAR(30)",
    "house_type": "VARCHAR(30)",
    "employment_type": "VARCHAR(30)",
    "employment_source": "VARCHAR(12)",
    "mis_row_id": "INT",
    "mis_match": "VARCHAR(20)",
    "mis_credit_remark": "TEXT",
    "mis_ops_remark": "TEXT",
}

# Extra columns on lead_tradelines (one row per existing loan in the CAM).
TRADELINE_EXTRA_COLUMNS = {
    "bank_clean": "VARCHAR(200)",       # one clean name per bank, e.g. 'HDFC Bank'
    "loan_type_clean": "VARCHAR(100)",  # e.g. 'Home Loan', 'Credit Card'
    "slab": "VARCHAR(12)",              # Tiny / Small / Medium / Large (by loan amount)
}


# ---------------------------------------------------------------- tables

def _add_missing_columns(cursor, conn, table, columns):
    cursor.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
    """, (config.RESULTS_DB_DATABASE, table))
    have = {r[0].lower() for r in cursor.fetchall()}
    for name, ddl in columns.items():
        if name.lower() not in have:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            conn.commit()


def ensure_lead_data_table():
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                application_id INT NOT NULL,
                provider_name VARCHAR(500),
                ticket_id INT,
                customer_id INT,
                customer_name VARCHAR(300),
                customer_phone VARCHAR(30),
                phone_key VARCHAR(10),
                ticket_status VARCHAR(100),
                loan_type VARCHAR(200),
                loan_category VARCHAR(50),
                amount DECIMAL(16,2),
                application_date DATE,
                prior_disbursed_amount DECIMAL(16,2) DEFAULT 0,
                state VARCHAR(100),
                city VARCHAR(150),
                designation VARCHAR(100),
                experience_years INT,
                age INT,
                ongoing_loans INT,
                monthly_income DECIMAL(16,2),
                foir_percent DECIMAL(6,2),
                existing_emi DECIMAL(16,2),
                credit_remark TEXT,
                cam_url TEXT,
                cam_status VARCHAR(20) NOT NULL DEFAULT 'no_cam',
                cam_error TEXT,
                cam_fields JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY unique_lead (application_id, provider_name(190)),
                KEY idx_app_date (application_date),
                KEY idx_status (ticket_status),
                KEY idx_state (state),
                KEY idx_city (city),
                KEY idx_designation (designation),
                KEY idx_loan_type (loan_type),
                KEY idx_phone (phone_key),
                KEY idx_ticket (ticket_id)
            )
        """)
        conn.commit()
        _add_missing_columns(cursor, conn, "lead_data", LEAD_DATA_EXTRA_COLUMNS)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_tradelines (
                id INT AUTO_INCREMENT PRIMARY KEY,
                lead_data_id INT NOT NULL,
                member_name VARCHAR(300),
                loan_type VARCHAR(200),
                is_secured TINYINT(1),
                ownership VARCHAR(100),
                sanction_amount DECIMAL(16,2),
                current_balance DECIMAL(16,2),
                emi_amount DECIMAL(16,2),
                tenure_months INT,
                end_date VARCHAR(40),
                loan_completed TINYINT(1),
                KEY idx_lead (lead_data_id)
            )
        """)
        _add_missing_columns(cursor, conn, "lead_tradelines", TRADELINE_EXTRA_COLUMNS)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ingest_runs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                kind VARCHAR(10) NOT NULL,
                date_from DATE NOT NULL,
                date_to DATE NOT NULL,
                status VARCHAR(12) NOT NULL DEFAULT 'running',
                counts JSON,
                mis_status VARCHAR(12),
                mis_message TEXT,
                error TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP NULL,
                KEY idx_status_from (status, date_from)
            )
        """)
        conn.commit()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS loan_type_map (
                loan_type_key VARCHAR(200) PRIMARY KEY,
                loan_type_label VARCHAR(200),
                loan_class VARCHAR(20) NOT NULL
            )
        """)
        conn.commit()
        for t in SEED_SECURED_TYPES:
            cursor.execute(
                "INSERT IGNORE INTO loan_type_map (loan_type_key, loan_type_label, loan_class) "
                "VALUES (%s, %s, 'secured')",
                (t, {"lap": "LAP", "loan against fd": "Loan Against FD"}.get(t, t.title())))
        conn.commit()
    except mysql.connector.errors.ProgrammingError as e:
        if e.errno == 1142:
            print("Note: DB user can't create/alter tables - assuming they already exist.")
        else:
            raise
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------- helpers

def phone_key(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-10:] if digits else None


def month_range(month_str):
    """'2026-09' -> (2026-09-01, 2026-10-01)  (end is NOT included)"""
    year, month = map(int, month_str.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def year_range(year):
    year = int(year)
    return date(year, 1, 1), date(year + 1, 1, 1)


def resolve_period(month=None, year=None, from_date=None, to_date=None):
    """month / year / custom range -> (start, end_exclusive); (None, None) = all time."""
    if month:
        return month_range(month)
    if year:
        return year_range(year)
    if from_date or to_date:
        start = date.fromisoformat(from_date) if from_date else date(1970, 1, 1)
        end = (date.fromisoformat(to_date) + timedelta(days=1)) if to_date else date(2999, 1, 1)
        return start, end
    return None, None


# ---------------------------------------------------------------- loan type map

def load_loan_type_map():
    """{lowercase loan type: 'secured' | 'unsecured'}"""
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT loan_type_key, loan_class FROM loan_type_map")
    out = {k: v for k, v in cursor.fetchall()}
    cursor.close()
    conn.close()
    return out


def resolve_loan_class(loan_type, oms_category, type_map):
    """Your mapping first, then OMS's own loan_category, else unknown (None)."""
    key = str(loan_type or "").strip().lower()
    if key in type_map:
        return type_map[key]
    cat = str(oms_category or "").strip().lower()
    return cat if cat in ("secured", "unsecured") else None


def set_loan_type_class(loan_type, loan_class):
    if loan_class not in ("secured", "unsecured"):
        raise ValueError("loan_class must be 'secured' or 'unsecured'")
    key = str(loan_type or "").strip().lower()
    if not key:
        raise ValueError("loan_type is required")
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO loan_type_map (loan_type_key, loan_type_label, loan_class)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE loan_class = VALUES(loan_class)
    """, (key, str(loan_type).strip(), loan_class))
    cursor.execute("""
        UPDATE lead_data SET loan_class = %s WHERE LOWER(TRIM(loan_type)) = %s
    """, (loan_class, key))
    conn.commit()
    cursor.close()
    conn.close()


def list_loan_types():
    """Every loan type seen in stored leads, with its current class and lead count."""
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT TRIM(loan_type) AS loan_type, COUNT(*) AS leads,
               MAX(loan_class) AS loan_class, MAX(loan_category) AS oms_category
        FROM lead_data WHERE loan_type IS NOT NULL AND TRIM(loan_type) <> ''
        GROUP BY TRIM(loan_type) ORDER BY leads DESC
    """)
    rows = cursor.fetchall()
    cursor.execute("SELECT loan_type_key FROM loan_type_map")
    mapped = {r["loan_type_key"] for r in cursor.fetchall()}
    cursor.close()
    conn.close()
    for r in rows:
        r["leads"] = int(r["leads"])
        r["mapped"] = r["loan_type"].lower() in mapped
    return rows


# ---------------------------------------------------------------- writing leads

_LEAD_COLS = [
    "ticket_id", "customer_id", "customer_name", "customer_phone", "phone_key",
    "customer_email", "ticket_status", "loan_type", "loan_category", "loan_class",
    "amount", "application_date", "prior_disbursed_amount",
    "state", "city", "designation", "experience_years", "age", "ongoing_loans",
    "monthly_income", "foir_percent", "existing_emi", "credit_remark",
    "highest_degree", "registration_year", "cibil_score", "enquiries_3m",
    "enquiries_6m", "family_profile", "abb", "secured_loans", "unsecured_loans",
    "marital_status", "house_type", "employment_type", "employment_source",
    "cam_url", "cam_status", "cam_error", "cam_fields",
]


def upsert_lead(rec: dict):
    """Insert or update ONE lead (OMS + CAM fields), and replace its tradelines."""
    values = dict(rec)
    values["phone_key"] = phone_key(rec.get("customer_phone"))
    values["prior_disbursed_amount"] = rec.get("prior_disbursed_amount") or 0
    values["cam_status"] = rec.get("cam_status", "no_cam")
    cam_fields = rec.get("cam_fields")
    values["cam_fields"] = json.dumps(cam_fields, default=str) if cam_fields is not None else None

    cols = ["application_id", "provider_name"] + _LEAD_COLS
    marks = ",".join(["%s"] * len(cols))
    updates = ",".join(f"{c}=VALUES({c})" for c in _LEAD_COLS)

    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"INSERT INTO lead_data ({','.join(cols)}) VALUES ({marks}) "
        f"ON DUPLICATE KEY UPDATE {updates}",
        [values.get(c) for c in cols])
    cursor.execute(
        "SELECT id FROM lead_data WHERE application_id=%s AND provider_name=%s",
        (rec["application_id"], rec["provider_name"]))
    lead_id = cursor.fetchone()[0]

    tradelines = rec.get("tradelines")
    if tradelines is not None:
        cursor.execute("DELETE FROM lead_tradelines WHERE lead_data_id=%s", (lead_id,))
        if tradelines:
            cursor.executemany("""
                INSERT INTO lead_tradelines
                    (lead_data_id, member_name, loan_type, is_secured, ownership,
                     sanction_amount, current_balance, emi_amount, tenure_months,
                     end_date, loan_completed, bank_clean, loan_type_clean, slab)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, [(lead_id, t.get("member_name"), t.get("loan_type"), t.get("is_secured"),
                   t.get("ownership"), t.get("sanction_amount"), t.get("current_balance"),
                   t.get("emi_amount"), t.get("tenure_months"), t.get("end_date"),
                   t.get("loan_completed"), t.get("bank_clean"), t.get("loan_type_clean"),
                   t.get("slab")) for t in tradelines])
    conn.commit()
    cursor.close()
    conn.close()
    return lead_id


_REFRESH_SQL = """
    UPDATE lead_data SET
        ticket_id=%s, customer_id=%s, customer_name=%s, customer_phone=%s,
        phone_key=%s, customer_email=%s, ticket_status=%s, loan_type=%s,
        loan_category=%s, loan_class=%s, amount=%s, application_date=%s,
        prior_disbursed_amount=%s, city=COALESCE(%s, city)
    WHERE application_id=%s AND provider_name=%s
"""


def _refresh_params(rec: dict):
    return (
        rec.get("ticket_id"), rec.get("customer_id"), rec.get("customer_name"),
        rec.get("customer_phone"), phone_key(rec.get("customer_phone")),
        rec.get("customer_email"), rec.get("ticket_status"), rec.get("loan_type"),
        rec.get("loan_category"), rec.get("loan_class"), rec.get("amount"),
        rec.get("application_date"), rec.get("prior_disbursed_amount") or 0,
        rec.get("city"), rec["application_id"], rec["provider_name"],
    )


def refresh_oms_fields(rec: dict):
    """Cheap update for a lead whose CAM is already stored: only the OMS side."""
    refresh_oms_fields_bulk([rec])


def refresh_oms_fields_bulk(recs, chunk=500):
    """Same update for MANY leads using ONE database connection (much faster)."""
    recs = list(recs)
    if not recs:
        return
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor()
    try:
        for i in range(0, len(recs), chunk):
            cursor.executemany(_REFRESH_SQL, [_refresh_params(r) for r in recs[i:i + chunk]])
            conn.commit()
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------- ingest record
# One row in `ingest_runs` for every month-sized piece that ingest.py fetches,
# so the dashboard can show "data fetched from DATE to DATE".
# Record keeping must NEVER stop an ingest, so every function here catches its
# own errors and just prints a note.

def start_ingest_run(kind, date_from, date_to):
    """Writes a 'running' row. Returns its id (None if the record could not be written)."""
    try:
        conn = mysql_client.get_results_connection()
        cursor = conn.cursor()
        # a run that was killed (server stopped) never finished - do not leave it 'running' forever
        cursor.execute("""UPDATE ingest_runs SET status='failed', error='stopped before it finished'
                          WHERE status='running' AND started_at < NOW() - INTERVAL 12 HOUR""")
        cursor.execute("INSERT INTO ingest_runs (kind, date_from, date_to, status) "
                       "VALUES (%s, %s, %s, 'running')", (kind, date_from, date_to))
        run_id = cursor.lastrowid
        conn.commit()
        cursor.close()
        conn.close()
        return run_id
    except Exception as e:
        print(f"(could not write the ingest record: {e})")
        return None


def finish_ingest_run(run_id, status, counts=None, error=None):
    if run_id is None:
        return
    try:
        conn = mysql_client.get_results_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE ingest_runs SET status=%s, counts=%s, error=%s, finished_at=NOW() "
                       "WHERE id=%s",
                       (status, json.dumps(counts) if counts is not None else None,
                        (error or None) and str(error)[:2000], run_id))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"(could not update the ingest record: {e})")


def set_run_mis(run_ids, status, message=None):
    ids = [r for r in run_ids if r is not None]
    if not ids:
        return
    try:
        conn = mysql_client.get_results_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE ingest_runs SET mis_status=%s, mis_message=%s "
                       f"WHERE id IN ({','.join(['%s'] * len(ids))})",
                       [status, (message or None) and str(message)[:1000]] + ids)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"(could not save the MIS result in the ingest record: {e})")


def merge_date_ranges(ranges):
    """[(from, to), ...] -> sorted list where overlapping or back-to-back ranges are joined.
    (2026-01-01..2026-01-31) + (2026-02-01..2026-02-28) -> (2026-01-01..2026-02-28)"""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def get_ingest_coverage(recent=30):
    """
    {"covered": [{"from", "to"}, ...],   dates already fetched (finished runs only)
     "last_finished_at": ...,
     "recent_runs": [...]}               newest first, with status, counts and MIS result
    """
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT date_from, date_to FROM ingest_runs WHERE status='done'")
        covered = merge_date_ranges([(r["date_from"], r["date_to"]) for r in cursor.fetchall()])
        cursor.execute("SELECT MAX(finished_at) AS last FROM ingest_runs WHERE status='done'")
        last = cursor.fetchone()["last"]
        cursor.execute("""SELECT id, kind, date_from, date_to, status, counts, mis_status,
                                 mis_message, error, started_at, finished_at
                          FROM ingest_runs ORDER BY id DESC LIMIT %s""", (int(recent),))
        runs = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()
    for r in runs:
        if isinstance(r["counts"], (str, bytes)):
            try:
                r["counts"] = json.loads(r["counts"])
            except ValueError:
                pass
        for k in ("date_from", "date_to", "started_at", "finished_at"):
            r[k] = str(r[k]) if r[k] is not None else None
    return {
        "covered": [{"from": str(a), "to": str(b)} for a, b in covered],
        "last_finished_at": str(last) if last else None,
        "recent_runs": runs,
    }


def get_existing():
    """{(application_id, provider): {"cam_url", "cam_status"}} for stored leads."""
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT application_id, provider_name, cam_url, cam_status FROM lead_data")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {(r["application_id"], r["provider_name"]): r for r in rows}


# ---------------------------------------------------------------- reading leads

def _in_clause(column, values, clauses, params):
    values = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not values:
        return
    clauses.append(f"LOWER({column}) IN ({','.join(['%s'] * len(values))})")
    params.extend(v.lower() for v in values)


def _in_clause_unknown(column, values, clauses, params):
    """Like _in_clause, but the word 'Unknown' also picks leads where the CAM had no value."""
    values = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not values:
        return
    want_unknown = any(v.lower() == "unknown" for v in values)
    real = [v.lower() for v in values if v.lower() != "unknown"]
    parts = []
    if real:
        parts.append(f"LOWER(TRIM({column})) IN ({','.join(['%s'] * len(real))})")
        params.extend(real)
    if want_unknown:
        parts.append(f"({column} IS NULL OR TRIM({column}) = '')")
    clauses.append("(" + " OR ".join(parts) + ")")


def _as_number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _range_clause(column, low, high, clauses, params):
    """min / max on a column. A lead with NO value is left out once a range is given."""
    low, high = _as_number(low), _as_number(high)
    if low is not None:
        clauses.append(f"{column} >= %s")
        params.append(low)
    if high is not None:
        clauses.append(f"{column} <= %s")
        params.append(high)


# A loan counts as RUNNING today when (same rule as loan_rules.is_loan_active):
#   - it has an end date that is today or later, or
#   - it has no end date (credit card ...) and its balance is not 0.
# It is worked out when you filter, using TODAY's date, so the answer stays
# right as time passes - nothing saved at ingest time goes stale.
ACTIVE_LOAN_SQL = (
    "(lt.loan_completed IS NULL OR lt.loan_completed = 0) AND ("
    "(lt.end_date IS NOT NULL AND TRIM(lt.end_date) <> '' "
    "AND CAST(LEFT(TRIM(lt.end_date), 10) AS DATE) >= CURDATE()) "
    "OR ((lt.end_date IS NULL OR TRIM(lt.end_date) = '') AND COALESCE(lt.current_balance, 1) > 0))"
)


def _active_count_sql(extra=""):
    return ("(SELECT COUNT(*) FROM lead_tradelines lt "
            f"WHERE lt.lead_data_id = lead_data.id AND {ACTIVE_LOAN_SQL}{extra})")


# Running-loan counts for every lead row (added to the lists the screens use).
LIVE_COUNT_COLUMNS = (
    f"{_active_count_sql()} AS active_loans, "
    f"{_active_count_sql(' AND lt.is_secured = 1')} AS active_secured_loans, "
    f"{_active_count_sql(' AND (lt.is_secured IS NULL OR lt.is_secured = 0)')} AS active_unsecured_loans"
)


def _count_range_clause(extra, low, high, clauses, params):
    low, high = _as_number(low), _as_number(high)
    sub = _active_count_sql(extra)
    if low is not None:
        clauses.append(f"{sub} >= %s")
        params.append(int(low))
    if high is not None:
        clauses.append(f"{sub} <= %s")
        params.append(int(high))


def _as_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def build_where(filters: dict):
    """
    filters (all optional; nothing chosen = no restriction):
      month / year / from_date / to_date    when the lead was logged (OMS application date)
      statuses, states, cities, loan_types, designations, loan_classes   lists
      min_amount, max_amount                application amount in rupees
      only_with_cam                         leave out leads without a readable CAM

    About the person:
      min_income, max_income                monthly income in rupees
      employment_types                      list: Salaried / Self-employed / Other / Unknown
      min_cibil, max_cibil                  CIBIL score range
      min_experience, max_experience        years of work experience
      marital_statuses                      list: Married / Unmarried / Divorced / Widowed / Unknown
      house_types                           list: Owned / Rented / Hostel / Other / Unknown

    About the loans they ALREADY have (only loans still running today are counted):
      min_active_loans,   max_active_loans
      min_secured_loans,  max_secured_loans
      min_unsecured_loans, max_unsecured_loans
      slabs             [{"slab": "Tiny", "min": 3, "max": null}, ...]  - how many running
                        loans in each size slab (Tiny/Small/Medium/Large, see loan_rules.py)
      many_small_loans  true -> 5+ running loans and most of them Tiny or Small
      loan_matches      [{"bank": "HDFC Bank", "loan_type": "Personal Loan",
                          "min_amount": 500000, "max_amount": 1000000}, ...]
                        Every part of ONE entry must be true for the SAME single running loan.
                        Several entries must ALL be satisfied (each by some loan).
                        bank / loan_type can also be lists (any of them).
                        "min_count" (default 1) / "max_count": how many such loans the lead
                        must have, e.g. min_count 2 = at least 2 HDFC personal loans;
                        max_count 0 = must have none of them.
      loan_match_mode   "all" (default) = every entry must hold, "any" = at least one entry holds
    """
    f = filters or {}
    clauses, params = [], []

    start, end = resolve_period(f.get("month"), f.get("year"),
                                f.get("from_date"), f.get("to_date"))
    if start and end:
        clauses.append("application_date >= %s AND application_date < %s")
        params.extend([start, end])

    _in_clause("TRIM(ticket_status)", f.get("statuses"), clauses, params)
    _in_clause("state", f.get("states"), clauses, params)
    _in_clause("city", f.get("cities"), clauses, params)
    _in_clause("loan_type", f.get("loan_types"), clauses, params)
    _in_clause("designation", f.get("designations"), clauses, params)
    _in_clause("loan_class", f.get("loan_classes"), clauses, params)

    if f.get("min_amount") not in (None, ""):
        clauses.append("amount >= %s")
        params.append(float(f["min_amount"]))
    if f.get("max_amount") not in (None, ""):
        clauses.append("amount <= %s")
        params.append(float(f["max_amount"]))
    if f.get("only_with_cam"):
        clauses.append("cam_status = 'ok'")

    # ---- about the person
    _range_clause("monthly_income", f.get("min_income"), f.get("max_income"), clauses, params)
    _range_clause("cibil_score", f.get("min_cibil"), f.get("max_cibil"), clauses, params)
    _range_clause("experience_years", f.get("min_experience"), f.get("max_experience"),
                  clauses, params)
    _in_clause_unknown("employment_type", f.get("employment_types"), clauses, params)
    _in_clause_unknown("marital_status", f.get("marital_statuses"), clauses, params)
    _in_clause_unknown("house_type", f.get("house_types"), clauses, params)

    # ---- the loans they already have (running today)
    _count_range_clause("", f.get("min_active_loans"), f.get("max_active_loans"),
                        clauses, params)
    _count_range_clause(" AND lt.is_secured = 1",
                        f.get("min_secured_loans"), f.get("max_secured_loans"),
                        clauses, params)
    _count_range_clause(" AND (lt.is_secured IS NULL OR lt.is_secured = 0)",
                        f.get("min_unsecured_loans"), f.get("max_unsecured_loans"),
                        clauses, params)

    valid_slabs = {n.lower(): n for n in loan_rules.SLAB_NAMES}
    for item in (f.get("slabs") or []):
        if not isinstance(item, dict):
            continue
        slab = valid_slabs.get(str(item.get("slab") or "").strip().lower())
        if not slab:
            continue
        lo, hi = _as_number(item.get("min")), _as_number(item.get("max"))
        if lo is None and hi is None:
            continue
        _count_range_clause(" AND lt.slab = '%s'" % slab, lo, hi, clauses, params)

    if f.get("many_small_loans") in (True, "true", "True", "1", 1):
        total = _active_count_sql()
        small = _active_count_sql(" AND lt.slab IN ('Tiny', 'Small')")
        clauses.append(f"({total} >= %s AND {small} >= %s * {total})")
        params.extend([loan_rules.MANY_SMALL_MIN_LOANS, loan_rules.MANY_SMALL_SHARE])

    match_clauses, match_params = [], []
    for match in (f.get("loan_matches") or []):
        if not isinstance(match, dict):
            continue
        conds, mparams = [], []
        banks = [b.lower() for b in _as_list(match.get("bank"))]
        types = [t.lower() for t in _as_list(match.get("loan_type"))]
        if banks:
            conds.append(f"LOWER(lt.bank_clean) IN ({','.join(['%s'] * len(banks))})")
            mparams.extend(banks)
        if types:
            conds.append(f"LOWER(lt.loan_type_clean) IN ({','.join(['%s'] * len(types))})")
            mparams.extend(types)
        lo, hi = _as_number(match.get("min_amount")), _as_number(match.get("max_amount"))
        if lo is not None:
            conds.append("lt.sanction_amount >= %s")
            mparams.append(lo)
        if hi is not None:
            conds.append("lt.sanction_amount <= %s")
            mparams.append(hi)
        if not conds:
            continue
        # How many such running loans: "at least min_count" (default 1) and optionally
        # "at most max_count" (0 = the lead must have NONE of them).
        min_count, max_count = _as_number(match.get("min_count")), _as_number(match.get("max_count"))
        if min_count is None:
            min_count = 0 if max_count is not None else 1
        count_sql = ("(SELECT COUNT(*) FROM lead_tradelines lt WHERE lt.lead_data_id = lead_data.id "
                     f"AND {ACTIVE_LOAN_SQL} AND " + " AND ".join(conds) + ")")
        parts, part_params = [], []
        if min_count > 0:
            parts.append(f"{count_sql} >= %s")
            part_params += mparams + [int(min_count)]
        if max_count is not None:
            parts.append(f"{count_sql} <= %s")
            part_params += mparams + [int(max_count)]
        if not parts:
            continue
        match_clauses.append("(" + " AND ".join(parts) + ")")
        match_params.extend(part_params)

    if match_clauses:
        # "all" (default): every condition must hold.  "any": at least one condition holds.
        joiner = " OR " if str(f.get("loan_match_mode") or "").lower() == "any" else " AND "
        clauses.append("(" + joiner.join(match_clauses) + ")")
        params.extend(match_params)

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _decode_cam(rows):
    for r in rows:
        if isinstance(r.get("cam_fields"), (str, bytes)):
            try:
                r["cam_fields"] = json.loads(r["cam_fields"])
            except ValueError:
                r["cam_fields"] = None
    return rows


def query_leads(filters: dict):
    """All stored leads matching the filters, newest first, cam_fields decoded."""
    where, params = build_where(filters)
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        f"SELECT lead_data.*, {LIVE_COUNT_COLUMNS} FROM lead_data{where} "
        f"ORDER BY application_date DESC, id DESC", params)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return _decode_cam(rows)


LIST_COLUMNS = (
    "id, application_id, ticket_id, customer_name, customer_phone, customer_email, "
    "ticket_status, loan_type, loan_category, loan_class, amount, application_date, "
    "state, city, designation, experience_years, age, ongoing_loans, secured_loans, "
    "unsecured_loans, credit_remark, mis_credit_remark, mis_ops_remark, mis_match, cam_status, "
    "monthly_income, existing_emi, cibil_score, employment_type, employment_source, "
    "marital_status, house_type"
)


def list_leads(filters: dict, limit=50, offset=0):
    """One page of stored leads (slim columns, no cam_fields) + the total count."""
    where, params = build_where(filters)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(f"SELECT COUNT(*) AS n FROM lead_data{where}", params)
    total = int(cursor.fetchone()["n"])
    cursor.execute(
        f"SELECT {LIST_COLUMNS}, {LIVE_COUNT_COLUMNS} FROM lead_data{where} "
        f"ORDER BY application_date DESC, id DESC LIMIT %s OFFSET %s",
        params + [limit, offset])
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    for r in rows:
        r["application_date"] = str(r["application_date"])[:10] if r["application_date"] else None
        r["amount"] = float(r["amount"]) if r["amount"] is not None else None
        for key in ("monthly_income", "existing_emi"):
            r[key] = float(r[key]) if r[key] is not None else None
    return {"total": total, "limit": limit, "offset": offset, "leads": rows}


def get_lead_detail(lead_id):
    """One stored lead with every field, its CAM data and its tradelines."""
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM lead_data WHERE id=%s", (lead_id,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return None
    cursor.execute("SELECT * FROM lead_tradelines WHERE lead_data_id=%s ORDER BY id", (lead_id,))
    row["tradelines"] = cursor.fetchall()
    cursor.close()
    conn.close()
    for t in row["tradelines"]:        # still running TODAY? (not just when it was saved)
        t["is_active"] = loan_rules.is_loan_active(t.get("end_date"), t.get("current_balance"))
    return _decode_cam([row])[0]


def get_filter_options(period_filters: dict = None):
    """Dropdown values - exactly what exists in the stored data for the chosen period."""
    where, params = build_where(period_filters or {})
    sep = " AND " if where else " WHERE "
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)

    def distinct(column):
        cursor.execute(
            f"SELECT DISTINCT TRIM({column}) AS v FROM lead_data{where}{sep}{column} IS NOT NULL "
            f"AND TRIM({column}) <> '' ORDER BY v", params)
        return [r["v"] for r in cursor.fetchall()]

    options = {
        "statuses": distinct("ticket_status"),
        "states": distinct("state"),
        "loan_types": distinct("loan_type"),
        "designations": distinct("designation"),
        "loan_classes": distinct("loan_class"),
    }
    cursor.execute(
        f"SELECT DISTINCT state, city FROM lead_data{where}{sep}city IS NOT NULL "
        f"AND TRIM(city) <> '' ORDER BY state, city", params)
    cities_by_state = {}
    for r in cursor.fetchall():
        cities_by_state.setdefault(r["state"] or "Unknown", []).append(r["city"])
    options["cities_by_state"] = cities_by_state
    options["cities"] = sorted({c for v in cities_by_state.values() for c in v})

    cursor.execute(
        f"SELECT MIN(amount) AS lo, MAX(amount) AS hi, COUNT(*) AS n FROM lead_data{where}", params)
    row = cursor.fetchone() or {}
    options["amount_range"] = {"min": float(row.get("lo") or 0), "max": float(row.get("hi") or 0)}
    options["total_leads"] = int(row.get("n") or 0)

    # ---- filters about the person
    options["employment_types"] = ["Salaried", "Self-employed", "Other", "Unknown"]
    options["marital_statuses"] = ["Married", "Unmarried", "Divorced", "Widowed", "Unknown"]
    options["house_types"] = ["Owned", "Rented", "Hostel", "Other", "Unknown"]
    cursor.execute(
        f"SELECT MIN(monthly_income) AS inc_lo, MAX(monthly_income) AS inc_hi, "
        f"MIN(cibil_score) AS cibil_lo, MAX(cibil_score) AS cibil_hi, "
        f"MIN(experience_years) AS exp_lo, MAX(experience_years) AS exp_hi "
        f"FROM lead_data{where}", params)
    r = cursor.fetchone() or {}
    options["income_range"] = {"min": float(r.get("inc_lo") or 0), "max": float(r.get("inc_hi") or 0)}
    options["cibil_range"] = {"min": int(r.get("cibil_lo") or 300), "max": int(r.get("cibil_hi") or 900)}
    options["experience_range"] = {"min": int(r.get("exp_lo") or 0), "max": int(r.get("exp_hi") or 0)}

    # ---- filters about the loans they already have
    options["loan_slabs"] = [{"slab": n, "meaning": loan_rules.SLAB_HELP[n]}
                             for n in loan_rules.SLAB_NAMES]
    options["many_small_loans_rule"] = (
        f"{loan_rules.MANY_SMALL_MIN_LOANS}+ running loans, at least "
        f"{int(loan_rules.MANY_SMALL_SHARE * 100)}% of them Tiny or Small")
    cursor.execute(
        f"SELECT DISTINCT lt.bank_clean AS v FROM lead_tradelines lt "
        f"WHERE lt.lead_data_id IN (SELECT id FROM lead_data{where}) "
        f"AND lt.bank_clean IS NOT NULL AND lt.bank_clean <> '' ORDER BY v", params)
    options["banks"] = [x["v"] for x in cursor.fetchall()]
    cursor.execute(
        f"SELECT DISTINCT lt.loan_type_clean AS v FROM lead_tradelines lt "
        f"WHERE lt.lead_data_id IN (SELECT id FROM lead_data{where}) "
        f"AND lt.loan_type_clean IS NOT NULL AND lt.loan_type_clean <> '' ORDER BY v", params)
    options["existing_loan_types"] = [x["v"] for x in cursor.fetchall()]
    cursor.close()
    conn.close()
    return options


def recompute_loan_columns(log=print, chunk=500):
    """
    Re-works the loan columns of everything ALREADY stored, with NO CAM download:
      lead_tradelines: is_secured, bank_clean, loan_type_clean, slab
      lead_data:       ongoing_loans, secured_loans, unsecured_loans (running TODAY)
    Use it after you change loan_rules.py (a new bank spelling, a new secured type,
    different slab limits ...).      python ingest.py --recompute-loans
    """
    type_map = load_loan_type_map()
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""SELECT id, lead_data_id, member_name, loan_type, sanction_amount,
                             current_balance, end_date FROM lead_tradelines""")
    rows = cursor.fetchall()
    log(f"Loans to re-check: {len(rows)}")

    updates = []
    for r in rows:
        updates.append((
            1 if loan_rules.is_secured_loan_type(r["loan_type"], type_map) else 0,
            loan_rules.clean_bank_name(r["member_name"]),
            loan_rules.clean_loan_type(r["loan_type"]),
            loan_rules.loan_slab(r["sanction_amount"], r["current_balance"]),
            r["id"]))
    wcur = conn.cursor()
    for i in range(0, len(updates), chunk):
        wcur.executemany(
            "UPDATE lead_tradelines SET is_secured=%s, bank_clean=%s, "
            "loan_type_clean=%s, slab=%s WHERE id=%s", updates[i:i + chunk])
        conn.commit()

    # per-lead counts of loans running today
    per_lead = {}
    for r in rows:
        counts = per_lead.setdefault(r["lead_data_id"], [0, 0, 0])
        if loan_rules.is_loan_active(r["end_date"], r["current_balance"]):
            counts[0] += 1
            if loan_rules.is_secured_loan_type(r["loan_type"], type_map):
                counts[1] += 1
            else:
                counts[2] += 1
    lead_updates = [(c[0], c[1], c[2], lead_id) for lead_id, c in per_lead.items()]
    for i in range(0, len(lead_updates), chunk):
        wcur.executemany(
            "UPDATE lead_data SET ongoing_loans=%s, secured_loans=%s, unsecured_loans=%s "
            "WHERE id=%s", lead_updates[i:i + chunk])
        conn.commit()
    wcur.close()
    cursor.close()
    conn.close()
    log(f"Done. {len(updates)} loan(s) and {len(lead_updates)} lead(s) updated.")
    return {"loans": len(updates), "leads": len(lead_updates)}


def get_ingest_summary(filters: dict = None):
    """How many leads are stored for a period, split by CAM status and by class."""
    where, params = build_where(filters or {})
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        f"SELECT cam_status, COUNT(*) AS n FROM lead_data{where} GROUP BY cam_status", params)
    by_cam = {r["cam_status"]: int(r["n"]) for r in cursor.fetchall()}
    cursor.execute(
        f"SELECT COALESCE(loan_class,'unknown') AS c, COUNT(*) AS n FROM lead_data{where} "
        f"GROUP BY COALESCE(loan_class,'unknown')", params)
    by_class = {r["c"]: int(r["n"]) for r in cursor.fetchall()}
    cursor.close()
    conn.close()
    return {"by_cam_status": by_cam, "by_loan_class": by_class,
            "total": sum(by_cam.values())}


def get_data_coverage():
    """
    What the "Data & Ingest" screen shows:
      last_application_date / first_application_date   newest / oldest application date stored
      total_leads, leads_with_cam, last_updated
      months          [{month 'YYYY-MM', leads, with_cam, last_application_date}] oldest first
      fetched_ranges  [{from, to}] dates already fetched (finished runs only, joined together)
      recent_runs     the latest runs, newest first (status, counts, MIS result)
    """
    def _i(v):
        return int(v) if v is not None else 0

    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT COUNT(*) AS total, SUM(cam_status = 'ok') AS with_cam,
                                 MIN(application_date) AS first_date,
                                 MAX(application_date) AS last_date,
                                 MAX(updated_at) AS last_updated
                          FROM lead_data""")
        t = cursor.fetchone() or {}
        cursor.execute("""SELECT DATE_FORMAT(application_date, '%Y-%m') AS month,
                                 COUNT(*) AS leads, SUM(cam_status = 'ok') AS with_cam,
                                 MAX(application_date) AS last_application_date
                          FROM lead_data WHERE application_date IS NOT NULL
                          GROUP BY DATE_FORMAT(application_date, '%Y-%m')
                          ORDER BY month""")
        months = [{"month": r["month"], "leads": _i(r["leads"]), "with_cam": _i(r["with_cam"]),
                   "last_application_date": str(r["last_application_date"])[:10]}
                  for r in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()

    try:                       # the run record is a bonus - the screen must work without it
        runs = get_ingest_coverage(recent=5)
    except Exception:
        runs = {"covered": [], "recent_runs": []}

    return {
        "last_application_date": str(t["last_date"])[:10] if t.get("last_date") else None,
        "first_application_date": str(t["first_date"])[:10] if t.get("first_date") else None,
        "total_leads": _i(t.get("total")),
        "leads_with_cam": _i(t.get("with_cam")),
        "last_updated": str(t["last_updated"]) if t.get("last_updated") else None,
        "months": months,
        "fetched_ranges": runs["covered"],
        "recent_runs": runs["recent_runs"],
    }


def search_leads_by_phone(phone, limit=50):
    """
    Stored leads for a phone number, newest application first. Used by the
    dashboard's phone search (Lead Detail tab).
      * 10 or more digits typed -> the exact customer (last 10 digits are compared,
        so +91 / 0 in front does not matter)
      * fewer digits           -> every number that contains those digits
    Returns rows WITH cam_fields (the caller works out the buckets from it).
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return []
    if len(digits) >= 10:
        key = digits[-10:]
        where, params = "(phone_key = %s OR customer_phone LIKE %s)", [key, f"%{key}%"]
    else:
        where, params = "(phone_key LIKE %s OR customer_phone LIKE %s)", [f"%{digits}%", f"%{digits}%"]
    conn = mysql_client.get_results_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, application_id, ticket_id, customer_name, customer_phone, provider_name, "
            "ticket_status, loan_type, amount, application_date, cam_status, "
            "prior_disbursed_amount, cam_fields "
            f"FROM lead_data WHERE {where} ORDER BY application_date DESC, id DESC LIMIT %s",
            params + [int(limit)])
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()