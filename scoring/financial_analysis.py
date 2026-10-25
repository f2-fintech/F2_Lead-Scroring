"""
Deterministic financial analysis for a lead. NO AI anywhere in this file -
every number is computed from the CAM with plain arithmetic, so it can be
verified by hand against the source document. CIBIL reports are NOT used
(only the CIBIL SCORE written in the CAM is used, and only as a cap on the
SECURED bucket - see SECURED_CIBIL_CAPS).

Two dimensions are built here:
  1. Financial Capacity  - income, FOIR, eligible capacity, running loans and
                           their EMI, monthly surplus, secured/unsecured
                           eligibility + capacity buckets
  2. Financial Stability - age, experience, FD/investments (cushion),
                           SIP/insurance/credit-card (recurring commitments)

The capacity bucket can be CAPPED DOWN (never up) by independent hard
caps, each applied via the same "worst wins" rule, in this order:
  - Age (lending policy limits - see categorize_age)
  - CIBIL score (SECURED bucket only - see categorize_cibil_cap)
  - negative_remaining_capacity (already over FOIR-allowed capacity)

Loans are re-checked against TODAY's date every time buckets are worked out:
a loan whose end date has passed is not running, so its EMI is not counted.
That keeps the buckets right months after the CAM was read.
Every cap that actually fires is recorded with a human-readable reason, so
nothing about the final bucket is silent or unexplained.

Plus: missing-data handling (a missing factor is EXCLUDED, never scored as
zero) and an Analysis Confidence rating.

SECURED BUCKET (new method) - three layers, all plain arithmetic:
  1. Capacity : monthly surplus x the number of months of TENURE LEFT
                (age-aware: the closer to retirement, the shorter the tenure,
                the smaller the amount). No interest rate is assumed.
  2. Quality  : plus / minus points (house, experience, assets, enquiries,
                small-loan EMI burden). Net +2 or more = one bucket UP,
                net -2 or less = one bucket DOWN. Never more than one step.
  3. Hard caps: CIBIL cap (worst wins). Quality can never lift a bucket
                above a cap, and never lifts a lead that has no capacity.
  Missing income or FOIR -> "Insufficient data" (not a bucket).
All the numbers to tune are in the "SECURED BUCKET SETTINGS" block below.
The UNSECURED bucket is unchanged.

The AI step (extraction/gemini_client.py -> generate_bucket_explanation)
receives this profile and only explains it - it never calculates or changes
the bucket.
"""
from datetime import date
import re
from config import config
import loan_rules

SECURED_THRESHOLDS = [
    (50000000, "Excellent"),  # > 5 Cr
    (30000000, "Good"),       # > 3 Cr (up to 5 Cr)
    (10000000, "Medium"),     # 1 Cr - 3 Cr
]  # everything below 1 Cr = SECURED_BELOW_LAST ("Low")
SECURED_BELOW_LAST = "Low"

UNSECURED_THRESHOLDS = [
    (25000000, "Excellent"),  # > 2.5 Cr
    (15000000, "Good"),       # 1.5 Cr - 2.5 Cr
    (5000000, "Medium"),      # 50 L - 1.5 Cr
    (2500000, "Low"),         # 25 L - 50 L
]  # anything below the last threshold (25 L) = "Very Low"

BUCKET_ORDER = ["Very Low", "Low", "Medium", "Good", "Excellent"]
BUCKET_RANK = {b: i for i, b in enumerate(BUCKET_ORDER)}

# Age-based lending limits. These are hard policy limits - not a scoring
# signal, a ceiling on what is achievable regardless of income or experience.
#   SECURED   : the last age we lend is RETIREMENT_AGE (see the secured settings
#               below) - the tenure is the years left up to that age.
#   UNSECURED : the last age we lend is UNSECURED_END_AGE. Above it we cannot
#               fund anything (eligibility is 0 and the bucket is Very Low).
UNSECURED_END_AGE = 65
AGE_VERY_LOW_CUTOFF = UNSECURED_END_AGE   # above this: Very Low, no exceptions
AGE_LOW_LOWER = 60            # 60-65 inclusive: Low (the last 5 years before the end age)
AGE_LOW_UPPER = UNSECURED_END_AGE
AGE_VERY_LOW_CAP = "Very Low"
AGE_LOW_CAP = "Low"

NEGATIVE_REMAINING_CAPACITY_CAP = "Low"

# CIBIL cap for the SECURED and the UNSECURED bucket (it can only pull the bucket DOWN).
# Read top to bottom: the first row whose "score above" is beaten gives the cap.
#   (score must be ABOVE this number, highest bucket allowed, short description)
# A lead with no CIBIL score in the CAM gets NO cap.
# >>> These are starting values - change the numbers or buckets here and the
#     whole app follows. <<<
SECURED_CIBIL_CAPS = [
    (750, None, "above 750 (very good)"),             # no cap - Excellent is possible
    (699, "Good", "700 to 750 (normal)"),
    (650, "Medium", "651 to 699 (acceptable)"),
    (-1, "Low", "650 or below (weak)"),
]


# ===================================================================
#                    SECURED BUCKET SETTINGS
#   Change a number here and the whole app follows.
# ===================================================================

# --- Layer 1: capacity -> loan amount ---------------------------------
SECURED_MAX_TENURE_YEARS = 20       # longest tenure we assume
RETIREMENT_AGE = 70                 # last age we lend a SECURED loan: tenure = RETIREMENT_AGE - age (never above the max)

