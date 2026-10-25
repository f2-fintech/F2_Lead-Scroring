"""
Loads all settings from the .env file so the rest of the code
never hardcodes passwords or keys directly.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # OMS MySQL (read-only usage)
    MYSQL_HOST = os.getenv("MYSQL_HOST")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")

    # Our own results database (separate from OMS - never written to OMS)
    RESULTS_DB_HOST = os.getenv("RESULTS_DB_HOST")
    RESULTS_DB_PORT = int(os.getenv("RESULTS_DB_PORT", 3306))
    RESULTS_DB_USER = os.getenv("RESULTS_DB_USER")
    RESULTS_DB_PASSWORD = os.getenv("RESULTS_DB_PASSWORD")
    RESULTS_DB_DATABASE = os.getenv("RESULTS_DB_DATABASE")

    # MIS Google Sheets (read-only). Share each sheet (view access) with the
    # service account's email address.
    MIS_UNSECURED_SHEET_URL = os.getenv("MIS_UNSECURED_SHEET_URL")
    MIS_SECURED_SHEET_URL = os.getenv("MIS_SECURED_SHEET_URL")
    GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

    # Gemini (LLM for remark summaries, fallback extraction, bucket decisions)
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    # Optional: models to use, best first, separated by commas, e.g.
    #   GEMINI_MODELS=gemini-3.5-flash,gemini-3.1-flash-lite
    # Leave it out to use the list inside gemini_client.py.
    GEMINI_MODELS = os.getenv("GEMINI_MODELS", "")

    # Loan exposure thresholds (in rupees)
    SECURED_LOAN_THRESHOLD = float(os.getenv("SECURED_LOAN_THRESHOLD", 10000000))    # 1 Cr
    UNSECURED_LOAN_THRESHOLD = float(os.getenv("UNSECURED_LOAN_THRESHOLD", 3500000))  # 35 L

    # Hard risk flag: unsecured exposure beyond this multiple of ANNUAL income
    # is flagged as extreme. Tune once you've seen real results.
    EXTREME_UNSECURED_EXPOSURE_RATIO = float(os.getenv("EXTREME_UNSECURED_EXPOSURE_RATIO", 2.0))


config = Config()