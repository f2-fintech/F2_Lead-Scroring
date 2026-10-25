"""
loan_rules.py - ONE place for the loan rules used by ingest, the bucket logic
and the filters. Put this file in the SAME folder as server.py and config.py.

What lives here
  * which loan types are SECURED            -> is_secured_loan_type()
  * a clean loan type name (Home Loan ...)  -> clean_loan_type()
  * a clean bank name (HDFC Bank ...)       -> clean_bank_name()
  * loan size slabs (Tiny / Small / ...)    -> loan_slab()
  * is a loan still running TODAY?          -> is_loan_active()

Secured loans (everything else is unsecured, including normal credit cards):
  Home Loan / Mortgage, Loan Against Property (LAP), Business Loan,
  Auto / Vehicle Loan, Gold Loan, Loan Against Securities (shares, mutual
  funds, bonds, insurance), Secured Credit Card, Loan Against FD.

Nothing here talks to a database, so it is safe to import from anywhere.
"""
import re
from datetime import date, datetime

# ---------------------------------------------------------------- loan types

SECURED_LABELS = {
    "Home Loan",
    "Loan Against Property",
    "Business Loan",
    "Auto Loan",
    "Gold Loan",
    "Loan Against Securities",
    "Secured Credit Card",
    "Loan Against FD",
}

# (clean label, pattern). The FIRST pattern that matches wins, so the more
# specific ones come first (e.g. "secured credit card" before "credit card").
_TYPE_RULES = [
    ("Secured Credit Card", r"\bsecured\s+(credit\s+)?card|card\s+against\s+(fd|fixed)"),
    ("Loan Against FD", r"against\s+(an\s+)?(fd|fixed\s+deposit|deposit)|\bfd\s+loan|\bfd\s+backed"),
    ("Loan Against Securities",
     r"against\s+(share|securit|mutual|mf\b|bond|insurance|lic\b|polic)|\blas\b|share\s+loan"),
    ("Loan Against Property",
     r"\blap\b|against\s+property|property\s+loan|commercial\s+property"),
    ("Home Loan",
     r"home\s+loan|housing|mortgage|home\s+(improvement|extension|top)|\bhl\b"),
    ("Business Loan", r"business\s+loan|\bmsme\b"),
    ("Auto Loan",
     r"\bauto\b|car\s+loan|\bcar\b|vehicle|two[\s-]?wheeler|2[\s-]?wheeler|\bbike\b|"
     r"motorcycle|scooter|\bcv\b"),
    ("Gold Loan", r"\bgold\b"),
    ("Credit Card", r"credit\s*card|\bcc\b|charge\s+card"),
    ("Professional Loan", r"professional"),
    ("Personal Loan", r"personal"),
    ("Education Loan", r"education|student"),
    ("Consumer Loan", r"consumer|durable|\bcd\s+loan"),
    ("Overdraft", r"overdraft|\bod\b|cash\s+credit"),
]
_TYPE_RULES = [(label, re.compile(pattern, re.I)) for label, pattern in _TYPE_RULES]


def _norm_text(value):
    return " ".join(str(value or "").replace("_", " ").split())


def clean_loan_type(loan_type):
    """'Auto Loan ' / 'AUTO LOAN' -> 'Auto Loan'.  Unknown types keep their own
    (tidied) name.  Returns None when there is no loan type at all."""
    text = _norm_text(loan_type)
    if not text:
        return None
    if re.search(r"\bunsecured\b", text, re.I):
        # "Unsecured credit card", "Unsecured business loan" ... stay unsecured
        text_for_match = re.sub(r"\bunsecured\b", "", text, flags=re.I)
        for label, pattern in _TYPE_RULES:
            if label in ("Secured Credit Card",):
                continue
            if pattern.search(text_for_match):
                return label if label not in SECURED_LABELS else f"Unsecured {label}"
        return text.title()
    for label, pattern in _TYPE_RULES:
        if pattern.search(text):
            return label
    return text.title()


def is_secured_loan_type(loan_type, type_map=None):
    """
    True when this (existing) loan is a SECURED one.

    type_map is optional: {lowercase loan type: 'secured' | 'unsecured'} (your
    loan_type_map table). If the loan type is written EXACTLY like a key in it,
    your choice wins; otherwise the keyword rules above decide. A type nobody
    knows is treated as unsecured.
    """
    text = _norm_text(loan_type)
    if not text:
        return False
    if type_map:
        chosen = type_map.get(text.lower())
        if chosen in ("secured", "unsecured"):
            return chosen == "secured"
    return clean_loan_type(text) in SECURED_LABELS


# ---------------------------------------------------------------- bank names

_CORPORATE_WORDS = re.compile(
    r"\b(limited|ltd|pvt|private|public|co|company|corp|corporation|inc|the|of india)\b", re.I)

# Names that are always written in capitals.
_ACRONYMS = {
    "hdfc", "icici", "sbi", "rbl", "idfc", "hsbc", "pnb", "bob", "bobcard", "au", "idbi",
    "uco", "lic", "hdb", "tvs", "dcb", "ujjivan", "aditya", "ltfs", "dbs", "cs", "ibl", "iifl",
    "kvb", "csb", "nbfc", "emi", "ncc", "avanse", "fedfina", "ndfc", "mas", "ugro",
}
_ACRONYMS.discard("aditya")
_ACRONYMS.discard("ujjivan")
_LOWER_WORDS = {"of", "and", "the", "for"}