# --- Layer 2: quality points ------------------------------------------
QUALITY_STEP_UP_AT = 2              # net points >= this  -> one bucket up
QUALITY_STEP_DOWN_AT = -2           # net points <= this  -> one bucket down
QUALITY_MIN_EXPERIENCE_YEARS = 5    # this many years of experience = +1 (everyone except the doctor rule below)
# DOCTORS: how many years of experience earn the +1 depends on the highest qualification.
DOCTOR_GRADUATE_MORE_THAN_YEARS = 7     # graduate degree (MBBS, BDS ...): MORE than 7 years
DOCTOR_POSTGRAD_MIN_YEARS = 5           # postgraduate degree (MD, MS, DNB ...): 5 years or more
# Investments / reserves (FD, mutual funds, shares ... whatever the CAM gives,
# added together). First row that is beaten wins. "MORE than" the amount.
#   (more than this many rupees, points)
INVESTMENT_RULES = [(5000000, +2), (2000000, +1)]    # > 50 lakh = +2, > 20 lakh = +1
INVESTMENT_ASSET_KEYS = ("fixed_deposit", "mutual_fund", "shares", "investments", "reserves")
# Rented house: with family (married) = -1, living single (unmarried) = -2.
# Rented and marital status not known = -1 (a missing fact is never the bigger minus).
RENTED_WITH_FAMILY_POINTS = -1
RENTED_SINGLE_POINTS = -2
HOSTEL_POINTS = -1
# (at least this many enquiries, points). First row that matches wins.
ENQUIRY_3M_RULES = [(5, -2), (3, -1)]
ENQUIRY_6M_RULES = [(11, -2)]           # MORE than 10 enquiries in 6 months = -2
# Small-loan EMI burden: only looked at when the person has this many
# small (Tiny / Small slab) RUNNING loans. Then their combined EMI as a
# share of monthly income decides the points. (EMI is already taken off
# the surplus, so tiny EMIs are NOT punished - only a real burden is.)
SMALL_LOANS_MIN_COUNT = 5
SMALL_LOAN_EMI_SHARE_MINUS_1 = 0.10   # share >= 10% of income -> -1
SMALL_LOAN_EMI_SHARE_MINUS_2 = 0.20   # share >  20% of income -> -2

INSUFFICIENT_DATA = "Insufficient data"   # shown instead of a bucket when income / FOIR is missing


def categorize_by_thresholds(amount, thresholds, below_last="Very Low"):
    if amount is None:
        return "Unknown"
    for cutoff, label in thresholds:
        if amount > cutoff:
            return label
    return below_last


# ---------------------------------------------------------------- categories

def categorize_income(monthly_income):
    if monthly_income is None:
        return "Unknown"
    if monthly_income <= 100000:
        return "Very Low"
    if monthly_income <= 300000:
        return "Low"
    if monthly_income <= 600000:
        return "Medium"
    if monthly_income <= 1000000:
        return "Good"
    return "Excellent"


def categorize_foir_by_percent(foir_percent):
    """Raw FOIR band from the percentage alone. Relativized afterward by
    evaluate_effective_foir_category() against real remaining capacity."""
    if foir_percent is None:
        return "Unknown"
    if foir_percent < 30:
        return "Very Low"
    if foir_percent < 40:
        return "Low"
    if foir_percent < 50:
        return "Medium"
    if foir_percent < 60:
        return "Good"
    return "Excellent"  # uncapped - high FOIR is fine IF capacity is actually left


def categorize_experience(years):
    """More experience = better bucket. These exact label names double as
    the fallback bucket when income is missing (see build_financial_profile)."""
    if not years:
        return "Unknown"
    if years < 3:
        return "Very Low"
    if years < 5:
        return "Low"
    if years < 10:
        return "Medium"
    if years < 15:
        return "Good"
    return "Excellent"


_MONTH_NUMBERS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Matches a date with a spelled-out or abbreviated month name, in either
# "23 Sept 2003" (day-month-year) or "May 14, 1975" (month-day-year)
# order, with an optional ordinal suffix and any of space/slash/hyphen/
# comma as the separator - covers "23/NOV/1990" and "Wednesday, May 14,
# 1975" as well. Doesn't rely on Python's %b/%B, which don't recognize
# 4-letter abbreviations like "Sept" on their own.
_SPELLED_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?[\s/\-,]+([a-zA-Z]+)[\s/\-,]+(\d{4})"
    r"|([a-zA-Z]+)[\s/\-,]+(\d{1,2})(?:st|nd|rd|th)?[\s/\-,]+(\d{4})"
)


def _parse_spelled_date(text):
    match = _SPELLED_DATE_RE.search(text)
    if not match:
        return None
    if match.group(1):
        day, month_word, year = match.group(1), match.group(2), match.group(3)
    else:
        month_word, day, year = match.group(4), match.group(5), match.group(6)

    month_num = _MONTH_NUMBERS.get(month_word.strip().lower())
    if not month_num:
        return None
    try:
        return date(int(year), month_num, int(day))
    except ValueError:
        return None


