"""
Reads a CAM Excel file and pulls out the fields the analysis engine needs.

Primary method: search for known labels (e.g. "FOIR (%)") and read the
value sitting beside them. Free, instant, and consistent as long as the
template holds.

Recurring commitments (insurance / SIP / credit card / etc.) and financial
assets (FD / mutual funds / etc.) are found by KEYWORD SCANNING rather than
exact labels, because these headings vary between CAMs and we haven't
catalogued them all. If nothing is found, that means the customer has no
such commitment/asset - not that data is missing.

Fallback: if the expected core labels aren't found at all (template drift),
the CAM is rejected rather than guessed at by AI.
"""
import re
import requests
from openpyxl import load_workbook
from io import BytesIO
from datetime import date, datetime

# NOTE: AI is no longer used for CAM field extraction - the template is
# known and consistent, so extraction stays 100% deterministic. AI is only
# used once per lead, for the final bucket explanation (see main.py).

LABEL_MAP = {
    "TOTAL MONTHLY INCOME": "total_monthly_income_cell",  # manually typed,
                                                            # often blank/wrong for
                                                            # salaried leads - but can
                                                            # be the MOST reliable figure
                                                            # for self-employed leads
                                                            # (see fallback logic below).
    "MONTHLY SALARY": "monthly_salary",
    "ADDITIONAL INCOME": "additional_income",
    "CONSULTANCY": "consultancy_text",
    "FOIR (%)": "foir_percent",
    "EXISTING EMI": "existing_emi",
    "EXPEREINCE": "experience_years",   # matches the mis-spelled label in the template
    "EXPERIENCE": "experience_years",
    "DOCTOR / CA / CS": "profession",
    "CURRENT ADDRESS": "current_address",
    "PERMANENT ADDRESS": "permanent_address",
    "HIGHEST DEGREE": "highest_degree",
    "CIBIL SCORE": "cibil_score",
    "DATE OF BIRTH": "date_of_birth",
    "TOTAL ACTIVE LOAN (COUNT/AMOUNT)": "total_active_loans_raw",
    "CREDIT REMARK": "credit_remark",
}

# FOIR is always needed. For income, the CAM must have EITHER the
# TOTAL MONTHLY INCOME label or the MONTHLY SALARY label.
REQUIRED_LABELS_FOR_SUCCESS = ["FOIR (%)"]
INCOME_LABELS_ANY_OF = ["TOTAL MONTHLY INCOME", "MONTHLY SALARY"]

# INCOME RULE (from the credit team):
#   1. The CAM's own TOTAL MONTHLY INCOME is the figure to use. It is looked
#      for on the left summary AND on the right side of the sheet.
#   2. ONLY if that total is not given anywhere, income is built from
#      Monthly Salary + Additional Income + Consultancy income.
#   3. If neither works, income is "not given" and the lead goes to the
#      Very Low bucket (see financial_analysis.py).
# No value is hard-required, so a lead is never skipped just because income
# is missing. CIBIL is NOT used anywhere.
REQUIRED_VALUES_FOR_SUCCESS = []

# Below this, a computed income total is treated as "parsing clearly
# failed" rather than "genuinely near-zero income" - real incomes on these
# CAMs are never this low. Used to decide when to fall back to the CAM's
# own reported total instead (see extract_cam()).
MIN_PLAUSIBLE_COMPUTED_INCOME = 1000

# If the MONTHLY SALARY cell is written as a YEARLY figure ("20 lakh annually",
# "20 LPA", "24 L per annum"), it is divided by 12 before being used.
_ANNUAL_WORDS_RE = re.compile(
    r"\b(annual|annually|yearly|per\s*year|per\s*annum|p\.?\s?a\.?|lpa)\b",
    re.IGNORECASE,
)

# Keywords that signal a RECURRING monthly commitment (cash going out)
COMMITMENT_KEYWORDS = {
    "insurance": ["insurance", "policy premium", "life cover", "mediclaim", "lic"],
    "sip": ["sip", "systematic investment"],
    "credit_card": ["credit card", "card payment", "card outstanding", "cc payment"],
    "rent": ["rent paid", "monthly rent", "house rent"],
    "other_commitment": ["recurring deposit", "rd amount", "chit"],
}

# Keywords that signal a FINANCIAL ASSET / cushion (money held)
ASSET_KEYWORDS = {
    "fixed_deposit": ["fixed deposit", "fd amount", "fd value", " fd"],
    "mutual_fund": ["mutual fund", "mf holding", "mf value"],
    "shares": ["shares", "equity holding", "demat"],
    "savings": ["savings balance", "bank balance"],
}

# ------------------------------------------------------- address parsing