# lowercase (corporate words removed)  ->  the one name we use
_BANK_ALIASES = {
    "sbi": "SBI",
    "state bank": "SBI",
    "state bank india": "SBI",
    "sbi cards": "SBI Cards",
    "sbi card": "SBI Cards",
    "sbi card payment services": "SBI Cards",
    "hdfc": "HDFC Bank",
    "hdfc bank": "HDFC Bank",
    "icici": "ICICI Bank",
    "icici bank": "ICICI Bank",
    "axis": "Axis Bank",
    "axis bank": "Axis Bank",
    "kotak": "Kotak Mahindra Bank",
    "kotak mahindra bank": "Kotak Mahindra Bank",
    "kotak mahindra prime": "Kotak Mahindra Prime",
    "bajaj finance": "Bajaj Finance",
    "bajaj fin": "Bajaj Finance",
    "bajaj finserv": "Bajaj Finserv",
    "bob": "Bank of Baroda",
    "bank baroda": "Bank of Baroda",
    "bobcard": "BOBCARD",
    "pnb": "Punjab National Bank",
    "punjab national bank": "Punjab National Bank",
    "idfc first bank": "IDFC First Bank",
    "idfc bank": "IDFC First Bank",
    "idfc first": "IDFC First Bank",
    "yes bank": "Yes Bank",
    "indusind bank": "IndusInd Bank",
    "indusind": "IndusInd Bank",
    "tata capital financial services": "Tata Capital",
    "tata capital": "Tata Capital",
    "hdb financial services": "HDB Financial Services",
    "au small finance bank": "AU Small Finance Bank",
    "au bank": "AU Small Finance Bank",
    "rbl bank": "RBL Bank",
    "rbl": "RBL Bank",
    "canara bank": "Canara Bank",
    "union bank": "Union Bank",
    "union bank india": "Union Bank",
    "indian bank": "Indian Bank",
}


def clean_bank_name(member_name):
    """One tidy name for a bank / NBFC: 'BOBCARD LIMITED' -> 'BOBCARD',
    'Bajaj Finance Limited' -> 'Bajaj Finance', 'SBI Cards ' -> 'SBI Cards'.
    Add more spellings to _BANK_ALIASES whenever you spot a new one."""
    text = _norm_text(member_name)
    if not text:
        return None
    key = re.sub(r"[^a-z0-9& ]", " ", text.lower())
    key = _CORPORATE_WORDS.sub(" ", key)
    key = " ".join(key.split())
    if not key:
        return text.title()
    if key in _BANK_ALIASES:
        return _BANK_ALIASES[key]
    words = []
    for i, w in enumerate(key.split()):
        if w in _ACRONYMS or re.fullmatch(r"[a-z]&[a-z]", w):
            words.append(w.upper())
        elif w in _LOWER_WORDS and i > 0:
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


# ---------------------------------------------------------------- loan size slabs

LAKH = 100000

# (upper limit in rupees - NOT included, label). Change the numbers here and
# every filter and screen follows.
SLABS = [
    (1 * LAKH, "Tiny"),      # below 1 lakh
    (5 * LAKH, "Small"),     # 1 lakh  to below 5 lakh
    (25 * LAKH, "Medium"),   # 5 lakh  to below 25 lakh
]
LAST_SLAB = "Large"          # 25 lakh and above
SLAB_NAMES = [label for _, label in SLABS] + [LAST_SLAB]
SLAB_HELP = {
    "Tiny": "below 1 lakh", "Small": "1 to 5 lakh",
    "Medium": "5 to 25 lakh", "Large": "25 lakh and above",
}


def loan_slab(sanction_amount, current_balance=None):
    """Slab of ONE loan from its sanction amount (current balance if that is missing)."""
    amount = sanction_amount if sanction_amount not in (None, "") else current_balance
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    for limit, label in SLABS:
        if amount < limit:
            return label
    return LAST_SLAB


# "Many small loans": this many running loans ...
MANY_SMALL_MIN_LOANS = 5
# ... of which at least this share are Tiny or Small.
MANY_SMALL_SHARE = 0.6


# ---------------------------------------------------------------- is the loan still running?

def parse_loan_date(value):
    """date / datetime / '2028-12-31' / '2028-12-31 00:00:00' -> date, else None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def is_loan_active(end_date, current_balance=None, as_of=None):
    """
    Is this loan still running on `as_of` (default: TODAY)?
      * It has an end date  -> running only if the end date is today or later.
      * No end date (credit cards, overdrafts) -> running unless its current
        balance is exactly 0.
    """
    as_of = as_of or date.today()
    ended = parse_loan_date(end_date)
    if ended is not None:
        return ended >= as_of
    if current_balance in (None, ""):
        return True
    try:
        return float(current_balance) > 0
    except (TypeError, ValueError):
        return True


def refresh_loans(loans, as_of=None):
    """
    Takes the loans saved from the CAM and re-checks every one against TODAY.
    Returns NEW loan dicts (the saved ones are not changed):
      loan_completed  - True when the end date has passed (or a card is at 0)
      emi_amount      - the original CAM EMI while running, 0 once it has ended
    This is what keeps the app right months after the CAM was read.
    """
    out = []
    for loan in (loans or []):
        copy = dict(loan)
        active = is_loan_active(loan.get("end_date"), loan.get("current_balance"), as_of)
        reported = loan.get("emi_amount_reported")
        if reported is None:
            reported = loan.get("emi_amount")
        copy["emi_amount_reported"] = reported
        copy["loan_completed"] = not active
        copy["emi_amount"] = (reported or 0) if active else 0.0
        out.append(copy)
    return out