def parse_date_of_birth(value):
    """
    CAMs store date of birth in inconsistent formats - seen so far:
      - an actual Excel date/datetime cell (openpyxl gives us a
        datetime.date / datetime.datetime object directly)
      - "Wednesday, May 14, 1975" (weekday, month name, day, year)
      - "03/NOV/1990" (day/monthabbrev/year)
    Returns a datetime.date, or None if it can't be parsed.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        # datetime.datetime is a subclass check away
        if hasattr(value, "date"):
            return value.date()
    except Exception:
        pass

    text = str(value).strip()
    if not text:
        return None

    spelled = _parse_spelled_date(text)
    if spelled:
        return spelled

    from datetime import datetime
    # A date saved as text from a datetime ("1975-05-14 00:00:00" / ISO "...T...").
    # Stored CAM data was saved this way, so older leads have it.
    text = re.split(r"[T ]\d{1,2}:\d{2}", text)[0].strip()

    formats_to_try = [
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ]
    for fmt in formats_to_try:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def compute_age(date_of_birth, as_of: date = None):
    """Age in whole years as of today (or a given reference date)."""
    if date_of_birth is None:
        return None
    as_of = as_of or date.today()
    years = as_of.year - date_of_birth.year
    had_birthday_this_year = (as_of.month, as_of.day) >= (date_of_birth.month, date_of_birth.day)
    if not had_birthday_this_year:
        years -= 1
    return years


def categorize_age(age):
    """
    Hard lending-policy cap based on age - NOT a scoring signal, a ceiling
    on the achievable bucket regardless of income/experience.

    age > 65            -> Very Low, no exceptions (cannot fund)
    60 <= age <= 65      -> Low
    age < 60            -> no cap
    """
    if age is None:
        return None
    if age > AGE_VERY_LOW_CUTOFF:
        return AGE_VERY_LOW_CAP
    if AGE_LOW_LOWER <= age <= AGE_LOW_UPPER:
        return AGE_LOW_CAP
    return None


def categorize_cibil_cap(cibil_score, caps=None):
    """
    Highest SECURED bucket the CIBIL score allows, or None for "no cap".
    (None is also returned when the CAM has no CIBIL score.)
    Returns (cap_bucket_or_None, description_of_the_band).
    """
    if cibil_score is None:
        return None, None
    try:
        score = float(cibil_score)
    except (TypeError, ValueError):
        return None, None
    for above, cap, description in (caps or SECURED_CIBIL_CAPS):
        if score > above:
            return cap, description
    return None, None


# ------------------------------------------- SECURED: layer 1 (capacity)

def compute_secured_tenure(age):
    """
    Tenure (in years) we can give this applicant: the years left to
    RETIREMENT_AGE, never more than SECURED_MAX_TENURE_YEARS.
    Returns (tenure_years, limited_by_age, age_known).
    Unknown age -> the full maximum tenure (and age_known is False).
    """
    if age is None:
        return float(SECURED_MAX_TENURE_YEARS), False, False
    left = RETIREMENT_AGE - age
    tenure = max(0.0, min(float(SECURED_MAX_TENURE_YEARS), float(left)))
    return tenure, left < SECURED_MAX_TENURE_YEARS, True


# ------------------------------------------- SECURED: layer 2 (quality)

_POSTGRAD_WORDS = {"md", "ms", "dnb", "dm", "mch", "mds", "pg", "postgraduate", "postgrad", "masters", "master"}
_GRADUATE_WORDS = {"mbbs", "bds", "bams", "bhms", "bums", "bpt", "graduate", "graduation"}
_DOCTOR_WORDS = {"doctor", "dr", "physician", "surgeon", "dentist"}


def doctor_qualification_level(highest_degree, profession):
    """
    For DOCTORS only: "postgraduate" (MD, MS, DNB, DM, MCh, MDS ...) or
    "graduate" (MBBS, BDS, BAMS ...), read from the highest qualification text.
    Returns None for a non-doctor, or when the qualification is not recognised
    (the normal experience rule is used then).
    """
    def words(text):
        t = str(text or "").lower().replace(".", "")
        return set(re.sub(r"[^a-z0-9]+", " ", t).split())

    degree_words = words(highest_degree)
    prof_words = words(profession)
    is_doctor = bool(prof_words & _DOCTOR_WORDS) or bool(degree_words & {"mbbs", "bds", "bams", "bhms", "bums"})
    if not is_doctor:
        return None
    joined = " ".join(sorted(degree_words))
    if degree_words & _POSTGRAD_WORDS or "post graduate" in str(highest_degree or "").lower():
        return "postgraduate"
    if degree_words & _GRADUATE_WORDS:
        return "graduate"
    return None


def evaluate_secured_quality(*, house_type, experience_years, assets, enquiries_3m,
                             enquiries_6m, running_loans, monthly_income,
                             highest_degree=None, profession=None, marital_status=None):
    """
    Plus / minus points that can nudge the capacity bucket by ONE step.
    Returns {"score": int, "items": [{"label", "points"}], "small_loan_emi_share": float|None}.
    Anything missing simply scores nothing (a missing fact is never a minus).
    Existing EMI itself is NOT scored here - it is already taken off the
    surplus in layer 1, so scoring it again would punish the same loan twice.
    """
    items = []

    def add(label, points):
        items.append({"label": label, "points": points})

    # House
    if house_type == "Owned":
        add("Lives in an owned house", +1)
    elif house_type == "Rented":
        if marital_status == "Married":
            add("Lives in a rented house with family", RENTED_WITH_FAMILY_POINTS)
        elif marital_status == "Unmarried":
            add("Lives in a rented house, single", RENTED_SINGLE_POINTS)
        else:
            add("Lives in a rented house", RENTED_WITH_FAMILY_POINTS)
    elif house_type == "Hostel":
        add("Lives in a hostel / PG", HOSTEL_POINTS)

    # Experience
    try:
        years = float(experience_years) if experience_years not in (None, "") else None
    except (TypeError, ValueError):
        years = None
    level = doctor_qualification_level(highest_degree, profession)
    if years is not None:
        if level == "postgraduate":
            if years >= DOCTOR_POSTGRAD_MIN_YEARS:
                add(f"{years:g} years of experience as a doctor with a postgraduate degree", +1)
        elif level == "graduate":
            if years > DOCTOR_GRADUATE_MORE_THAN_YEARS:
                add(f"{years:g} years of experience as a doctor with a graduate degree", +1)
        elif years >= QUALITY_MIN_EXPERIENCE_YEARS:
            add(f"{years:g} years of experience", +1)

    # Investments / reserves found in the CAM (FD, mutual funds, shares ...) added together
    investments_total = sum(v for k, v in (assets or {}).items()
                            if k in INVESTMENT_ASSET_KEYS
                            and isinstance(v, (int, float)) and v > 0)
    for more_than, points in INVESTMENT_RULES:
        if investments_total > more_than:
            add(f"Investments / reserves are more than Rs.{more_than / 100000:g} lakh", points)
            break

    # Credit enquiries - take the worse of the 3-month and 6-month result
    worst_enq = (0, None)
    if enquiries_3m is not None:
        for at_least, points in ENQUIRY_3M_RULES:
            if enquiries_3m >= at_least:
                worst_enq = (points, f"{enquiries_3m} credit enquiries in the last 3 months")
                break
    if enquiries_6m is not None:
        for at_least, points in ENQUIRY_6M_RULES:
            if enquiries_6m >= at_least and points < worst_enq[0]:
                worst_enq = (points, f"{enquiries_6m} credit enquiries in the last 6 months")
                break
    if worst_enq[1]:
        add(worst_enq[1], worst_enq[0])

    # Many small running loans AND a real EMI burden from them
    small_share = None
    small = [l for l in (running_loans or [])
             if loan_rules.loan_slab(l.get("sanction_amount"), l.get("current_balance"))
             in ("Tiny", "Small")]
    if len(small) >= SMALL_LOANS_MIN_COUNT and monthly_income:
        small_emi = sum((l.get("emi_amount") or 0) for l in small)
        small_share = small_emi / monthly_income
        if small_share > SMALL_LOAN_EMI_SHARE_MINUS_2:
            add(f"{len(small)} small running loans take {small_share:.0%} of monthly income as EMI", -2)
        elif small_share >= SMALL_LOAN_EMI_SHARE_MINUS_1:
            add(f"{len(small)} small running loans take {small_share:.0%} of monthly income as EMI", -1)

    return {
        "score": sum(i["points"] for i in items),
        "items": items,
        "small_loan_emi_share": round(small_share, 3) if small_share is not None else None,
    }


def apply_quality_step(bucket, quality, has_capacity):
    """
    Moves the bucket ONE step up or down from the quality score.
    No step up when the lead has no capacity at all (surplus <= 0 or no tenure
    left) - quality can't create room that is not there.
    Returns (new_bucket, step_taken, reason_or_None).
    """
    if bucket not in BUCKET_RANK:
        return bucket, 0, None
    score = quality["score"]
    step = 0
    if score >= QUALITY_STEP_UP_AT and has_capacity:
        step = +1
    elif score <= QUALITY_STEP_DOWN_AT:
        step = -1
    new_rank = min(max(BUCKET_RANK[bucket] + step, 0), len(BUCKET_ORDER) - 1)
    new_bucket = BUCKET_ORDER[new_rank]
    if new_bucket == bucket:
        return bucket, 0, None
    direction = "up" if step > 0 else "down"
    return new_bucket, step, (
        f"Quality points add up to {score:+d}, which moved the bucket one step "
        f"{direction}, from '{bucket}' to '{new_bucket}'.")


# --------------------------------------------------- hard-cap application

def apply_hard_caps(capacity_bucket, *, age, age_cap, negative_remaining_capacity,
                    cibil_score=None, cibil_cap=None, cibil_band=None, bucket_name="secured"):
    """
    Applies every independent hard cap using the same "worst wins" rule:
    the final bucket is the WORST (lowest-ranked) of the capacity bucket and
    every applicable cap. Nothing can push the bucket UP - caps only ever
    pull it down or leave it unchanged.

    Returns (final_bucket, list_of_reason_strings) - reasons are only
    included for caps that actually changed something.
    """
    if capacity_bucket not in BUCKET_RANK:
        return capacity_bucket, []  # e.g. "Unknown" - nothing to cap

    current_bucket = capacity_bucket
    reasons = []

    # --- Age cap ---
    if age_cap and age_cap in BUCKET_RANK:
        if BUCKET_RANK[age_cap] < BUCKET_RANK[current_bucket]:
            if age_cap == AGE_VERY_LOW_CAP:
                bracket_desc = f"age {age} is above {AGE_VERY_LOW_CUTOFF}"
            else:
                bracket_desc = f"age {age} falls in the {AGE_LOW_LOWER}-{AGE_LOW_UPPER} age bracket"
            reasons.append(
                f"Applicant {bracket_desc}, which our lending policy caps at "
                f"'{age_cap}' - this capped the bucket from '{current_bucket}' "
                f"down to '{age_cap}'."
            )
            current_bucket = age_cap

    # --- CIBIL cap (only passed in for the SECURED bucket) ---
    if cibil_cap and cibil_cap in BUCKET_RANK:
        if BUCKET_RANK[cibil_cap] < BUCKET_RANK[current_bucket]:
            reasons.append(
                f"CIBIL score {cibil_score:g} is {cibil_band}, which caps the {bucket_name} "
                f"bucket at '{cibil_cap}' - this capped the bucket from "
                f"'{current_bucket}' down to '{cibil_cap}'."
            )
            current_bucket = cibil_cap

    # --- Negative remaining capacity cap ---
    if negative_remaining_capacity and NEGATIVE_REMAINING_CAPACITY_CAP in BUCKET_RANK:
        if BUCKET_RANK[NEGATIVE_REMAINING_CAPACITY_CAP] < BUCKET_RANK[current_bucket]:
            reasons.append(
                f"Remaining capacity is negative (existing EMI already exceeds "
                f"FOIR-allowed capacity) - capped the bucket from "
                f"'{current_bucket}' down to '{NEGATIVE_REMAINING_CAPACITY_CAP}'."
            )
            current_bucket = NEGATIVE_REMAINING_CAPACITY_CAP

    return current_bucket, reasons


def compute_monthly_surplus(eligible_capacity, existing_emi, recurring_commitments_total):
    """
    (Income x FOIR%) minus ALL obligations - existing EMIs (secured +
    unsecured combined) AND recurring commitments (SIP/insurance/mutual
    funds/etc, whatever was found in the CAM). This is the number the
    secured/unsecured eligibility formulas are built on.
    """
    if eligible_capacity is None:
        return None
    return eligible_capacity - (existing_emi or 0) - (recurring_commitments_total or 0)


def compute_secured_cap_eligibility(monthly_surplus, loan_per_emi_factor=100):
    """Loan amount the surplus can repay. The factor is the number of months of
    tenure left (no interest rate is assumed)."""
    if monthly_surplus is None:
        return None
    return monthly_surplus * loan_per_emi_factor


def compute_unsecured_cap_eligibility(monthly_surplus):
    if monthly_surplus is None:
        return None
    return (monthly_surplus / 2) * 100


# ------------------------------------------------------- capacity waterfall

def compute_eligible_capacity(monthly_income, foir_percent):
    """How much of this person's income FOIR allows toward obligations."""
    if not monthly_income or foir_percent is None:
        return None
    return monthly_income * (foir_percent / 100)


