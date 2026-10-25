"""
Handles everything to do with talking to the OMS MySQL database (read-only):
- fetching leads (customer_application + tickets)
- de-duplicating by (application_id, provider)
- finding the CAM (.xlsx) and CIBIL (.pdf) attachments inside ticket-activities

Also handles saving results into OUR OWN separate results database
(never OMS) - see get_results_connection().
"""
import re
import mysql.connector
from datetime import date
from typing import Optional
from config import config
import bcrypt
import jwt
import datetime as _dt

def get_connection():
    """Open a fresh connection to the OMS MySQL database (read-only usage)."""
    return mysql.connector.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
    )


def get_results_connection():
    """
    Open a connection to OUR OWN results database - completely separate
    from OMS. This is the only connection we ever write to.
    """
    return mysql.connector.connect(
        host=config.RESULTS_DB_HOST,
        port=config.RESULTS_DB_PORT,
        user=config.RESULTS_DB_USER,
        password=config.RESULTS_DB_PASSWORD,
        database=config.RESULTS_DB_DATABASE,
    )


def fetch_leads(since_date: Optional[date] = None):
    """
    Fetch leads (applications) that need scoring.

    since_date: only fetch applications updated/created on or after this date.
                Pass None to fetch everything.

    De-duplication rule: one row per (application_id, provider) combination.
    If the same combination appears more than once, we keep the most
    recently updated one.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            ca.id AS application_id,
            ca.customer_id,
            ca.provider AS provider_name,
            ca.loan_type,
            ca.loan_category,
            ca.amount,
            ca.application_date,
            ca.last_updated,
            c.name AS customer_name,
            c.contact AS customer_phone,
            t.id AS ticket_id,
            t.status AS ticket_status
        FROM customer_application ca
        JOIN customer c ON c.id = ca.customer_id
        JOIN tickets t ON t.customer_application_id = ca.id
        WHERE (%s IS NULL OR ca.last_updated >= %s)
        ORDER BY ca.last_updated DESC
    """
    cursor.execute(query, (since_date, since_date))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    deduped = {}
    for row in rows:
        key = (row["application_id"], row["provider_name"])
        if key not in deduped:
            deduped[key] = row

    return list(deduped.values())


