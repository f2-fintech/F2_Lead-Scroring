# F2 Lead Scoring

Daily pipeline that fetches leads from OMS, reads their CAM (Excel),
scores them, sorts them into 4 priority buckets, and writes an
AI-generated plain-English explanation for each bucket placement.

## Setup

1. `python -m venv venv`
2. Activate it:
   - Windows: `venv\Scripts\activate`
   - Mac/Linux: `source venv/bin/activate`
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in your real MySQL credentials
   and Gemini API key (from https://aistudio.google.com/apikey).

## Running

```bash
# Daily sync - only today's new/updated leads
python main.py

# One-time historical backfill - last 60 days, for testing the model
python main.py --historical
```

## How it fits together

```
db/mysql_client.py       -> talks to OMS MySQL (fetch leads, find CAM attachment, save results)
extraction/cam_extractor.py -> reads the CAM Excel (fixed-cell lookup, or Gemini fallback)
extraction/gemini_client.py -> Gemini calls (remark summary, fallback extraction, bucket explanation)
scoring/scoring_engine.py   -> the weighted scoring model + hard overrides + bucket assignment
main.py                     -> ties it all together, run this daily
```

Results are saved into a `lead_scores` table (auto-created on first run)
that your web app's backend/API should read from to display buckets,
F2 Hot Leads, and the per-lead portfolio page.

## Things you'll likely want to tune

- `scoring/scoring_engine.py` -> `TIER_1_CITIES` / `TIER_2_CITIES` sets -
  add your actual city list.
- `scoring/scoring_engine.py` -> `WEIGHTS` dict - adjust once you've run
  the historical batch and compared against real outcomes.
- `.env` -> `SECURED_LOAN_THRESHOLD` / `UNSECURED_LOAN_THRESHOLD` - the
  ₹5L / ₹35L cutoffs for what counts as a "significant" loan.
- `main.py` -> `has_missed_payments` / `max_dpd_days` / `suit_filed` are
  currently hardcoded to safe defaults since this pipeline only reads the
  CAM (which already contains the CIBIL score). If you later want the
  actual DPD history table from the CIBIL PDF, we can add a
  `extraction/cibil_pdf_extractor.py` that feeds those three values in.