def compute_remaining_capacity(eligible_capacity, existing_emi):
    """Eligible capacity minus what's already being paid. Can go negative -
    that is itself a meaningful signal (already over capacity)."""
    if eligible_capacity is None:
        return None
    return eligible_capacity - (existing_emi or 0)


def compute_remaining_capacity_ratio(remaining_capacity, eligible_capacity):
    """Normalized 0-1 (or negative) so it's comparable across income levels."""
    if remaining_capacity is None or not eligible_capacity:
        return None
    return remaining_capacity / eligible_capacity


def compute_disposable_income(monthly_income, existing_emi, recurring_commitments_total):
    """
    Actual cash left in hand = income - EMIs - other recurring commitments
    (insurance, SIP, credit card payments etc. found in the CAM).

    Different from remaining capacity: remaining capacity is a FOIR-based
    ALLOWANCE; this is real leftover CASH.
    """
    if not monthly_income:
        return None
    return monthly_income - (existing_emi or 0) - (recurring_commitments_total or 0)


def evaluate_effective_foir_category(raw_foir_category, remaining_capacity_ratio):
    """
    A high FOIR band only holds up if real capacity is actually left. If
    obligations already ate the allowance, the label is downgraded - a high
    FOIR with nothing left over is not a good sign.
    """
    if raw_foir_category not in ("Good", "Excellent"):
        return raw_foir_category
    if remaining_capacity_ratio is None:
        return raw_foir_category
    if remaining_capacity_ratio < 0.15:
        return "Low"
    if remaining_capacity_ratio < 0.35:
        return "Medium"
    return raw_foir_category


