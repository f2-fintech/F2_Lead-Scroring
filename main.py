"""
main.py - runs the scoring.

For every lead it:
  1. finds the CAM (.xlsx) attached to the lead's ticket in OMS,
  2. reads the CAM (cam_extractor.extract_cam),
  3. takes off any amount we already disbursed to the same customer,
  4. works out the secured / unsecured buckets (financial_analysis),
  5. saves the result in OUR results database (never in OMS).

Two entry points are used by server.py:
    run_daily(fresh=False)                 - the 5-minute background sync
    run_for_month("2026-09", fresh=False)  - the "Run Monthly Scoring" button

You can also run it by hand:
    python main.py                    -> daily pass
    python main.py --month 2026-09    -> score one month
    python main.py --month 2026-09 --fresh
    python main.py --fresh            -> wipe ALL saved scores, then score again

NOTE: this file was rebuilt from how the other files use it. If you find
your original main.py, compare the two.
"""
import argparse
import importlib
from datetime import date, timedelta

from db import mysql_client

# How far back the daily/background pass looks for new or changed leads.
# Older leads are handled by the monthly run.
DAILY_LOOKBACK_DAYS = 7


def _import_first(attr_name, module_paths):
    """
    Imports `attr_name` from the first module path that exists. Keeps this
    file working whichever folder cam_extractor / financial_analysis live in.
    """
    errors = []
    for path in module_paths:
        try:
            return getattr(importlib.import_module(path), attr_name)
        except (ImportError, AttributeError) as e:
            errors.append(f"{path}: {e}")
    raise ImportError(
        f"Could not find '{attr_name}'. Tried:\n  " + "\n  ".join(errors)
        + "\nTell me which folder the file is in and I will fix the import line."
    )


extract_cam = _import_first(
    "extract_cam",
    ["extraction.cam_extractor", "cam_extractor", "analysis.cam_extractor", "db.cam_extractor"],
)
build_financial_profile = _import_first(
    "build_financial_profile",
    ["analysis.financial_analysis", "financial_analysis",
     "extraction.financial_analysis", "scoring.financial_analysis", "db.financial_analysis"],
)


# ---------------------------------------------------------------- helpers

def month_range(month_str):
    """'2026-09' -> (2026-09-01, 2026-10-01)"""
    year, month = map(int, month_str.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def score_lead(lead):
    """
    Scores ONE lead and saves it. Returns a short status word:
      "scored"   - saved
      "no_cam"   - the ticket has no .xlsx attachment yet (tried again next pass)
      "cam_error"- every CAM file on the ticket failed to read
    """
    ticket_id = lead["ticket_id"]

    cam_urls = mysql_client.find_cam_attachment(ticket_id)  # newest first
    if not cam_urls:
        return "no_cam"

    cam_fields, cam_url, last_error = None, None, None
    for url in cam_urls:
        try:
            cam_fields = extract_cam(url)
            cam_url = url
            break
        except Exception as e:  # bad/blank/changed CAM - try the next file
            last_error = e
    if cam_fields is None:
        print(f"  ticket {ticket_id}: could not read CAM ({last_error})")
        return "cam_error"

    # Loans we already disbursed to this same customer (other tickets) are
    # taken off the eligibility, and shown in the UI as "after disbursal".
    prior_disbursed = mysql_client.fetch_customer_disbursed_amount(
        lead["customer_id"], exclude_ticket_id=ticket_id)

    profile = build_financial_profile(cam_fields, prior_disbursed_amount=prior_disbursed)
    capacity = profile["financial_capacity"]

    mysql_client.save_lead_score({
        "application_id": lead["application_id"],
        "provider_name": lead["provider_name"],
        "ticket_id": ticket_id,
        "customer_name": lead["customer_name"],
        "customer_phone": lead["customer_phone"],
        "cam_url": cam_url,
        "secured_bucket": capacity["secured_bucket"],
        "unsecured_bucket": capacity["unsecured_bucket"],
        "financial_profile": profile,
        "cam_fields": cam_fields,
    })
    return "scored"


def _score_many(leads, skip_already_scored=True):
    """Scores a list of leads. One bad lead never stops the whole run."""
    already = mysql_client.get_scored_keys() if skip_already_scored else set()

    counts = {"total": len(leads), "scored": 0, "already_scored": 0,
              "no_cam": 0, "cam_error": 0, "error": 0}
    print(f"Leads to look at: {len(leads)}")

    for i, lead in enumerate(leads, start=1):
        key = (lead["application_id"], lead["provider_name"])
        if key in already:
            counts["already_scored"] += 1
            continue
        try:
            status = score_lead(lead)
        except Exception as e:
            print(f"  ticket {lead.get('ticket_id')}: unexpected error: {e}")
            status = "error"
        counts[status] += 1

        if i % 25 == 0:
            print(f"  ...{i}/{len(leads)} done")

    print(
        "Finished. "
        f"scored={counts['scored']}, already scored={counts['already_scored']}, "
        f"no CAM yet={counts['no_cam']}, CAM unreadable={counts['cam_error']}, "
        f"errors={counts['error']}"
    )
    return counts


# ---------------------------------------------------------------- entry points

def run_daily(fresh=False):
    """
    Background sync: score leads updated in the last DAILY_LOOKBACK_DAYS days
    that are not scored yet (and now have a CAM).

    fresh=True wipes EVERY saved score first and re-scores everything.
    """
    if fresh:
        mysql_client.clear_all_scores()
        leads = mysql_client.fetch_leads(since_date=None)
    else:
        leads = mysql_client.fetch_leads(
            since_date=date.today() - timedelta(days=DAILY_LOOKBACK_DAYS))
    return _score_many(leads, skip_already_scored=not fresh)


def run_for_month(month_str, fresh=False):
    """
    Scores every lead applied in the given month ("YYYY-MM") and files it
    into its buckets.

    fresh=False: leads of that month that are already scored are skipped.
    fresh=True : the month is re-scored even if scored before (nothing else
                 is deleted - other months stay untouched).
    """
    start, end = month_range(month_str)
    print(f"Scoring month {month_str} (applications from {start} up to {end})")
    leads = mysql_client.fetch_leads_between(start, end)
    return _score_many(leads, skip_already_scored=not fresh)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score leads from OMS")
    parser.add_argument("--month", help="Score only this month, like 2026-09")
    parser.add_argument("--fresh", action="store_true",
                        help="Re-score (with no --month: wipe all scores first)")
    args = parser.parse_args()

    mysql_client.ensure_scores_table()
    if args.month:
        run_for_month(args.month, fresh=args.fresh)
    else:
        run_daily(fresh=args.fresh)