# Used to pull STATE out of a free-text PERMANENT ADDRESS field. Matched
# case-insensitively, longest name first so "Uttar Pradesh" doesn't
# get shadowed by a shorter false match.
INDIAN_STATES_AND_UTS = sorted([
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir",
    "Ladakh", "Lakshadweep", "Puducherry",
], key=len, reverse=True)

_STATE_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in INDIAN_STATES_AND_UTS) + r")\b",
    re.IGNORECASE,
)
_DIST_RE = re.compile(r"\bdist(?:rict)?\s*[:\.]?\s*([A-Za-z ]+)", re.IGNORECASE)
_PO_RE = re.compile(r"\bpo\s*[:\.]?\s*([A-Za-z ]+)", re.IGNORECASE)


def _parse_city_state(address_text):
    """
    Best-effort, deterministic city/state split of a free-text address like:
    "P34, welkin enclave, mahal yojana, D-block, jagatpura, jaipur,
     po: jagatpura, dist: jaipur rajasthan - 302017"

    STATE: matched directly against the fixed list of Indian states/UTs -
    this part is reliable since there are only 36 possible values.

    CITY: CAMs consistently write "dist: <city>" right before the state,
    so that's the first choice (most reliable - it's a real postal
    district, not just a locality name mentioned somewhere in the string).
    Falls back to the "po: <city>" post-office field, then to the
    comma-separated segment right before the state name, if neither
    label is present.

    Returns {"city": ..., "state": ...} - either can be None if not found.
    Never raises; a CAM with an unusual address format just yields Nones
    rather than a wrong guess.
    """
    if not address_text:
        return {"city": None, "state": None}

    text = str(address_text).strip()

    state_match = _STATE_RE.search(text)
    state = state_match.group(1).title() if state_match else None

    city = None
    dist_match = _DIST_RE.search(text)
    if dist_match:
        # Strip off a trailing state name that's stuck to the same word
        # group, e.g. "jaipur rajasthan" -> "jaipur"
        raw = dist_match.group(1).strip()
        if state:
            raw = re.sub(re.escape(state), "", raw, flags=re.IGNORECASE).strip()
        city = raw.strip(" ,-").title() or None

    if not city:
        po_match = _PO_RE.search(text)
        if po_match:
            city = po_match.group(1).strip(" ,-").title() or None

    if not city and state_match:
        # Fall back to the comma-separated segment immediately before the
        # state name in the raw text.
        before_state = text[:state_match.start()]
        parts = [p.strip() for p in before_state.split(",") if p.strip()]
        if parts:
            candidate = re.sub(r"\b(po|dist(?:rict)?)\s*[:\.]?\s*", "", parts[-1], flags=re.IGNORECASE).strip()
            city = candidate.title() or None

    return {"city": city, "state": state}


# ------------------------------------------------------- currency parsing

# Shared unit table - used by BOTH _parse_currency (plain numeric cells)
# and _parse_free_text_income (descriptive sentences), so a figure reads
# the same rupee value no matter which field it was typed into.
_UNIT_MULTIPLIERS = {
    "crore": 10000000, "cr": 10000000,
    "lakh": 100000, "lac": 100000, "l": 100000,
    "k": 1000,
}


def _unit_multiplier(unit_token):
    if not unit_token:
        return None
    unit = unit_token.lower().rstrip("s")
    return _UNIT_MULTIPLIERS.get(unit)


_CURRENCY_UNIT_RE = re.compile(
    r'(\d[\d,]*\.?\d*|\.\d+)\s*(crores?|cr|lakhs?|lacs?|l|k)?(?![a-z])',
    re.IGNORECASE,
)