# ---------------------------------------------------------- loan  exposure

def analyze_loan_exposure(loans, monthly_income, existing_emi):
    """
    Facts + ratios about the loan portfolio. Produces NO score - kept as
    supporting context for the AI explanation, since crossing a threshold
    is never good or bad on its own. (extreme_unsecured_exposure is parked
    for now - not currently used to cap the bucket, per team decision.)
    """
    secured_total = 0
    unsecured_total = 0
    for loan in (loans or []):
        amount = loan.get("sanction_amount") or 0
        is_secured = loan_rules.is_secured_loan_type(loan.get("loan_type"))
        if is_secured:
            secured_total += amount
        else:
            unsecured_total += amount

    total_exposure = secured_total + unsecured_total
    annual_income = monthly_income * 12 if monthly_income else None

    return {
        "loan_count": len(loans or []),
        "secured_total": secured_total,
        "unsecured_total": unsecured_total,
        "total_exposure": total_exposure,
        "secured_over_threshold": secured_total > config.SECURED_LOAN_THRESHOLD,
        "unsecured_over_threshold": unsecured_total > config.UNSECURED_LOAN_THRESHOLD,
        "exposure_to_annual_income_ratio": (
            round(total_exposure / annual_income, 2) if annual_income else None),
        "unsecured_to_annual_income_ratio": (
            round(unsecured_total / annual_income, 2) if annual_income else None),
        "emi_to_income_ratio": (
            round((existing_emi or 0) / monthly_income, 2) if monthly_income else None),
    }


# --------------------------------------------------------- running loans

EMI_MISMATCH_TOLERANCE = 500  # rupees