def fetch_leads_before(before_date: date):
    """
    Fetch leads (applications) last updated STRICTLY BEFORE the given date.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            ca.id AS application_id,
            ca.customer_id,
            ca.provider AS provider_name,
            ca.loan_type,
            ca.loan_category,
            ca.amount,
            ca.application_date,
            ca.last_updated,
            c.name AS customer_name,
            c.contact AS customer_phone,
            t.id AS ticket_id,
            t.status AS ticket_status
        FROM customer_application ca
        JOIN customer c ON c.id = ca.customer_id
        JOIN tickets t ON t.customer_application_id = ca.id
        WHERE ca.application_date < %s
        ORDER BY ca.last_updated DESC
    """
    cursor.execute(query, (before_date,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    deduped = {}
    for row in rows:
        key = (row["application_id"], row["provider_name"])
        if key not in deduped:
            deduped[key] = row

    return list(deduped.values())


def fetch_leads_between(start_date: date, end_date: date):
    """
    Fetch leads (applications) last updated within [start_date, end_date)
    - inclusive of start_date, exclusive of end_date. Used for scoring a
    specific month's worth of leads.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            ca.id AS application_id,
            ca.customer_id,
            ca.provider AS provider_name,
            ca.loan_type,
            ca.loan_category,
            ca.amount,
            ca.application_date,
            ca.last_updated,
            c.name AS customer_name,
            c.contact AS customer_phone,
            t.id AS ticket_id,
            t.status AS ticket_status
        FROM customer_application ca
        JOIN customer c ON c.id = ca.customer_id
        JOIN tickets t ON t.customer_application_id = ca.id
        WHERE ca.application_date >= %s AND ca.application_date < %s
        ORDER BY ca.last_updated DESC
    """
    cursor.execute(query, (start_date, end_date))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    deduped = {}
    for row in rows:
        key = (row["application_id"], row["provider_name"])
        if key not in deduped:
            deduped[key] = row

    return list(deduped.values())


def find_cam_attachment(ticket_id: int):
    """
    Look through ticket-activities for this ticket and return the URL(s)
    of attachments that look like a CAM file (.xlsx), most recent first.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT attachment, created_at
        FROM `ticket-activities`
        WHERE ticket_id = %s
          AND attachment IS NOT NULL
          AND attachment LIKE '%%.xlsx'
        ORDER BY created_at DESC
    """
    cursor.execute(query, (ticket_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [row["attachment"] for row in rows]


def find_cibil_attachment(ticket_id: int):
    """
    Same idea as find_cam_attachment, but looks for a .pdf attachment
    (the Experian/CIBIL report) instead of .xlsx.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT attachment, created_at
        FROM `ticket-activities`
        WHERE ticket_id = %s
          AND attachment IS NOT NULL
          AND attachment LIKE '%%.pdf'
        ORDER BY created_at DESC
    """
    cursor.execute(query, (ticket_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return [row["attachment"] for row in rows]

def fetch_customer_disbursed_amount(customer_id: int, exclude_ticket_id: int = None) -> float:
    """
    Sums disbursed_amount across every OTHER ticket for this customer whose
    status is 'Disbursed' - i.e. loans we've already given this same
    customer, which reduce how much MORE they're eligible for on this lead.
    The current lead's own ticket is excluded so a lead never nets against
    itself. LOWER() guards against case variants of the status string.
    """
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT COALESCE(SUM(t.disbursed_amount), 0)
        FROM tickets t
        JOIN customer_application ca ON ca.id = t.customer_application_id
        WHERE ca.customer_id = %s
          AND LOWER(t.status) = 'disbursed'
    """
    params = [customer_id]
    if exclude_ticket_id is not None:
        query += " AND t.id != %s"
        params.append(exclude_ticket_id)
    cursor.execute(query, tuple(params))
    (total,) = cursor.fetchone()
    cursor.close()
    conn.close()
    return float(total or 0)


def get_todays_leads():
    """Convenience wrapper for the F2 Hot Leads feature - only today's new leads."""
    return fetch_leads(since_date=date.today())


def fetch_leads_page(offset: int, limit: int):
    """
    Fetch one page of leads (no date filter), ordered newest-first.
    Used by the bucket-coverage scan in main.py.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT
            ca.id AS application_id,
            ca.customer_id,
            ca.provider AS provider_name,
            ca.loan_type,
            ca.loan_category,
            ca.amount,
            ca.application_date,
            ca.last_updated,
            c.name AS customer_name,
            c.contact AS customer_phone,
            t.id AS ticket_id,
            t.status AS ticket_status
        FROM customer_application ca
        JOIN customer c ON c.id = ca.customer_id
        JOIN tickets t ON t.customer_application_id = ca.id
        ORDER BY ca.last_updated DESC
        LIMIT %s OFFSET %s
    """
    cursor.execute(query, (limit, offset))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _ensure_column(cursor, conn, column_name, ddl):
    """
    Adds a column to lead_scores if it's missing - handles tables that
    already existed from an older version of this script, so a new field
    doesn't crash inserts on a pre-existing table.
    """
    cursor.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = %s AND table_name = 'lead_scores' AND column_name = %s
    """, (config.RESULTS_DB_DATABASE, column_name))
    (has_column,) = cursor.fetchone()
    if not has_column:
        cursor.execute(f"ALTER TABLE lead_scores ADD COLUMN {ddl}")
        conn.commit()
        print(f"Added missing '{column_name}' column to lead_scores.")


def _drop_column_if_exists(cursor, conn, column_name):
    cursor.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = %s AND table_name = 'lead_scores' AND column_name = %s
    """, (config.RESULTS_DB_DATABASE, column_name))
    (has_column,) = cursor.fetchone()
    if has_column:
        cursor.execute(f"ALTER TABLE lead_scores DROP COLUMN {column_name}")
        conn.commit()
        print(f"Dropped '{column_name}' column from lead_scores.")

def ensure_scores_table():
    """
    Creates the table we save results into, if it doesn't already exist.
    This lives in OUR OWN database, never in OMS.
    """
    conn = get_results_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_scores (
                id INT AUTO_INCREMENT PRIMARY KEY,
                application_id INT NOT NULL,
                provider_name VARCHAR(500),
                ticket_id INT,
                customer_name VARCHAR(300),
                customer_phone VARCHAR(30),
                cam_url TEXT,
                secured_bucket VARCHAR(50),
                unsecured_bucket VARCHAR(50),
                financial_profile JSON,
                cam_fields JSON,
                UNIQUE KEY unique_lead (application_id, provider_name)
            )
        """)
        conn.commit()

        _ensure_column(cursor, conn, "application_id", "application_id INT NOT NULL AFTER id")
        _ensure_column(cursor, conn, "customer_phone", "customer_phone VARCHAR(30) AFTER customer_name")
        _ensure_column(cursor, conn, "cam_url", "cam_url TEXT AFTER customer_phone")

        _drop_column_if_exists(cursor, conn, "ai_explanation")
        _drop_column_if_exists(cursor, conn, "scored_at")
        _drop_column_if_exists(cursor, conn, "is_new_hot_lead")  # hot-leads feature removed

        # Try to drop the unique ticket_id constraint if it exists from previous run
        try:
            cursor.execute("ALTER TABLE lead_scores DROP INDEX ticket_id")
            conn.commit()
        except mysql.connector.errors.ProgrammingError:
            pass
            
        # Ensure unique_lead is created if we just added application_id
        try:
            cursor.execute("ALTER TABLE lead_scores ADD UNIQUE KEY unique_lead (application_id, provider_name)")
            conn.commit()
        except mysql.connector.errors.ProgrammingError:
            pass

    except mysql.connector.errors.ProgrammingError as e:
        if e.errno == 1142:  # CREATE/ALTER command denied
            print("Note: your DB user can't create/alter tables - assuming "
                  "'lead_scores' already matches the expected schema.")
        else:
            raise
    finally:
        cursor.close()
        conn.close()

def ensure_remarks_table():
    conn = get_results_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_remarks (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ticket_id INT NOT NULL,
                remark TEXT NOT NULL,
                user_id INT,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    except mysql.connector.errors.ProgrammingError as e:
        if e.errno != 1142:
            raise
    finally:
        cursor.close()
        conn.close()


def clear_all_scores():
    """
    Wipes every row from OUR results table (never touches OMS) before a
    fresh run.
    """
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM lead_scores")
    deleted_count = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    if deleted_count:
        print(f"Cleared {deleted_count} lead(s) from the previous run.")
    return deleted_count


def get_scored_keys():
    """
    Returns the set of (application_id, provider_name) already present in
    lead_scores, so a run can skip leads that were already processed
    instead of re-scoring everything from scratch every time.
    """
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT application_id, provider_name FROM lead_scores")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return {(app_id, provider) for app_id, provider in rows}


def save_lead_score(record: dict):
    """Insert or update (upsert) a lead's score - keyed on application_id + provider_name."""
    import json
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO lead_scores
            (application_id, provider_name, ticket_id, customer_name, customer_phone, cam_url,
             secured_bucket, unsecured_bucket, financial_profile, cam_fields)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            ticket_id = VALUES(ticket_id),
            customer_name = VALUES(customer_name),
            customer_phone = VALUES(customer_phone),
            cam_url = VALUES(cam_url),
            secured_bucket = VALUES(secured_bucket),
            unsecured_bucket = VALUES(unsecured_bucket),
            financial_profile = VALUES(financial_profile),
            cam_fields = VALUES(cam_fields)
    """, (
        record["application_id"], record["provider_name"], record.get("ticket_id"),
        record.get("customer_name"), record.get("customer_phone"), record.get("cam_url"),
        record["secured_bucket"], record["unsecured_bucket"],
        json.dumps(record["financial_profile"], default=str),
        json.dumps(record["cam_fields"], default=str),
    ))
    conn.commit()
    cursor.close()
    conn.close()


JWT_SECRET = config.RESULTS_DB_PASSWORD  # placeholder - move to its own JWT_SECRET in .env before going live
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24 * 30  # stay signed in for 30 days ("remember me")


def ensure_users_table():
    """Separate from OMS's own `users` table - this is just for portal logins."""
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            email VARCHAR(200) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cursor.close()
    conn.close()


def create_user(username: str, email: str, password: str) -> int:
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO app_users (username, email, password_hash) VALUES (%s, %s, %s)",
        (username, email, password_hash),
    )
    conn.commit()
    new_id = cursor.lastrowid
    cursor.close()
    conn.close()
    return new_id


def get_user_by_identifier(identifier: str):
    """identifier can be a username OR an email."""
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM app_users WHERE username = %s OR email = %s",
        (identifier, identifier),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def count_users() -> int:
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM app_users")
    n = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return n


def get_first_user():
    """The first user ever created is the admin."""
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM app_users ORDER BY id LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def verify_password(plain_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), password_hash.encode())


def create_access_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": _dt.datetime.utcnow() + _dt.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str):
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def search_leads_by_phone(phone: str):
    """
    Every scored lead whose phone number contains the given digits (partial
    match, so "98765" finds "9876543210"), newest first. Used by the
    dashboard's phone search box.
    """
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM lead_scores WHERE customer_phone LIKE %s ORDER BY id DESC",
        (f"%{phone}%",),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows


def _phone_key(phone):
    """
    Turns a phone number into a comparable key: digits only, last 10 digits.
    So "+91 98765-43210", "098765 43210" and "9876543210" are all treated
    as the SAME customer. Returns None if there is no phone on file.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return None
    return digits[-10:]


def _dedupe_by_phone(rows):
    """
    Keeps ONE row per phone number. Rows must already be sorted newest
    first, so the first row seen for a phone is the newest one and the
    older copies are dropped. Rows with no phone are all kept (nothing to
    compare them with).
    """
    seen = set()
    result = []
    for row in rows:
        key = _phone_key(row.get("customer_phone"))
        if key:
            if key in seen:
                continue
            seen.add(key)
        result.append(row)
    return result


def get_bucket_counts(bucket_type: str):
    """
    How many leads are in each bucket. A customer with several records
    (same phone number) is counted ONCE per bucket - the same rule used by
    get_leads_by_bucket, so the numbers on the cards always match the list.
    """
    column = "secured_bucket" if bucket_type == "secured" else "unsecured_bucket"
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        f"SELECT {column} AS bucket, customer_phone FROM lead_scores "
        f"ORDER BY application_id DESC, id DESC"
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    counts = {b: 0 for b in ["Very Low", "Low", "Medium", "Good", "Excellent"]}
    seen_by_bucket = {b: set() for b in counts}
    for row in rows:
        bucket_name = row["bucket"]
        if bucket_name not in counts:
            continue
        key = _phone_key(row["customer_phone"])
        if key:
            if key in seen_by_bucket[bucket_name]:
                continue
            seen_by_bucket[bucket_name].add(key)
        counts[bucket_name] += 1
    return counts


def get_leads_by_bucket(bucket_type: str, bucket_name: str):
    """
    Leads in one bucket, newest first, ONE row per phone number.
    "Newest" = highest application_id (then highest row id).
    """
    column = "secured_bucket" if bucket_type == "secured" else "unsecured_bucket"
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        f"SELECT * FROM lead_scores WHERE {column} = %s "
        f"ORDER BY application_id DESC, id DESC",
        (bucket_name,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return _dedupe_by_phone(rows)


def get_lead_by_id(lead_id: int):
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM lead_scores WHERE id = %s", (lead_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row

def get_customer_application_history(application_id: int):
    """
    Every OTHER application made by the same customer as this one, with the
    lender (provider) it was sent to and the ticket status. Reads from OMS
    (read-only). Newest first. Each row has is_earlier = True if that
    application was made before this one.
    """
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT
            ca.id AS application_id,
            ca.provider AS lender,
            ca.loan_type,
            ca.loan_category,
            ca.amount,
            ca.application_date,
            t.id AS ticket_id,
            t.status AS ticket_status,
            t.disbursed_amount
        FROM customer_application ca
        LEFT JOIN tickets t ON t.customer_application_id = ca.id
        WHERE ca.customer_id = (
                SELECT customer_id FROM customer_application WHERE id = %s
              )
          AND ca.id != %s
        ORDER BY ca.application_date DESC, ca.id DESC
    """, (application_id, application_id))
    rows = cursor.fetchall()

    cursor.execute(
        "SELECT application_date FROM customer_application WHERE id = %s",
        (application_id,),
    )
    this_row = cursor.fetchone()
    cursor.close()
    conn.close()

    this_date = this_row["application_date"] if this_row else None
    for row in rows:
        for key in ("amount", "disbursed_amount"):
            if row.get(key) is not None:
                row[key] = float(row[key])
        row["is_earlier"] = bool(
            this_date and row["application_date"] and row["application_date"] < this_date
        )
        if row["application_date"] is not None:
            row["application_date"] = row["application_date"].isoformat()
    return rows


def ensure_assessments_table():
    """Analyst vs banker eligibility notes, one row per entry (history is kept)."""
    conn = get_results_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lead_assessments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ticket_id INT NOT NULL,
                analyst_eligibility BIGINT NULL,
                banker_eligibility BIGINT NULL,
                variation_reason TEXT NULL,
                user_id INT,
                username VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_assess_ticket (ticket_id)
            )
        """)
        conn.commit()
    except mysql.connector.errors.ProgrammingError as e:
        if e.errno != 1142:
            raise
    finally:
        cursor.close()
        conn.close()


def add_assessment(ticket_id, analyst, banker, reason, user_id, username):
    conn = get_results_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO lead_assessments (ticket_id, analyst_eligibility, banker_eligibility, "
            "variation_reason, user_id, username) VALUES (%s, %s, %s, %s, %s, %s)",
            (ticket_id, analyst, banker, reason, user_id, username))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def get_assessments(ticket_id):
    """Newest first."""
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM lead_assessments WHERE ticket_id = %s ORDER BY created_at DESC, id DESC",
                       (ticket_id,))
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()
    for row in rows:
        if row.get("created_at"):
            row["created_at"] = row["created_at"].isoformat()
    return rows


def add_remark(ticket_id: int, remark: str, user_id: int, username: str):
    conn = get_results_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO lead_remarks (ticket_id, remark, user_id, username) VALUES (%s, %s, %s, %s)",
        (ticket_id, remark, user_id, username)
    )
    conn.commit()
    cursor.close()
    conn.close()

def get_remarks(ticket_id: int):
    conn = get_results_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM lead_remarks WHERE ticket_id = %s ORDER BY created_at ASC",
        (ticket_id,)
    )
    rows = cursor.fetchall()
    for row in rows:
        # Convert datetime to string for json serialization
        if 'created_at' in row and row['created_at']:
            row['created_at'] = row['created_at'].isoformat()
    cursor.close()
    conn.close()
    return rows