def _parse_currency(value):
    """
    Turn a currency-ish value into a plain float in rupees. Handles:
      - Plain numbers already stored as int/float in the sheet
      - Formatted text like '₹ 4,000,000.00'
      - Manually typed shorthand, BOTH full words and abbreviations:
        '1.52 Lakhs' / '1.52 L' / '1.52l', '2.5 Cr' / '2.5 crore', '50k'
        - these MUST be expanded by their unit, or e.g. "1.5 L" silently
        became Rs.1.5 instead of Rs.1,50,000 (the bug this fixes - it
        previously only recognized the full words "crore"/"lakh"/"lac",
        not the abbreviations "l"/"cr" that people actually type).
    None if not parseable.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    match = _CURRENCY_UNIT_RE.search(text)
    if not match:
        return None

    number = float(match.group(1).replace(",", ""))
    unit_token = match.group(2)
    multiplier = _unit_multiplier(unit_token)

    # Keep the old safety rail: only treat a trailing "k" as x1000 when the
    # base number is small - "5000k" is far more likely a stray letter
    # than genuine "50 lakh" shorthand.
    if multiplier and unit_token and unit_token.lower().rstrip("s") == "k" and number >= 1000:
        multiplier = None

    if multiplier:
        number *= multiplier

    return number


# ---------------------------------------------- free-text income parsing

_RANGE_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|l|k)?(?![a-z])',
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|l|k)?(?![a-z])',
    re.IGNORECASE,
)


def _parse_add_part(part: str):
    """
    Returns the rupee value of ONE amount phrase (already split apart from
    any comma/pipe alternates and and/+/& sums). A hyphenated range takes
    its LOWER bound, with the unit correctly attached to both ends of the
    range (e.g. "1-1.5 lakhs" reads as Rs.1,00,000, not just "1" with no
    unit at all).
    """
    range_match = _RANGE_RE.search(part)
    if range_match:
        low, high, unit_token = range_match.groups()
        multiplier = _unit_multiplier(unit_token)
        if multiplier:
            return float(low) * multiplier
        return None  # a bare numeric range with no unit isn't a real amount here

    single_match = _SINGLE_RE.search(part)
    if not single_match:
        return None
    number, unit_token = single_match.groups()
    multiplier = _unit_multiplier(unit_token)
    if multiplier:
        return float(number) * multiplier
    number = float(number)
    return number if number >= 1000 else None  # bare small number = noise (%, count, etc.)


def _parse_free_text_income(text) -> float:
    """
    Parses income written as a descriptive sentence rather than a clean
    number, e.g.:
      "YES, 1-1.5 LAKHS FROM CONSULTANCY"
      "1 LAKH AND 2 LAKH"        -> summed:   Rs.3,00,000
      "2 L"                      -> Rs.2,00,000
      "3-4 LAKHS FROM JAYDEEP, 10%"
      "3 LAKHS FROM OWN PRACTICE | 1.5 LAKHS IN A/C"
      "NO"  /  blank

    Used for BOTH consultancy_text and additional_income - either field can
    legitimately be written this way.

    Two joiners are treated differently, because they mean different things
    in these sheets:
      - "AND" / "+" / "&" joins SEPARATE amounts that together make up one
        total (e.g. "1 lakh and 2 lakh" = Rs.3,00,000) - these are SUMMED.
      - "," / "|" separates ALTERNATE or uncertain readings of the same
        figure (e.g. a range, or the same amount phrased two ways) - the
        LOWER of these is taken, staying conservative since this kind of
        income is inherently less certain than fixed salary.
    """
    if not text:
        return 0
    text = str(text).strip().lower()
    if text in ("no", "0", "nil", "-", "na", "n/a"):
        return 0

    alt_segments = re.split(r"[|,]", text)

    segment_totals = []
    for segment in alt_segments:
        add_parts = re.split(r"\band\b|\+|&", segment)
        values = [v for v in (_parse_add_part(p) for p in add_parts) if v]
        if values:
            segment_totals.append(sum(values))

    return min(segment_totals) if segment_totals else 0


def _find_total_income_candidates(ws):
    """
    Finds EVERY "TOTAL MONTHLY INCOME" label on the whole sheet - the left
    summary section and the right side - and returns the amounts written
    beside them as [(side, amount), ...], left side first.

    The value is the first filled cell up to 3 cells to the right of the
    label (covers merged cells), or a number typed in the same cell as the
    label ("TOTAL MONTHLY INCOME: 235000").
    """
    found = []
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None or isinstance(cell.value, (int, float)):
                continue
            text = str(cell.value).strip()
            if not text or len(text) > 40:
                continue
            normalized = re.sub(r"[^A-Z]", "", text.upper())
            if not normalized.startswith("TOTALMONTHLYINCOME"):
                continue

            amount = None
            digits_in_label = re.search(r"\d.*", text)
            if digits_in_label:
                amount = _parse_currency(digits_in_label.group(0))
            if amount is None:
                for offset in (1, 2, 3):
                    candidate = ws.cell(row=cell.row, column=cell.column + offset).value
                    if candidate in (None, ""):
                        continue
                    amount = _parse_currency(candidate)
                    break

            side = "left" if cell.column <= 4 else "right"
            found.append((side, amount))

    found.sort(key=lambda item: 0 if item[0] == "left" else 1)
    return found


def _find_direct_age(ws):
    """
    Looks for a "CUSTOMER'S AGE" cell and returns the value beside it.
    Normalizes away spaces/apostrophes/case before comparing, since CAMs
    vary in how the apostrophe is typed (straight quote, curly quote, or
    dropped entirely) - LABEL_MAP's exact-string matching would otherwise
    miss some of those variants.
    """
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            normalized = re.sub(r"[^A-Z]", "", str(cell.value).strip().upper())
            if normalized == "CUSTOMERSAGE":
                return ws.cell(row=cell.row, column=cell.column + 1).value
    return None


def _download_excel(url: str) -> BytesIO:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return BytesIO(resp.content)


def _extract_via_labels(ws):
    """
    Find known labels anywhere in the sheet and grab the adjacent value.

    Some labels (e.g. "CONSULTANCY") can legitimately appear more than once
    on a CAM for unrelated fields. If a later occurrence is blank, it must
    NOT silently wipe out a real value already found from an earlier
    occurrence - so a blank/None never overwrites an existing value.
    """
    found, found_labels = {}, set()
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            label = str(cell.value).strip()
            if label in LABEL_MAP:
                field = LABEL_MAP[label]
                value_cell = ws.cell(row=cell.row, column=cell.column + 1)
                new_value = value_cell.value

                already_has_value = field in found and found[field] not in (None, "", 0)
                new_is_blank = new_value in (None, "", 0)
                if already_has_value and new_is_blank:
                    # Keep the earlier real value - this later occurrence
                    # of the same label is a different, blank field.
                    found_labels.add(label)
                    continue

                found[field] = new_value
                found_labels.add(label)
    return found, found_labels


def _scan_for_keyword_values(ws, keyword_map, max_column=4):
    """
    Walk the left-hand summary section looking for commitment/asset keywords.
    When one matches, read the number sitting next to it (or one cell
    further, if the first is blank). Returns {category: amount} for whatever
    was actually found; an empty dict means the customer genuinely has no
    such commitment/asset, NOT that data is missing.

    Deliberately keyword-based rather than exact-label based, since these
    headings differ between CAM files.

    max_column defaults to 4 (columns A-D) so this never strays into the
    tradelines loan table on the right - otherwise a "Credit Card" LOAN row
    would be wrongly read as a monthly commitment.
    """
    results = {}
    for row in ws.iter_rows(max_col=max_column):
        for cell in row:
            if cell.value is None or isinstance(cell.value, (int, float)):
                continue
            text = str(cell.value).strip().lower()
            if not text or len(text) > 60:
                continue

            for category, keywords in keyword_map.items():
                if category in results:
                    continue
                if any(kw in text for kw in keywords):
                    for offset in (1, 2):
                        if cell.column + offset > max_column + 1:
                            break
                        candidate = ws.cell(row=cell.row, column=cell.column + offset).value
                        amount = _parse_currency(candidate)
                        if amount:
                            results[category] = amount
                            break
    return results


def _find_tradelines_header(ws):
    """Locates the header row of the tradelines table (shared by
    _extract_tradelines and _extract_tradeline_summary_rows so both read
    from the same row without searching twice)."""
    for row in ws.iter_rows(max_row=5):
        for cell in row:
            if cell.value == "TRADELINES":
                return cell.row
    return None


def _parse_date_cell(value):
    """
    Tradeline START/Ending Date cells are usually real Excel dates (openpyxl
    hands those back as datetime/date objects already), but occasionally
    get typed as plain text. Handles both; returns a date or None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_tradelines(ws, header_row) -> list:
    """Reads the loan-by-loan tradelines table on the right side of the sheet.

    Each row also has an Ending Date (tenure end). If that date has already
    passed, the loan is treated as CLOSED: its EMI is counted as Rs.0 for
    every downstream calculation (it's no longer a real monthly outgoing),
    while the original reported EMI is kept alongside it for reference/audit.
    Sanction amount and current balance are left as-is - only the EMI
    contribution changes.
    """
    if not header_row:
        return []

    today = date.today()
    loans = []
    row_num = header_row + 1
    while True:
        ownership = ws.cell(row=row_num, column=5).value
        loan_type = ws.cell(row=row_num, column=6).value
        lender = ws.cell(row=row_num, column=7).value

        if ownership in (None, "TOTAL", "Secured", "Unsecured") or loan_type is None:
            break

        reported_emi = _parse_currency(ws.cell(row=row_num, column=10).value)
        end_date = _parse_date_cell(ws.cell(row=row_num, column=13).value)
        is_completed = end_date is not None and end_date < today
        effective_emi = 0.0 if is_completed else reported_emi

        loans.append({
            "ownership": ownership,
            "loan_type": loan_type,
            "lender": lender,
            "sanction_amount": _parse_currency(ws.cell(row=row_num, column=8).value),
            "current_balance": _parse_currency(ws.cell(row=row_num, column=9).value),
            "emi_amount": effective_emi,                # used for all downstream sums
            "emi_amount_reported": reported_emi,         # original CAM figure, for audit
            "start_date": _parse_date_cell(ws.cell(row=row_num, column=11).value),
            "tenure_months": ws.cell(row=row_num, column=12).value,
            "end_date": end_date,
            "loan_completed": is_completed,
        })
        row_num += 1
    return loans