def summarize_running_loans(loans, existing_emi_cell, tradeline_emi_total, monthly_income, emi_check=None):
    """
    Everything about the loans the applicant is ALREADY paying, taken from
    the CAM: how many, total outstanding, and total monthly EMI.

    EMI used for the analysis = OUR OWN sum of the individual tradeline
    rows (tradeline_emi_total), NOT the CAM's manually-typed EXISTING EMI
    cell. This is deliberate: the tradeline rows have Ending Dates, so a
    loan that's already finished is already counted as Rs.0 EMI there
    (see cam_extractor.py's completed-loan handling) - the analyst-typed
    cell is just one manually-typed number and can't be corrected the same
    way, so it's no longer trusted as the primary figure. It's only used
    as a fallback when there are no tradeline rows to sum at all (e.g. a
    CAM with zero running loans reported through the rows but a non-zero
    typed EMI for some other reason).
    """
    loans = loans or []
    if tradeline_emi_total is not None:
        emi_used, emi_source = tradeline_emi_total, "sum of individual loan rows (completed loans excluded)"
    elif existing_emi_cell is not None:
        emi_used, emi_source = existing_emi_cell, "CAM EXISTING EMI cell (no loan rows to sum)"
    else:
        emi_used, emi_source = None, None

    emi_mismatch = (
        existing_emi_cell is not None and tradeline_emi_total is not None
        and abs(existing_emi_cell - tradeline_emi_total) > EMI_MISMATCH_TOLERANCE
    )

    return {
        "running_loan_count": sum(1 for l in loans if not l.get("loan_completed")),
        "total_outstanding_balance": sum(
            (l.get("current_balance") or 0) for l in loans if not l.get("loan_completed")),
        "emi_used": emi_used,
        "emi_source": emi_source,
        "emi_from_loan_rows": tradeline_emi_total,
        "emi_cell_vs_loan_rows_mismatch": emi_mismatch,
        "emi_check": emi_check,  # does the typed total EMI still include finished loans?
        "emi_to_income_percent": (
            round(emi_used / monthly_income * 100, 1)
            if emi_used is not None and monthly_income else None),
        "loans": [
            {
                "lender": l.get("lender"),
                "loan_type": l.get("loan_type"),
                "ownership": l.get("ownership"),
                "sanction_amount": l.get("sanction_amount"),
                "current_balance": l.get("current_balance"),
                "emi_amount": l.get("emi_amount"),                   # 0 if loan_completed
                "emi_amount_reported": l.get("emi_amount_reported"),  # CAM's original figure
                "tenure_months": l.get("tenure_months"),
                "end_date": l.get("end_date"),
                "loan_completed": l.get("loan_completed", False),
            }
            for l in loans
        ],
    }


# ------------------------------------------- confidence + hard  risk  flags

CORE_FIELDS = ["monthly_income", "foir_percent", "existing_emi"]


def assess_data_confidence(available: list, missing: list):
    """
    High   = all three core fields present
    Medium = one core field missing
    Low    = two or more core fields missing

    Missing FD/SIP/insurance does NOT lower confidence - those are optional
    stability signals, and absence simply means no such obligation exists.
    """
    missing_core = [f for f in CORE_FIELDS if f in missing]
    if not missing_core:
        return "High"
    if len(missing_core) == 1:
        return "Medium"
    return "Low"


def compute_hard_flags(remaining_capacity, loan_exposure):
    """
    Deterministic risk facts, computed in Python. negative_remaining_capacity
    actively caps the bucket (see apply_hard_caps). extreme_unsecured_exposure
    is informational only (shown to the AI as context).
    """
    unsecured_ratio = loan_exposure.get("unsecured_to_annual_income_ratio")
    return {
        "negative_remaining_capacity": (
            remaining_capacity is not None and remaining_capacity < 0),
        "extreme_unsecured_exposure": (
            unsecured_ratio is not None
            and unsecured_ratio > config.EXTREME_UNSECURED_EXPOSURE_RATIO),
    }


# ------------------------------------------------------------ main  builder

