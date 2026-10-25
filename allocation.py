"""
allocation.py - works out the buckets from the leads stored in `lead_data`.

No CAM is downloaded and no OMS call is made here, so it is fast and can be
run again every time the bucket logic (financial_analysis.py) is changed, or
a different set of filters is chosen.

    allocate({"month": "2026-09", "statuses": ["Approved"], "states": ["Delhi"]})

The same call also takes the person / loan filters (see lead_data.build_where):
    allocate({"min_income": 200000, "employment_types": ["Salaried"],
              "min_cibil": 700, "max_active_loans": 3,
              "loan_matches": [{"bank": "HDFC Bank", "loan_type": "Personal Loan"}]})

The buckets are worked out FIRST from every lead that passes the filters, so
the counts always match the leads you see.
"""
from db import lead_data
from main import build_financial_profile
from remark_slots import classify_remarks

BUCKETS = ["Excellent", "Good", "Medium", "Low", "Very Low"]


def _dedupe_by_phone(rows):
    """One row per phone number (last 10 digits), newest application first."""
    seen, out = set(), []
    for r in rows:                      # rows are already newest first
        key = r.get("phone_key") or f"no-phone-{r['id']}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _num(value):
    return float(value) if value is not None else None


def allocate(filters: dict = None):
    rows = lead_data.query_leads({**(filters or {}), "only_with_cam": True})
    matched = len(rows)
    rows = _dedupe_by_phone(rows)

    counts = {
        # secured also has an "Insufficient data" group (income or FOIR missing)
        "secured": {**{b: 0 for b in BUCKETS}, "Insufficient data": 0},
        "unsecured": {b: 0 for b in BUCKETS},
    }
    leads, errors = [], 0

    for r in rows:
        try:
            profile = build_financial_profile(
                r["cam_fields"], prior_disbursed_amount=float(r["prior_disbursed_amount"] or 0))
            cap = profile["financial_capacity"]
        except Exception:
            errors += 1
            continue

        secured, unsecured = cap["secured_bucket"], cap["unsecured_bucket"]

        # Priority slot (P1-P4) from the MIS credit + ops remarks. A problem in
        # the remark logic must never stop the buckets, so it is caught here.
        try:
            slot = classify_remarks(r.get("mis_credit_remark"), r.get("mis_ops_remark"))
        except Exception:
            slot = {"slot": None, "reasons": []}
        counts["secured"][secured] = counts["secured"].get(secured, 0) + 1
        counts["unsecured"][unsecured] = counts["unsecured"].get(unsecured, 0) + 1

        leads.append({
            "id": r["id"],
            "application_id": r["application_id"],
            "ticket_id": r["ticket_id"],
            "customer_name": r["customer_name"],
            "customer_phone": r["customer_phone"],
            "cam_url": r.get("cam_url"),
            "ticket_status": r["ticket_status"],
            "loan_type": r["loan_type"],
            "loan_category": r["loan_category"],
            "amount": _num(r["amount"]),
            "application_date": str(r["application_date"])[:10] if r["application_date"] else None,
            "state": r["state"],
            "city": r["city"],
            "designation": r["designation"],
            "experience_years": r["experience_years"],
            "age": r["age"],
            "monthly_income": _num(r.get("monthly_income")),
            "cibil_score": r.get("cibil_score"),
            "employment_type": r.get("employment_type"),
            "employment_source": r.get("employment_source"),
            "marital_status": r.get("marital_status"),
            "house_type": r.get("house_type"),
            "ongoing_loans": r["ongoing_loans"],
            # running TODAY (worked out when you filter, so they never go stale)
            "active_loans": r.get("active_loans"),
            "active_secured_loans": r.get("active_secured_loans"),
            "active_unsecured_loans": r.get("active_unsecured_loans"),
            "credit_remark": r["credit_remark"],
            "mis_credit_remark": r.get("mis_credit_remark"),
            "mis_ops_remark": r.get("mis_ops_remark"),
            # unsecured priority slot, from the remarks (see remark_slots.py)
            "unsecured_slot": slot["slot"],
            "slot_reasons": slot["reasons"],
            "slot_reason_text": "; ".join(slot["reasons"]),
            "secured_bucket": secured,
            "unsecured_bucket": unsecured,
            "secured_eligibility": cap.get("secured_cap_eligibility"),
            "unsecured_eligibility": cap.get("unsecured_cap_eligibility"),
            "secured_cibil_cap": cap.get("secured_cibil_cap"),
            # how the secured bucket was reached: capacity -> quality -> caps
            "secured_capacity_bucket": cap.get("secured_capacity_bucket"),
            "secured_bucket_after_quality": cap.get("secured_bucket_after_quality"),
            "secured_tenure_years": (cap.get("secured_method") or {}).get("tenure_years_used"),
            "secured_quality": cap.get("secured_quality"),
            "secured_missing_inputs": cap.get("secured_missing_inputs"),
            "monthly_surplus": cap.get("monthly_surplus"),
            "secured_cap_reasons": cap.get("secured_bucket_cap_reasons"),
            "unsecured_cap_reasons": cap.get("unsecured_bucket_cap_reasons"),
        })

    return {
        "filters": filters or {},
        "matched_rows": matched,          # before one-per-phone de-duplication
        "total_leads": len(leads),        # after de-duplication
        "errors": errors,
        "counts": counts,
        "leads": leads,
    }