TRADELINE_SUMMARY_ROW_LABELS = ("TOTAL", "Secured", "Unsecured")


def _extract_tradeline_summary_rows(ws, header_row) -> dict:
    """
    Reads the TOTAL / Secured / Unsecured rows below the tradelines table -
    the CAM's OWN pre-computed sanction/balance/EMI totals for that lead.

    This is the THIRD independent EMI source (alongside the top EXISTING
    EMI cell and our own sum of the individual tradeline rows) used in the
    3-way EMI reconciliation in extract_cam(). These rows are usually a
    few blank rows below the last loan, not immediately adjacent to it, so
    this scans the rest of the sheet below the header rather than assuming
    adjacency to where _extract_tradelines() stopped.

    Returns e.g. {"total": {...}, "secured": {...}, "unsecured": {...}} -
    only whichever of the three rows were actually found.
    """
    if not header_row:
        return {}

    summary = {}
    for row in ws.iter_rows(min_row=header_row + 1):
        for cell in row:
            if cell.column != 5:  # OWNERSHIP column - where these row labels live
                continue
            if cell.value in TRADELINE_SUMMARY_ROW_LABELS:
                key = str(cell.value).strip().lower()
                summary[key] = {
                    "sanction_amount": _parse_currency(ws.cell(row=cell.row, column=8).value),
                    "current_balance": _parse_currency(ws.cell(row=cell.row, column=9).value),
                    "emi_amount": _parse_currency(ws.cell(row=cell.row, column=10).value),
                }
    return summary


