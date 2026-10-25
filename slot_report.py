"""
slot_report.py - how are your REAL remarks being sorted into P1 / P2 / P3 / P4?

Put this file in the SAME folder as server.py and main.py, then run:

    python slot_report.py                     -> counts + remarks that matched nothing
    python slot_report.py --month 2026-09     -> one application month
    python slot_report.py --show 60           -> show 60 unmatched remarks (default 30)
    python slot_report.py --csv slots.csv     -> save every lead with its slot and reasons

Only READS from the database. It never changes anything.

Use it like this:
  1. Look at "Remarks that matched NO keyword". Those all fell into P2 only
     because nothing matched. If you see a wording that should be P1 or P3
     (for example a different spelling of "not picking up"), add it to the
     pattern lists in remark_slots.py, then run this again.
  2. Look at "Most common reasons" to see which rules do most of the work.
  3. If a lead is in the wrong slot, put its remark in test_slots.py with the
     slot it should have, and change remark_slots.py until test_slots.py passes.
"""
import argparse
import csv
from collections import Counter

from remark_slots import classify_remarks, SLOT_ORDER

SLOT_NAMES = {
    "P1": "Act now",
    "P2": "Fix in 3 months",
    "P3": "Hard to fix",
    "P4": "No remark",
}


def summarize(leads):
    """Pure calculation (no database). Returns a dict of results."""
    by_slot = Counter()
    by_bucket = {}
    reasons = Counter()
    unmatched = Counter()
    rows = []

    for lead in leads:
        credit, ops = lead.get("mis_credit_remark"), lead.get("mis_ops_remark")
        result = classify_remarks(credit, ops)
        slot = result["slot"]
        by_slot[slot] += 1
        by_bucket.setdefault(lead.get("unsecured_bucket"), Counter())[slot] += 1
        for s in result["signals"]:
            reasons[f"{s['label']}  [{s['slot']}]"] += 1
        if result["other"]:
            text = " | ".join(x for x in (str(credit or "").strip(), str(ops or "").strip()) if x)
            unmatched[" ".join(text.split())[:140]] += 1
        rows.append({
            "ticket_id": lead.get("ticket_id"),
            "customer_name": lead.get("customer_name"),
            "unsecured_bucket": lead.get("unsecured_bucket"),
            "slot": slot,
            "reasons": "; ".join(result["reasons"]),
            "credit_remark": credit or "",
            "ops_remark": ops or "",
        })
    return {"by_slot": by_slot, "by_bucket": by_bucket, "reasons": reasons,
            "unmatched": unmatched, "rows": rows}


def print_report(res, total, show):
    print("=" * 70)
    print(f"  PRIORITY SLOTS   ({total} leads)")
    print("=" * 70)
    for slot in SLOT_ORDER:
        n = res["by_slot"].get(slot, 0)
        pct = f"{100 * n / total:.0f}%" if total else "-"
        print(f"  {slot}  {SLOT_NAMES[slot]:<16}{n:>7}   {pct:>5}")

    print("\nSlots inside each UNSECURED bucket")
    print(f"  {'Bucket':<12}" + "".join(f"{s:>7}" for s in SLOT_ORDER))
    for bucket in ["Excellent", "Good", "Medium", "Low", "Very Low"]:
        c = res["by_bucket"].get(bucket, Counter())
        print(f"  {bucket:<12}" + "".join(f"{c.get(s, 0):>7}" for s in SLOT_ORDER))

    print("\nMost common reasons found")
    for label, n in res["reasons"].most_common(15):
        print(f"  {n:>6}  {label}")

    unmatched_total = sum(res["unmatched"].values())
    print(f"\nRemarks that matched NO keyword (fell into P2): {unmatched_total} lead(s)")
    for text, n in res["unmatched"].most_common(show):
        print(f"  {n:>4}x  {text}")
    if len(res["unmatched"]) > show:
        print(f"  ... and {len(res['unmatched']) - show} more different remarks (use --show to see more)")


def main():
    parser = argparse.ArgumentParser(description="How are remarks sorted into P1-P4?")
    parser.add_argument("--month", help="Only applications from this month, like 2026-09")
    parser.add_argument("--show", type=int, default=30, help="How many unmatched remarks to list")
    parser.add_argument("--csv", help="Save every lead with its slot and reasons to this file")
    args = parser.parse_args()

    from allocation import allocate        # imported here so summarize() can be tested alone

    result = allocate({"month": args.month} if args.month else {})
    leads = result["leads"]
    print(f"Leads worked out: {len(leads)} (errors {result['errors']})")
    res = summarize(leads)
    print_report(res, len(leads), args.show)

    if args.csv:
        columns = ["ticket_id", "customer_name", "unsecured_bucket", "slot", "reasons",
                   "credit_remark", "ops_remark"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(res["rows"])
        print(f"\nSaved {len(res['rows'])} rows to {args.csv}")


if __name__ == "__main__":
    main()