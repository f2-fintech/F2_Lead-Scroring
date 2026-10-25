import json, datetime, os, sys

# Look for financial_analysis.py (and config.py / loan_rules.py) in this
# folder and the usual sub-folders, so the file can sit anywhere in the project.
_here = os.path.dirname(os.path.abspath(__file__))
_folders = ["", "analysis", "extraction", "scoring", "db"]
for _sub in reversed(_folders):
    _path = os.path.join(_here, _sub)
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.insert(0, _path)
try:
    import financial_analysis as fa
except ImportError as e:
    print("Could not import financial_analysis:", e)
    print("Looked in:", [os.path.join(_here, f) for f in _folders])
    print("Copy test_engine.py into your project's main folder and run it again.")
    sys.exit(1)
print("Using:", fa.__file__)

def run(name, **kw):
    base = {"total_monthly_income":200000,"foir_percent":60,"loans":[],"age_direct":35,
            "house_type":None,"experience_years":None,"financial_assets":{},
            "enquiries_3m":None,"enquiries_6m":None,"cibil_score":None,"recurring_commitments":{}}
    base.update(kw.pop("cam", {}))
    pd = kw.pop("prior", 0)
    p = fa.build_financial_profile(base, prior_disbursed_amount=pd)
    c = p["financial_capacity"]
    q = c["secured_quality"]
    print(f"{name:42s} surplus={c['monthly_surplus']!s:>8} tenure={c['secured_method']['tenure_years_used']:>4} "
          f"elig={c['secured_cap_eligibility']!s:>9} cap_bucket={c['secured_capacity_bucket']:<17} "
          f"q={q['score']:+d} step={q['step']:+d} FINAL={c['secured_bucket']:<17} unsec={c['unsecured_bucket']}")
    return c

def loan(emi, sanction=200000, bal=100000, end="2035-01-01"):
    return {"loan_type":"Personal Loan","emi_amount":emi,"emi_amount_reported":emi,
            "sanction_amount":sanction,"current_balance":bal,"end_date":end}

# DOB round-trip fix
dob = json.loads(json.dumps({"d":datetime.datetime(1975,5,14)},default=str))["d"]
print("DOB after JSON round trip:", fa.parse_date_of_birth(dob), "| age-from-dob example ok\n")

c = run("1 young, clean (age 35, surplus 80k)", cam={"loans":[loan(30000)],"recurring_commitments":{"sip":10000}})
run("2 same but age 50",  cam={"age_direct":50,"loans":[loan(30000)],"recurring_commitments":{"sip":10000}})
run("3 same but age 55",  cam={"age_direct":55,"loans":[loan(30000)],"recurring_commitments":{"sip":10000}})
run("4 same but age 58",  cam={"age_direct":58,"loans":[loan(30000)],"recurring_commitments":{"sip":10000}})
run("5 same but age 60", cam={"age_direct":60,"loans":[loan(30000)],"recurring_commitments":{"sip":10000}})
run("6 age unknown",       cam={"age_direct":None,"loans":[loan(30000)]})
print()
run("7 +owned +exp8 +FD  (score +3 -> up)", cam={"loans":[loan(30000)],"house_type":"Owned","experience_years":8,"financial_assets":{"fixed_deposit":500000}})
run("8 rented + 6 enq/3m (score -3 -> down)", cam={"loans":[loan(30000)],"house_type":"Rented","enquiries_3m":6})
run("9 owned only (+1 -> no step)", cam={"loans":[loan(30000)],"house_type":"Owned"})
run("10 +3 but ZERO capacity (no lift)", cam={"loans":[loan(130000)],"house_type":"Owned","experience_years":9,"financial_assets":{"fixed_deposit":9}})
run("11 +3 but CIBIL 660 caps at Medium", cam={"loans":[loan(30000)],"house_type":"Owned","experience_years":8,"financial_assets":{"fixed_deposit":5},"cibil_score":660})
print()
# small loans: 6 small loans, tiny EMIs vs heavy EMIs
tiny = [loan(1500, sanction=50000, bal=20000) for _ in range(6)]       # 9,000 = 4.5% of 2L
heavy= [loan(9000, sanction=150000, bal=90000) for _ in range(6)]      # 54,000 = 27% of 2L
mid  = [loan(5000, sanction=150000, bal=90000) for _ in range(6)]      # 30,000 = 15%
for nm, ls in (("12 6 small loans, tiny EMI (4.5%)",tiny),("13 6 small loans, mid EMI (15%)",mid),("14 6 small loans, heavy EMI (27%)",heavy)):
    c = run(nm, cam={"loans":ls})
    print("      items:", [(i['label'][:46], i['points']) for i in c['secured_quality']['items']])
print()
c = run("15 income missing", cam={"total_monthly_income":None}); print("      ", c["secured_bucket_cap_reasons"])
c = run("16 FOIR missing",   cam={"foir_percent":None});        print("      ", c["secured_bucket_cap_reasons"])
c = run("17 prior disbursed 70L", cam={"loans":[loan(30000)]}, prior=7000000)
c = run("18 EMIs exceed FOIR allowance", cam={"loans":[loan(150000)]}); print("      ", c["secured_bucket_cap_reasons"])
print()
print("--- NEW RULES: end ages 70 (secured) / 65 (unsecured) ---")
for a in (58, 62, 65, 66, 69, 70):
    run(f"20 age {a}", cam={"age_direct": a, "loans": [loan(30000)]})
print()
print("--- NEW RULES: investments and rented house ---")
run("21 investments 25L  (+1 -> no step alone)", cam={"loans":[loan(30000)], "financial_assets":{"mutual_fund":2500000}})
run("22 investments 60L  (+2 -> step up)", cam={"loans":[loan(30000)], "financial_assets":{"fixed_deposit":3000000, "mutual_fund":3000000}})
run("23 investments exactly 50L (+1)", cam={"loans":[loan(30000)], "financial_assets":{"mutual_fund":5000000}})
run("24 rented + married (-1)", cam={"loans":[loan(30000)], "house_type":"Rented", "marital_status":"Married"})
run("25 rented + unmarried (-2 -> step down)", cam={"loans":[loan(30000)], "house_type":"Rented", "marital_status":"Unmarried"})
run("26 rented, marital unknown (-1)", cam={"loans":[loan(30000)], "house_type":"Rented"})
print()
print("--- NEW RULES: unsecured ladder (surplus x 50) ---")
for inc in (40000, 80000, 160000, 300000, 600000, 1200000):
    c = run(f"27 income {inc}", cam={"total_monthly_income": inc, "foir_percent": 50})
    print("      unsecured eligibility:", c["unsecured_cap_eligibility"], "->", c["unsecured_bucket"])
print()
# the flat x100 rule vs new at age 35 for the same case
c = run("19 boundary: age 35, surplus 100k", cam={"total_monthly_income":200000,"foir_percent":50})
print("   loan per Rs1 of EMI at 20y:", c["secured_method"]["loan_per_rupee_of_emi"])