EMI_MATCH_TOLERANCE = 500  # rupees - two EMI figures this close are "the same"


def _check_typed_emi_for_finished_loans(loans, typed_emi_cell, cam_total_row_emi):
    """
    Checks whether a typed total EMI (the EXISTING EMI cell, or the CAM's
    own TOTAL row) still includes EMIs of loans that are already finished.

    Returns a small dict:
      status  : "no_finished_loans" | "typed_includes_finished" |
                "typed_excludes_finished" | "cannot_tell"
      typed_total_includes_finished_loans : True / False
      sum_active_loans, sum_all_loans, finished_loans_count,
      finished_loans_emi, typed_total (the number that was checked), note
    """
    loans = loans or []
    finished = [l for l in loans if l.get("loan_completed")]
    active_sum = sum((l.get("emi_amount") or 0) for l in loans)
    all_sum = sum((l.get("emi_amount_reported") or 0) for l in loans)
    finished_emi = all_sum - active_sum

    result = {
        "status": "no_finished_loans",
        "typed_total_includes_finished_loans": False,
        "typed_total": None,
        "typed_total_source": None,
        "sum_active_loans": active_sum if loans else None,
        "sum_all_loans": all_sum if loans else None,
        "finished_loans_count": len(finished),
        "finished_loans_emi": finished_emi,
        "note": None,
    }

    if not loans or not finished or finished_emi <= 0:
        return result  # nothing finished (or finished loans had no EMI) - nothing to check

    candidates = [
        ("EXISTING EMI cell", typed_emi_cell),
        ("CAM TOTAL row", cam_total_row_emi),
    ]
    candidates = [(name, value) for name, value in candidates if value is not None]
    if not candidates:
        result["status"] = "cannot_tell"
        result["note"] = "No typed total EMI found to check against the finished loans."
        return result

    for name, typed in candidates:
        matches_all = abs(typed - all_sum) <= EMI_MATCH_TOLERANCE
        matches_active = abs(typed - active_sum) <= EMI_MATCH_TOLERANCE
        if matches_all and not matches_active:
            result.update({
                "status": "typed_includes_finished",
                "typed_total_includes_finished_loans": True,
                "typed_total": typed,
                "typed_total_source": name,
                "note": (
                    f"The {name} (Rs.{typed:,.0f}) still includes {len(finished)} finished "
                    f"loan(s) worth Rs.{finished_emi:,.0f} of EMI. Finished loans were "
                    f"removed - EMI used is Rs.{active_sum:,.0f}."
                ),
            })
            return result

    # No typed total includes the finished loans. Report on the first one.
    name, typed = candidates[0]
    matches_active = abs(typed - active_sum) <= EMI_MATCH_TOLERANCE
    result.update({
        "status": "typed_excludes_finished" if matches_active else "cannot_tell",
        "typed_total": typed,
        "typed_total_source": name,
        "note": (
            f"The {name} (Rs.{typed:,.0f}) already leaves out the finished loans."
            if matches_active else
            f"The {name} (Rs.{typed:,.0f}) matches neither the active-loans sum "
            f"(Rs.{active_sum:,.0f}) nor the all-loans sum (Rs.{all_sum:,.0f}). "
            f"EMI used is the active-loans sum."
        ),
    })
    return result


# ------------------------------------------------- profile fields (filters)

