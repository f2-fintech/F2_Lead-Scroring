"""
bucket_outcomes.py - does a higher SECURED bucket really mean a better result?

Takes the leads stored in `lead_data`, works out the secured bucket for each
one (the same way the dashboard does), and shows, bucket by bucket, how many
of those leads ended up Approved / Disbursed and how many were Rejected.

If the buckets are well set, the success rate should go DOWN steadily from
Excellent to Very Low. If two neighbouring buckets have almost the same rate,
the thresholds between them are not separating anything and should be moved.

Put this file in the SAME folder as server.py and main.py, then run:

    python bucket_outcomes.py                    -> every stored lead
    python bucket_outcomes.py --month 2026-09    -> one application month
    python bucket_outcomes.py --csv outcomes.csv -> also save the table
    python bucket_outcomes.py --good "Approved,Disbursed" --bad "Rejected,Declined"

Only READS from the database. It never changes anything.

How to read the result
  * success rate = good / (good + bad). Leads still in process are left out
    of the rate (we do not know their result yet) but are shown in the table.
  * disbursed rate = disbursed / all leads in the bucket.
  * A bucket with fewer than --min-decided decided leads is marked "few" -
    do not trust its rate.
"""
import argparse
import csv

SECURED_ORDER = ["Excellent", "Good", "Medium", "Low", "Very Low", "Insufficient data"]


def classify_status(status, good_words, bad_words):
    """-> 'good' | 'bad' | 'open' from the ticket status text."""
    text = str(status or "").strip().lower()
    if any(w in text for w in good_words):
        return "good"
    if any(w in text for w in bad_words):
        return "bad"
    return "open"


def summarize(leads, good_words, bad_words, min_decided=30):
    """
    Pure calculation (no database): one row per secured bucket.
    Returns (rows, status_counts).
    """
    rows = {b: {"bucket": b, "leads": 0, "good": 0, "bad": 0, "open": 0,
                "disbursed": 0, "eligibility_total": 0.0, "eligibility_n": 0}
            for b in SECURED_ORDER}
    status_counts = {}

    for lead in leads:
        bucket = lead.get("secured_bucket")
        if bucket not in rows:
            rows[bucket] = {"bucket": bucket, "leads": 0, "good": 0, "bad": 0, "open": 0,
                            "disbursed": 0, "eligibility_total": 0.0, "eligibility_n": 0}
        r = rows[bucket]
        status = lead.get("ticket_status")
        status_counts[status] = status_counts.get(status, 0) + 1

        r["leads"] += 1
        r[classify_status(status, good_words, bad_words)] += 1
        if "disburs" in str(status or "").lower():
            r["disbursed"] += 1
        elig = lead.get("secured_eligibility")
        if elig is not None:
            r["eligibility_total"] += float(elig)
            r["eligibility_n"] += 1

    out = []
    for b in SECURED_ORDER + [k for k in rows if k not in SECURED_ORDER]:
        r = rows.get(b)
        if not r or r["leads"] == 0:
            continue
        decided = r["good"] + r["bad"]
        r["decided"] = decided
        r["success_rate"] = round(100 * r["good"] / decided, 1) if decided else None
        r["disbursed_rate"] = round(100 * r["disbursed"] / r["leads"], 1)
        r["avg_eligibility"] = (
            round(r["eligibility_total"] / r["eligibility_n"]) if r["eligibility_n"] else None)
        r["few"] = decided < min_decided
        out.append(r)
    return out, status_counts


def check_order(rows):
    """Does the success rate fall steadily from Excellent down to Very Low?
    Returns a list of plain-English warnings (empty = looks right)."""
    ranked = [r for r in rows if r["bucket"] in SECURED_ORDER[:5] and r["success_rate"] is not None]
    warnings = []
    for upper, lower in zip(ranked, ranked[1:]):
        if lower["success_rate"] >= upper["success_rate"]:
            note = " (but one of them has few decided leads)" if (upper["few"] or lower["few"]) else ""
            warnings.append(
                f"'{lower['bucket']}' ({lower['success_rate']}%) is not lower than "
                f"'{upper['bucket']}' ({upper['success_rate']}%){note}.")
    return warnings


def fmt_amount(value):
    if value is None:
        return "-"
    if value >= 10000000:
        return f"{value / 10000000:.2f} Cr"
    if value >= 100000:
        return f"{value / 100000:.1f} L"
    return f"{value:,.0f}"


def print_report(rows, status_counts, label):
    print("=" * 96)
    print(f"  SECURED BUCKET vs OUTCOME   ({label})")
    print("=" * 96)
    print(f"{'Bucket':<18}{'Leads':>7}{'Good':>7}{'Bad':>7}{'Open':>7}"
          f"{'Success %':>11}{'Disbursed %':>13}{'Avg eligibility':>18}")
    print("-" * 96)
    for r in rows:
        rate = "-" if r["success_rate"] is None else f"{r['success_rate']}"
        flag = "  few" if r["few"] and r["decided"] else ""
        print(f"{r['bucket']:<18}{r['leads']:>7}{r['good']:>7}{r['bad']:>7}{r['open']:>7}"
              f"{rate:>11}{r['disbursed_rate']:>13}{fmt_amount(r['avg_eligibility']):>18}{flag}")
    print("-" * 96)
    print("Ticket statuses seen (check these were sorted into good / bad correctly):")
    for status, n in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        print(f"   {str(status):<30}{n}")
    warnings = check_order(rows)
    print()
    if warnings:
        print("Buckets that do NOT separate cleanly:")
        for w in warnings:
            print("   - " + w)
    else:
        print("Success rate falls steadily from Excellent to Very Low - the order looks right.")


def save_csv(rows, path):
    columns = ["bucket", "leads", "good", "bad", "open", "decided",
               "success_rate", "disbursed_rate", "avg_eligibility", "few"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved the table to {path}")


def main():
    parser = argparse.ArgumentParser(description="Secured bucket vs real outcome")
    parser.add_argument("--month", help="Only applications from this month, like 2026-09")
    parser.add_argument("--good", default="approved,disbursed",
                        help="Words in a ticket status that mean SUCCESS (comma separated)")
    parser.add_argument("--bad", default="reject,declin",
                        help="Words in a ticket status that mean FAILURE (comma separated)")
    parser.add_argument("--min-decided", type=int, default=30,
                        help="Buckets with fewer decided leads are marked 'few' (default 30)")
    parser.add_argument("--csv", help="Also save the table to this file")
    args = parser.parse_args()

    from allocation import allocate     # imported here so summarize() can be tested alone

    filters = {"month": args.month} if args.month else {}
    result = allocate(filters)
    print(f"Leads worked out: {result['total_leads']} "
          f"(matched {result['matched_rows']}, errors {result['errors']})")

    good = [w.strip().lower() for w in args.good.split(",") if w.strip()]
    bad = [w.strip().lower() for w in args.bad.split(",") if w.strip()]
    rows, status_counts = summarize(result["leads"], good, bad, args.min_decided)
    print_report(rows, status_counts, args.month or "all stored leads")
    if args.csv:
        save_csv(rows, args.csv)


if __name__ == "__main__":
    main()