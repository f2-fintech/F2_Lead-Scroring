"""
All LLM calls live here:

1. summarize_credit_remark   - reads the free-text CREDIT REMARK on the CAM.
2. fallback_extract_cam_fields - used only if fixed-cell CAM lookup fails.
3. decide_bucket_and_explain - the reasoning step. Takes the deterministic
   financial profile and returns the final bucket plus a structured,
   plain-English explanation. 

Calls Google's Gemini API. File is still named gemini_client.py so imports elsewhere
don't need to change. Key goes in .env as GEMINI_API_KEY.
"""
import json
import re
import requests
from config import config
import time

# Ordered list of models to try. If the first is overloaded (503) or
# unavailable (404), we fall through to the next one automatically.
# Google retires models from time to time (the old 1.5 and 2.0 ones are gone),
# so you can also set GEMINI_MODELS in .env (comma separated) without touching
# this file. And if EVERY model here is gone, the code asks Google which models
# your key can use right now and tries those (see _discover_models).
DEFAULT_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def _configured_models():
    raw = getattr(config, "GEMINI_MODELS", "") or ""
    names = [m.strip() for m in raw.split(",") if m.strip()]
    return names or list(DEFAULT_MODELS)


MODELS = _configured_models()
_discovered_models = None


def _discover_models():
    """Asks Google which Flash models this API key can use today: newest first,
    the normal 'flash' before 'flash-lite'. Only plain text models (no image,
    audio, live or preview versions). Returns [] if the list cannot be read."""
    global _discovered_models
    if _discovered_models is not None:
        return _discovered_models
    found = []
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": config.GEMINI_API_KEY, "pageSize": 200}, timeout=30)
        if r.status_code == 200:
            for m in r.json().get("models", []):
                name = str(m.get("name", "")).replace("models/", "")
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                match = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?", name)
                if match:
                    found.append((float(match.group(1)), 1 if match.group(2) else 0, name))
    except Exception:
        pass
    found.sort(key=lambda x: (-x[0], x[1]))
    _discovered_models = [name for _, _, name in found]
    return _discovered_models


BUCKETS = ["Very Low", "Low", "Medium", "Good", "Excellent"]

def _call_gemini(prompt: str, max_tokens: int = 1500, retries: int = 3) -> str:
    """
    Call the Gemini API with automatic model fallback and exponential backoff.
    If a model returns 503 (overloaded) or 404 (not found), we move to the
    next model in the MODELS list. Rate-limit (429) and transient errors
    are retried with increasing delays.
    """
    headers = {"Content-Type": "application/json"}

    last_error = None

    models_to_try = list(MODELS)
    discovery_done = False
    i = 0
    while i < len(models_to_try):
        model = models_to_try[i]
        i += 1
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={config.GEMINI_API_KEY}"
        current_max_tokens = max_tokens

        for attempt in range(retries + 1):
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": current_max_tokens
                }
            }

            try:
                response = requests.post(url, headers=headers, json=payload, timeout=90)
            except requests.exceptions.Timeout:
                last_error = RuntimeError(f"Gemini API timeout for model {model}")
                time.sleep(2.0 * (attempt + 1))
                continue
            except requests.exceptions.ConnectionError as e:
                last_error = RuntimeError(f"Connection error for model {model}: {e}")
                time.sleep(2.0 * (attempt + 1))
                continue

            if response.status_code == 404:
                # Model doesn't exist — skip to next model immediately
                last_error = RuntimeError(f"Model {model} not found (404)")
                break

            if response.status_code == 503:
                # Model overloaded — retry once then try next model
                last_error = RuntimeError(f"Model {model} overloaded (503)")
                if attempt < 1:
                    time.sleep(5.0)
                    continue
                break  # move to next model

            if response.status_code == 429:
                # Rate limited — exponential backoff
                wait = min(15.0 * (attempt + 1), 60.0)
                last_error = RuntimeError(f"Rate limited on {model} (429)")
                time.sleep(wait)
                continue

            if response.status_code != 200:
                last_error = RuntimeError(f"Gemini API error {response.status_code} on {model}: {response.text[:300]}")
                time.sleep(2.0 * (attempt + 1))
                continue

            # --- Successful HTTP response ---
            try:
                data = response.json()
                candidate = data.get("candidates", [])[0]
                parts = candidate.get("content", {}).get("parts", [])
                content = "".join(p.get("text", "") for p in parts).strip()
                if not parts:
                    raise IndexError("no parts")
                finish_reason = candidate.get("finishReason")
            except (IndexError, KeyError):
                last_error = RuntimeError(f"Unexpected response format from {model}: {response.text[:300]}")
                time.sleep(1.5)
                continue

            if finish_reason == "MAX_TOKENS":
                last_error = RuntimeError(f"Response truncated at max_tokens={current_max_tokens}")
                current_max_tokens = min(current_max_tokens + 1000, 8192)
                time.sleep(1.0)
                continue

            if content:
                return content

            last_error = RuntimeError(f"Model {model} returned an empty response")
            time.sleep(1.5)

        # Every model in our list failed: ask Google what is available now.
        if i >= len(models_to_try) and not discovery_done:
            discovery_done = True
            for name in _discover_models():
                if name not in models_to_try:
                    models_to_try.append(name)

    raise last_error or RuntimeError("No Gemini model could be used - check GEMINI_API_KEY")


def _extract_json(text: str):
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in AI response: {text[:200]}")
    return json.loads(match.group(1))