def _find_labelled_value(ws, label_prefixes, max_col=4):
    """
    Finds a label in the LEFT summary part of the sheet (columns A-D) and
    returns the value written beside it. Labels are compared after removing
    spaces, punctuation and case, and only the START of the label has to match,
    so "HOUSE TYPE: (OWNED/RENTED/HOSTEL)" is found by "HOUSETYPE".
    Returns None when the label is missing or its value cell is empty.
    """
    for row in ws.iter_rows(max_col=max_col):
        for cell in row:
            if cell.value is None or isinstance(cell.value, (int, float)):
                continue
            text = str(cell.value).strip()
            if not text or len(text) > 80:
                continue
            normalized = re.sub(r"[^A-Z0-9]", "", text.upper())
            if not any(normalized.startswith(p) for p in label_prefixes):
                continue
            for offset in (1, 2):
                if cell.column + offset > max_col + 1:
                    break
                value = ws.cell(row=cell.row, column=cell.column + offset).value
                if value not in (None, ""):
                    return value
    return None


def _clean_choice(value):
    return " ".join(str(value or "").replace("_", " ").split())


def normalize_marital_status(value):
    """-> 'Married' | 'Unmarried' | 'Divorced' | 'Widowed' | None"""
    text = _clean_choice(value).lower()
    if not text:
        return None
    if re.search(r"un\s*-?married|single|bachelor|never", text):
        return "Unmarried"
    if re.search(r"divorc|separated", text):
        return "Divorced"
    if re.search(r"widow", text):
        return "Widowed"
    if re.search(r"married|spouse|wife|husband", text):
        return "Married"
    return _clean_choice(value).title()


def normalize_house_type(value):
    """-> 'Owned' | 'Rented' | 'Hostel' | 'Other' | None"""
    text = _clean_choice(value).lower()
    if not text:
        return None
    if re.search(r"hostel|\bpg\b|paying guest", text):
        return "Hostel"
    if re.search(r"\brent(ed|al)?\b|\blease(d)?\b", text):
        return "Rented"
    if re.search(r"\bown(ed)?\b|self[\s-]?owned", text):
        return "Owned"
    return "Other"


def normalize_employment_type(value):
    """-> 'Salaried' | 'Self-employed' | 'Other' | None"""
    text = _clean_choice(value).lower()
    if not text:
        return None
    if re.search(r"self|business|professional|proprietor|consult|practi|own\s+(business|practice)", text):
        return "Self-employed"
    if re.search(r"salar|employee|service|job|pvt|private|govt|government", text):
        return "Salaried"
    return "Other"


def _count_from_cell(value):
    if value in (None, ""):
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _read_profile_fields(ws, fields, monthly_salary, consultancy_income):
    """
    Marital status, house type, employment type (+ where it came from),
    registration year and enquiry counts. Every one is optional: a missing or
    empty cell just gives None and NEVER stops the CAM from being read.
    """
    out = {"marital_status": None, "house_type": None, "employment_type": None,
           "employment_source": None, "registration_year": None,
           "enquiries_3m": None, "enquiries_6m": None}
    try:
        out["marital_status"] = normalize_marital_status(
            _find_labelled_value(ws, ("MARITALSTATUS",)))
        out["house_type"] = normalize_house_type(
            _find_labelled_value(ws, ("HOUSETYPE", "TYPEOFHOUSE", "RESIDENCETYPE",
                                      "TYPEOFRESIDENCE", "RESIDENCESTATUS")))
        employment = normalize_employment_type(
            _find_labelled_value(ws, ("EMPLOYMENTTYPE", "TYPEOFEMPLOYMENT",
                                      "NATUREOFEMPLOYMENT", "EMPLOYMENTSTATUS")))
        if employment:
            out["employment_type"], out["employment_source"] = employment, "cam"
        elif monthly_salary and monthly_salary > 0:
            # the cell was left empty - a salary figure means a salaried person
            out["employment_type"], out["employment_source"] = "Salaried", "derived"
        elif (consultancy_income and consultancy_income > 0) or fields.get("profession"):
            # no salary, but consultancy income or a Doctor / CA / CS profession
            out["employment_type"], out["employment_source"] = "Self-employed", "derived"

        reg = _find_labelled_value(ws, ("REGISTRATIONYEAR",))
        out["registration_year"] = _clean_choice(reg) or None
        out["enquiries_3m"] = _count_from_cell(
            _find_labelled_value(ws, ("TOTALENQUIRYINLAST3MONTH",)))
        out["enquiries_6m"] = _count_from_cell(
            _find_labelled_value(ws, ("TOTALENQUIRYINLAST6MONTH",)))
    except Exception:
        pass
    return out