def build_financial_profile(cam_fields: dict, prior_disbursed_amount: float = 0,
                            as_of: date = None) -> dict:
    """
    Main entry point. Turns the CAM fields into the full structured profile
    handed to the AI for the bucket explanation. Uses the CAM only.
    """
    prior_disbursed_amount = prior_disbursed_amount or 0

    monthly_income = cam_fields.get("total_monthly_income")
    foir_percent = cam_fields.get("foir_percent")
    experience_years = cam_fields.get("experience_years")
    # Re-check every loan against TODAY: a loan whose end date has passed is
    # finished, so its EMI is not counted. (The CAM was read some time ago -
    # loans may have ended since.)
    loans = loan_rules.refresh_loans(cam_fields.get("loans", []) or [], as_of)
    running_only = [l for l in loans if not l.get("loan_completed")]
    tradeline_emi_now = (
        sum((l.get("emi_amount") or 0) for l in loans) if loans else None)
    commitments = cam_fields.get("recurring_commitments", {}) or {}
    assets = cam_fields.get("financial_assets", {}) or {}

    running_loans = summarize_running_loans(
        loans, cam_fields.get("existing_emi"),
        tradeline_emi_now, monthly_income,
        emi_check=cam_fields.get("emi_check"))
    existing_emi = running_loans["emi_used"]

    date_of_birth = parse_date_of_birth(cam_fields.get("date_of_birth"))
    age_from_dob = compute_age(date_of_birth, as_of)
    age_direct = cam_fields.get("age_direct")

    if age_direct is not None:
        age = age_direct
        age_source = "cam_direct_age_field"
    elif age_from_dob is not None:
        age = age_from_dob
        age_source = "computed_from_date_of_birth"
    else:
        age = None
        age_source = None
    age_cap = categorize_age(age)

    commitments_total = sum(v for v in commitments.values() if isinstance(v, (int, float)))
    experience_category = categorize_experience(experience_years)

    # --- Dimension 1: Financial Capacity ---
    eligible_capacity = compute_eligible_capacity(monthly_income, foir_percent)
    remaining_capacity = compute_remaining_capacity(eligible_capacity, existing_emi)
    remaining_capacity_ratio = compute_remaining_capacity_ratio(remaining_capacity, eligible_capacity)
    disposable_income = compute_disposable_income(monthly_income, existing_emi, commitments_total)

    raw_foir_category = categorize_foir_by_percent(foir_percent)
    effective_foir_category = evaluate_effective_foir_category(raw_foir_category, remaining_capacity_ratio)
    loan_exposure = analyze_loan_exposure(running_only, monthly_income, existing_emi)

    monthly_surplus = compute_monthly_surplus(eligible_capacity, existing_emi, commitments_total)
    # SECURED: how many rupees of loan one rupee of monthly EMI can repay,
    # = the number of months of tenure left before retirement (no interest rate).
    secured_tenure_years, tenure_limited_by_age, age_known = compute_secured_tenure(age)
    secured_loan_per_emi = secured_tenure_years * 12
    secured_cap_eligibility_raw = compute_secured_cap_eligibility(monthly_surplus, secured_loan_per_emi)
    unsecured_cap_eligibility_raw = compute_unsecured_cap_eligibility(monthly_surplus)

    secured_cap_eligibility = (
        max(0, secured_cap_eligibility_raw - prior_disbursed_amount)
        if secured_cap_eligibility_raw is not None else None
    )
    unsecured_cap_eligibility = (
        max(0, unsecured_cap_eligibility_raw - prior_disbursed_amount)
        if unsecured_cap_eligibility_raw is not None else None
    )

    # UNSECURED: above the end age we cannot fund anything at all.
    unsecured_age_blocked = age is not None and age > UNSECURED_END_AGE
    unsecured_zero_reason = None
    if unsecured_age_blocked:
        unsecured_cap_eligibility = 0
        unsecured_zero_reason = (
            f"Applicant is {age}, which is above the last lending age of "
            f"{UNSECURED_END_AGE} for an unsecured loan - nothing can be funded.")

    income_missing = monthly_income is None
    foir_missing = foir_percent is None

    # ---------------- UNSECURED (unchanged) ----------------
    if income_missing:
        # Income was not given in the CAM - nothing to run the formula on.
        unsecured_capacity_bucket = "Very Low"
    else:
        unsecured_capacity_bucket = categorize_by_thresholds(unsecured_cap_eligibility, UNSECURED_THRESHOLDS)

    hard_flags = compute_hard_flags(remaining_capacity, loan_exposure)

    cibil_score = cam_fields.get("cibil_score")
    cibil_cap, cibil_band = categorize_cibil_cap(cibil_score)

    unsecured_bucket, unsecured_cap_reasons = apply_hard_caps(
        unsecured_capacity_bucket,
        age=age,
        age_cap=age_cap,
        negative_remaining_capacity=hard_flags["negative_remaining_capacity"],
        cibil_score=cibil_score,
        cibil_cap=cibil_cap,
        cibil_band=cibil_band,
        bucket_name="unsecured",
    )

    if unsecured_zero_reason:
        unsecured_cap_reasons = [unsecured_zero_reason] + [
            r for r in unsecured_cap_reasons if r != unsecured_zero_reason]

    # ---------------- SECURED (new: capacity -> quality -> caps) ----------------
    secured_missing_inputs = []
    if income_missing:
        secured_missing_inputs.append("monthly income")
    if foir_missing:
        secured_missing_inputs.append("FOIR")

    secured_quality = evaluate_secured_quality(
        house_type=cam_fields.get("house_type"),
        marital_status=cam_fields.get("marital_status"),
        experience_years=experience_years,
        assets=assets,
        enquiries_3m=cam_fields.get("enquiries_3m"),
        enquiries_6m=cam_fields.get("enquiries_6m"),
        running_loans=running_only,
        monthly_income=monthly_income,
        highest_degree=cam_fields.get("highest_degree"),
        profession=cam_fields.get("profession"),
    )
    secured_quality_step, secured_quality_reason = 0, None
    bucket_after_quality = INSUFFICIENT_DATA
    secured_zero_reason = None
    secured_cap_reasons = []

    if secured_missing_inputs:
        # Cannot work out an amount - show it as its own group, not as a bucket.
        secured_capacity_bucket = INSUFFICIENT_DATA
        secured_bucket = INSUFFICIENT_DATA
        secured_cap_reasons = [
            "The CAM does not give " + " and ".join(secured_missing_inputs)
            + ", so the secured eligibility cannot be worked out."]
    else:
        secured_capacity_bucket = categorize_by_thresholds(
            secured_cap_eligibility, SECURED_THRESHOLDS, SECURED_BELOW_LAST)

        # Say clearly WHY the amount is zero, when it is.
        if secured_tenure_years <= 0:
            secured_zero_reason = (
                f"Applicant is {age}, so no tenure is left before the last lending age "
                f"of {RETIREMENT_AGE} for a secured loan - no secured loan amount is possible.")
        elif monthly_surplus is not None and monthly_surplus <= 0:
            secured_zero_reason = (
                "Existing EMIs and monthly commitments already use up all of the "
                "FOIR-allowed capacity, so there is nothing left for a new loan.")
        elif secured_cap_eligibility_raw is not None and secured_cap_eligibility_raw <= prior_disbursed_amount:
            secured_zero_reason = (
                f"The loan we already disbursed (Rs.{prior_disbursed_amount:,.0f}) "
                f"uses up all of the eligibility.")

        has_capacity = (secured_cap_eligibility or 0) > 0
        bucket_after_quality, secured_quality_step, secured_quality_reason = apply_quality_step(
            secured_capacity_bucket, secured_quality, has_capacity)

        # Age is handled by the tenure above, so no separate age cap here.
        secured_bucket, secured_cap_reasons = apply_hard_caps(
            bucket_after_quality,
            age=age,
            age_cap=None,
            negative_remaining_capacity=False,
            cibil_score=cibil_score,
            cibil_cap=cibil_cap,
            cibil_band=cibil_band,
        )
        lead_in = [r for r in (secured_zero_reason, secured_quality_reason) if r]
        secured_cap_reasons = lead_in + secured_cap_reasons

    financial_capacity = {
        "monthly_income": monthly_income,
        "income_category": categorize_income(monthly_income),
        "income_not_given": income_missing,
        "income_note": (
            "Income not given in the CAM - secured shows 'Insufficient data' and "
            "unsecured is Very Low."
            if income_missing else None),
        "foir_not_given": foir_missing,
        "secured_missing_inputs": secured_missing_inputs or None,
        "income_source": cam_fields.get("_income_source"),
        "foir_percent": foir_percent,
        "raw_foir_category": raw_foir_category,
        "effective_foir_category": effective_foir_category,
        "eligible_capacity": round(eligible_capacity) if eligible_capacity is not None else None,
        "existing_emi": existing_emi,
        "running_loans": running_loans,
        "recurring_commitments_total": commitments_total or None,
        "monthly_surplus": round(monthly_surplus) if monthly_surplus is not None else None,
        "secured_cap_eligibility": (
            round(secured_cap_eligibility) if secured_cap_eligibility is not None else None),
        "unsecured_cap_eligibility": (
            round(unsecured_cap_eligibility) if unsecured_cap_eligibility is not None else None),
        "prior_disbursed_amount": prior_disbursed_amount or None,
        "secured_cap_eligibility_before_prior_loans": (
            round(secured_cap_eligibility_raw) if secured_cap_eligibility_raw is not None else None),
        "unsecured_cap_eligibility_before_prior_loans": (
            round(unsecured_cap_eligibility_raw) if unsecured_cap_eligibility_raw is not None else None),
        "secured_method": {
            "tenure_years_used": secured_tenure_years,
            "max_tenure_years": SECURED_MAX_TENURE_YEARS,
            "retirement_age": RETIREMENT_AGE,
            "tenure_limited_by_age": tenure_limited_by_age,
            "age_known": age_known,
            "months_of_tenure": round(secured_loan_per_emi, 1),
            "loan_per_rupee_of_emi": round(secured_loan_per_emi, 1),   # same number, old name
        },
        "secured_quality": {
            "score": secured_quality["score"],
            "items": secured_quality["items"],
            "small_loan_emi_share": secured_quality["small_loan_emi_share"],
            "step": secured_quality_step,   # +1 up, -1 down, 0 none
            "reason": secured_quality_reason,
            "step_up_at": QUALITY_STEP_UP_AT,
            "step_down_at": QUALITY_STEP_DOWN_AT,
        },
        "secured_zero_reason": secured_zero_reason,
        "secured_capacity_bucket": secured_capacity_bucket,      # before quality + caps
        "unsecured_capacity_bucket": unsecured_capacity_bucket,  # before any caps
        "secured_bucket": secured_bucket,                        # FINAL decided bucket
        "unsecured_bucket": unsecured_bucket,                    # FINAL decided bucket
        "cibil_score": cibil_score,
        "secured_cibil_cap": cibil_cap,
        "unsecured_cibil_cap": cibil_cap,
        "secured_cibil_band": cibil_band,
        "secured_bucket_after_quality": bucket_after_quality,    # after quality step, before caps
        "secured_bucket_capped": secured_bucket != bucket_after_quality,   # a hard cap (CIBIL) lowered it
        "unsecured_bucket_capped": unsecured_bucket != unsecured_capacity_bucket,
        "secured_bucket_cap_reasons": secured_cap_reasons,
        "unsecured_bucket_cap_reasons": unsecured_cap_reasons,
        "unsecured_zero_reason": unsecured_zero_reason,
        "unsecured_end_age": UNSECURED_END_AGE,
        "secured_end_age": RETIREMENT_AGE,
        "remaining_capacity": round(remaining_capacity) if remaining_capacity is not None else None,
        "remaining_capacity_ratio": (
            round(remaining_capacity_ratio, 2) if remaining_capacity_ratio is not None else None),
        "disposable_income": round(disposable_income) if disposable_income is not None else None,
        "loan_exposure": loan_exposure,
        "prior_loan_note": (
            f"Applicant already has Rs.{prior_disbursed_amount} disbursed to them by us "
            f"on a prior loan - this was deducted from both eligibility amounts above."
        ) if prior_disbursed_amount else None,
    }

    # --- Dimension 2: Financial Stability (age lives here as a demographic fact) ---
    financial_stability = {
        "date_of_birth": date_of_birth,
        "age": age,
        "age_source": age_source,  # "cam_direct_age_field" or "computed_from_date_of_birth"
        "age_very_low_band": age is not None and age > AGE_VERY_LOW_CUTOFF,
        "age_low_band": age is not None and AGE_LOW_LOWER <= age <= AGE_LOW_UPPER,
        "experience_years": experience_years,
        "experience_category": experience_category,
        "highest_qualification": cam_fields.get("highest_degree"),
        "financial_assets": assets or None,          # FD / MF / investments - cushion
        "recurring_commitments": commitments or None,  # SIP / insurance / credit card
    }

    # --- Missing data handling ---
    available, missing = [], []
    for name, value in [
        ("monthly_income", monthly_income),
        ("foir_percent", foir_percent),
        ("existing_emi", existing_emi),
        ("experience_years", experience_years),
        ("loans", loans),
        ("age", age),
    ]:
        (available if value else missing).append(name)

    # Absent FD/SIP/insurance means "no such obligation/asset", not missing data
    if not commitments:
        available.append("recurring_commitments (none found - treated as no commitments)")
    else:
        available.append("recurring_commitments")
    if not assets:
        available.append("financial_assets (none found - treated as none held)")
    else:
        available.append("financial_assets")

    return {
        "financial_capacity": financial_capacity,
        "financial_stability": financial_stability,
        "hard_flags": hard_flags,
        "available_data": available,
        "missing_data": missing,
        "analysis_confidence": assess_data_confidence(available, missing),
    }