def summarize_credit_remark(remark_text: str) -> str:
    if not remark_text or not str(remark_text).strip():
        return ""
    prompt = f"""Summarize this credit analyst's remark in 2-3 short, plain
sentences. Keep all numbers/facts accurate, just make it easier to read.
Return ONLY the summary text, nothing else.

Remark:
{remark_text}"""
    return _call_gemini(prompt)


def fallback_extract_cam_fields(raw_sheet_text: str) -> dict:
    prompt = f"""You are reading a CAM (Credit Appraisal Memo) Excel sheet for a
loan applicant. Extract the following fields and return ONLY a valid JSON
object, no other text, no markdown fences.

Fields to extract:
- total_monthly_income (number, in rupees)
- foir_percent (number, e.g. 70 for 70%)
- existing_emi (number, in rupees)
- experience_years (number, if this is a doctor/professional; else null)
- profession (string, e.g. "DOCTOR", "CA", "CS", or null)
- current_address (string)
- total_active_loans (number)
- loans (array of objects, one per tradeline, each with:
    lender, loan_type, ownership, sanction_amount, current_balance,
    emi_amount, tenure_months)
- credit_remark (string - the free-text analyst remark, if present)

Sheet content:
{raw_sheet_text}"""
    result = _extract_json(_call_gemini(prompt, max_tokens=1200))
    if not isinstance(result, dict):
        raise ValueError(
            f"AI explanation response was not a JSON object (got {type(result).__name__}): {result!r}"
        )
    return result


def generate_bucket_explanation(financial_profile: dict, credit_remark: str = "") -> dict:
    """
    The bucket is ALREADY decided deterministically in Python. 
    This function explains why, genuinely interpreting the CAM remarks against 
    the bucket allocation and hard caps.
    """
    cap = financial_profile["financial_capacity"]
    prompt = f"""You are a credit analyst explaining an ALREADY-DECIDED lead
classification. Do NOT change or recompute the bucket decisions below.
Read the provided CAM analyst remark carefully and evaluate how it aligns with, 
supports, or contradicts the deterministic bucket placement and caps. 
CIBIL reports are NOT part of this analysis. The only time CIBIL may be mentioned
is when the profile shows it capped the SECURED or UNSECURED bucket (secured_cibil_cap / unsecured_cibil_cap is set) -
then say so, using only the CIBIL score and the cap reason given in the profile.
Explicitly call out positive and negative points identified in the remarks 
and explicitly relate them to the hard caps (like the CIBIL cap on secured, and the age limit on unsecured).

HOW THE SECURED BUCKET IS REACHED - explain the secured bucket in these three steps:
1. Capacity: the monthly surplus is turned into a loan amount using the tenure left
   before retirement (months of tenure, see secured_method). No interest rate is used. That amount
   (secured_cap_eligibility) gives secured_capacity_bucket. Age matters here:
   a shorter tenure means a smaller amount. If tenure_limited_by_age is true, say so.
2. Quality: secured_quality lists plus and minus points. A net score at or above
   step_up_at moves the bucket one step up, at or below step_down_at one step down.
   Mention the items that counted and whether a step happened (step is +1, -1 or 0).
3. Caps: the CIBIL cap can only lower the bucket (secured_bucket_cap_reasons).
If secured_zero_reason is set, say that reason. If the secured bucket is
"Insufficient data", say the CAM does not give income or FOIR (see
secured_missing_inputs) so no secured bucket could be worked out - do not call it Very Low.
The unsecured bucket works as before (age cap and CIBIL cap, no quality step).

CRITICAL NUMBER RULES - follow these exactly:
1. Every rupee figure you write must be COPIED VERBATIM from the FINANCIAL
   PROFILE below.
2. Format ALL rupee amounts using the Indian numbering system - Lakhs and
   Crores (e.g. "Rs.45,000", "Rs.4.5 Lakh", "Rs.45 Lakh", "Rs.1.2 Crore").

FINANCIAL PROFILE:
{json.dumps(financial_profile, indent=2, default=str)}

This lead's decided buckets are:
- Secured loan eligibility bucket: {cap['secured_bucket']}
- Unsecured loan eligibility bucket: {cap['unsecured_bucket']}

Analyst remark: {credit_remark}

If income_not_given is true in the profile, say clearly that income was not given in the
CAM and that this is why the secured bucket is "Insufficient data" and the unsecured bucket is Very Low.
Also cover the running loans and their total monthly EMI, and the applicant's age,
where they affect the buckets.

Provide your analysis in EXACTLY this JSON shape (no markdown fences or extra text):
{{
  "overall_summary": "<A comprehensive summary interpreting the analyst's remark in the context of the overall financial profile and bucket decision>",
  "secured_explanation": "<Why the secured bucket was assigned, detailing any hard caps applied>",
  "unsecured_explanation": "<Why the unsecured bucket was assigned, detailing any hard caps applied>",
  "key_positive_factors": ["<A specific strength derived from the profile or remarks>", "..."],
  "key_risk_factors": ["<A specific risk or concern derived from the profile, remarks, or hard caps>", "..."],
  "missing_information": ["<Anything missing or unclear>"]
}}"""

    result = _extract_json(_call_gemini(prompt, max_tokens=2000, retries=3))
    if not isinstance(result, dict):
        raise ValueError(
            f"AI explanation response was not a JSON object (got {type(result).__name__}): {result!r}"
        )
    return result