def extract_cam(url: str) -> dict:
    """
    Downloads the CAM Excel and returns a clean dict of fields.
    Raises ValueError if the file doesn't look like a CAM at all, or if it's
    a blank/unfilled template (labels present, but no real values).
    """
    wb = load_workbook(_download_excel(url), data_only=True)
    ws = wb.active

    fields, found_labels = _extract_via_labels(ws)

    if not found_labels:
        raise ValueError("This file does not look like a CAM (no expected labels found)")

    # Parse the three income components. Salary is a plain number (unit-
    # aware, in case someone typed "2.4 lakhs" there too). Additional
    # income and consultancy are free text and go through the sentence
    # parser, since either can be written descriptively.
    raw_salary = fields.get("monthly_salary")
    monthly_salary = _parse_currency(raw_salary)
    salary_was_yearly = False
    if monthly_salary and isinstance(raw_salary, str) and _ANNUAL_WORDS_RE.search(raw_salary):
        monthly_salary = monthly_salary / 12  # written as a yearly figure
        salary_was_yearly = True
    additional_income = _parse_free_text_income(fields.get("additional_income"))
    consultancy_income = _parse_free_text_income(fields.get("consultancy_text"))

    fields["monthly_salary"] = monthly_salary
    fields["additional_income"] = additional_income
    fields["consultancy_income"] = consultancy_income

    missing_required = [l for l in REQUIRED_LABELS_FOR_SUCCESS if l not in found_labels]
    if not any(l in found_labels for l in INCOME_LABELS_ANY_OF):
        missing_required.append("TOTAL MONTHLY INCOME or MONTHLY SALARY")
    empty_required_values = [
        v for v in REQUIRED_VALUES_FOR_SUCCESS
        if fields.get(v) in (None, 0, "0")
    ]

    if missing_required or empty_required_values:
        problem = missing_required or empty_required_values
        raise ValueError(
            f"CAM is missing required data: {', '.join(problem)} "
            f"(labels found: {sorted(found_labels)})"
        )

    # City/State - parsed from the free-text PERMANENT ADDRESS field, since
    # the CAM doesn't have separate City/State cells.
    city_state = _parse_city_state(fields.get("permanent_address"))
    fields["city"] = city_state["city"]
    fields["state"] = city_state["state"]

    header_row = _find_tradelines_header(ws)
    fields["loans"] = _extract_tradelines(ws, header_row)
    fields["_extraction_method"] = "fixed_cell"

    # Source #2 of the 3-way EMI reconciliation, and now also the PRIMARY
    # EMI figure used downstream (see summarize_running_loans in
    # financial_analysis.py). Deliberately None only when there are NO
    # loan rows at all - not when the loans present just happen to sum to
    # Rs.0 (e.g. every loan is already completed). Collapsing a genuine
    # zero into None here would wrongly make the code fall back to the
    # CAM's manually-typed EXISTING EMI cell instead of trusting a real,
    # verified zero.
    loans_list = fields.get("loans", [])
    fields["tradeline_emi_total"] = (
        sum((loan.get("emi_amount") or 0) for loan in loans_list)
        if loans_list else None
    )

    # Source #3: the CAM's OWN pre-computed TOTAL/Secured/Unsecured rows,
    # read directly rather than recomputed.
    fields["cam_reported_tradeline_totals"] = _extract_tradeline_summary_rows(ws, header_row) or None
    fields["_emi_check_note"] = None

    # Commitments & assets - keyword scan; empty means "none", not "missing"
    fields["recurring_commitments"] = _scan_for_keyword_values(ws, COMMITMENT_KEYWORDS)
    fields["financial_assets"] = _scan_for_keyword_values(ws, ASSET_KEYWORDS)

    # Normalize remaining numeric fields
    fields["existing_emi"] = _parse_currency(fields.get("existing_emi"))

    # --- 3-way EMI reconciliation ---
    # 1. sum_of_tradelines  : our own sum of the individual tradeline rows
    #                         (completed loans zeroed) - now the PRIMARY
    #                         source of truth used downstream, see
    #                         summarize_running_loans() in financial_analysis.py
    # 2. existing_emi_cell  : the top summary section's manually-typed
    #                         EXISTING EMI cell - only used as a fallback
    #                         when there are no tradeline rows to sum
    # 3. cam_total_row      : the CAM's own pre-computed TOTAL row EMI,
    #                         read directly rather than recomputed
    # Any two disagreeing by more than Rs.500 is flagged - informational
    # only for now, exactly like the income check below.
    cam_totals = fields.get("cam_reported_tradeline_totals") or {}
    cam_total_row_emi = cam_totals.get("total", {}).get("emi_amount")

    emi_sources = {
        "sum_of_tradelines": fields["tradeline_emi_total"],
        "existing_emi_cell": fields.get("existing_emi"),
        "cam_total_row": cam_total_row_emi,
    }
    present_sources = {k: v for k, v in emi_sources.items() if v is not None}
    if len(present_sources) >= 2 and (max(present_sources.values()) - min(present_sources.values())) > 500:
        detail = ", ".join(f"{k}=Rs.{v}" for k, v in present_sources.items())
        fields["_emi_check_note"] = (
            f"EMI sources disagree ({detail}) - using the sum of loan rows "
            f"(completed loans excluded) as the EMI figure."
        )

    # --- Finished-loan check on the TYPED total EMI ---
    # The typed "EXISTING EMI" cell (and the CAM's TOTAL row) is written by
    # a person. Sometimes it still includes loans that are already finished.
    # Compare it with two sums built from the loan rows:
    #   - active_only : finished loans counted as Rs.0  (what we really owe)
    #   - all_loans   : every loan's originally reported EMI (finished too)
    # If the typed total matches all_loans but NOT active_only, it still
    # contains finished loans. Either way the EMI used downstream is the
    # active_only sum, so the finished loans never reduce eligibility.
    fields["emi_check"] = _check_typed_emi_for_finished_loans(
        loans_list,
        typed_emi_cell=fields.get("existing_emi"),
        cam_total_row_emi=cam_total_row_emi,
    )
    if fields["emi_check"].get("typed_total_includes_finished_loans"):
        fields["_emi_check_note"] = fields["emi_check"]["note"]

    # --- Total Monthly Income ---
    # 1st choice: the CAM's own TOTAL MONTHLY INCOME (left side, then right).
    # 2nd choice (only if that is not given): salary + additional + consultancy.
    # Otherwise: income not given (None) -> Very Low bucket downstream.
    computed_income = (monthly_salary or 0) + (additional_income or 0) + (consultancy_income or 0)

    totals = []
    for side, amount in _find_total_income_candidates(ws):
        if amount and amount >= MIN_PLAUSIBLE_COMPUTED_INCOME:
            totals.append((side, amount))
    if not totals:
        # the label-scan value (exact label) as a last look
        cell_total = _parse_currency(fields.get("total_monthly_income_cell"))
        if cell_total and cell_total >= MIN_PLAUSIBLE_COMPUTED_INCOME:
            totals.append(("left", cell_total))

    fields["_income_check_note"] = None
    if totals:
        side, reported_total = totals[0]
        fields["total_monthly_income"] = reported_total
        fields["_income_source"] = f"TOTAL MONTHLY INCOME cell ({side} side)"
        others = [a for _, a in totals[1:] if abs(a - reported_total) > 1000]
        if others:
            fields["_income_check_note"] = (
                f"TOTAL MONTHLY INCOME appears more than once with different values "
                f"({reported_total} and {others[0]}) - used the {side} side one."
            )
    elif computed_income >= MIN_PLAUSIBLE_COMPUTED_INCOME:
        fields["total_monthly_income"] = computed_income
        fields["_income_source"] = "salary + additional income + consultancy (no total given)"
        if salary_was_yearly:
            fields["_income_check_note"] = (
                "Monthly salary was written as a yearly amount - divided by 12."
            )
    else:
        fields["total_monthly_income"] = None
        fields["_income_source"] = None
        fields["_income_check_note"] = "Income not given in the CAM."

    foir = fields.get("foir_percent")
    if isinstance(foir, (int, float)) and foir <= 1:
        foir = foir * 100  # template stores 0.7 - normalize to 70
    fields["foir_percent"] = foir

    if fields.get("experience_years"):
        match = re.search(r"\d+", str(fields["experience_years"]))
        fields["experience_years"] = int(match.group()) if match else None

    # CIBIL score - extracted for display only. NOT used anywhere in the
    # bucket/eligibility calculation (see the module docstring and
    # financial_analysis.py - CIBIL is deliberately excluded from scoring).
    if fields.get("cibil_score") not in (None, ""):
        match = re.search(r"\d+", str(fields["cibil_score"]))
        fields["cibil_score"] = int(match.group()) if match else None
    else:
        fields["cibil_score"] = None

    # Direct "CUSTOMER'S AGE" cell, if the CAM has one - preferred over
    # computing age from date of birth (see financial_analysis.py, which
    # falls back to the DOB calculation only when this is absent).
    raw_age = _find_direct_age(ws)
    if raw_age is not None:
        match = re.search(r"\d+", str(raw_age))
        fields["age_direct"] = int(match.group()) if match else None
    else:
        fields["age_direct"] = None

    # Marital status, house type, employment type, registration year and
    # enquiry counts - used by the dashboard filters (never needed to read a CAM).
    fields.update(_read_profile_fields(ws, fields, monthly_salary, consultancy_income))

    return fields