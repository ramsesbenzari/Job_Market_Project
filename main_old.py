import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

import geonamescache
import pandas as pd
import requests

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

from extractors import extract_industry, extract_education, extract_benefits, extract_skills, extract_soft_skills

PROJECT_ID = "my-first-project-27273"
DATASET = "data_job_market"
TABLE = "us_job_data"
TABLE_ID = f"{PROJECT_ID}.{DATASET}.{TABLE}"

ROLES = [
    "Data Analyst",
    "Product Analyst",
    "Marketing Analyst",
    "Business Analyst",
    "Business Intelligence Analyst",
    "Financial Analyst",
    "Operation Analyst",
    "Data Scientist",
]


# ═══════════════════════════════════════════════════════════════════════════════
# US STATE & COUNTRY CONSTANTS (for post-fetch filtering)
# ═══════════════════════════════════════════════════════════════════════════════

_US_STATE_ABBRS: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "GU", "VI", "AS", "MP",   # territories
})

_INTL_COUNTRY_TOKENS: frozenset[str] = frozenset({
    "germany", "deutschland", "brazil", "brasil", "canada", "uk",
    "united kingdom", "england", "france", "australia", "india",
    "netherlands", "spain", "mexico", "singapore", "japan", "china",
    "poland", "sweden", "norway", "denmark", "finland", "switzerland",
    "austria", "belgium", "ireland", "italy", "portugal", "romania",
    "czechia", "hungary", "colombia", "argentina", "chile", "peru",
    "south africa", "nigeria", "kenya", "egypt", "israel", "uae",
    "dubai", "hong kong", "new zealand", "philippines", "indonesia",
    "malaysia", "thailand", "vietnam", "taiwan", "south korea",
    "ukraine", "russia", "turkey", "pakistan", "bangladesh",
})


# ──────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS FOR GEO & EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

def _is_us_location(location_str: Optional[str]) -> bool:
    """
    Return True if location_str is consistent with a US job posting.

    Logic (first match wins):
      1. If the string contains a known non-US country name → False
      2. If the string contains a US state abbreviation token → True
      3. If the string contains US-specific strings → True
      4. If the string is None / empty / "remote" / "worldwide" → True
      5. Default → True  (benefit of the doubt)
    """
    if not location_str:
        return True  # missing location ≠ international

    loc_lower = location_str.lower().strip()

    # ── 1. Known international country names ────────────────────────────────
    for country in _INTL_COUNTRY_TOKENS:
        if country in loc_lower:
            return False

    # ── 2. US state abbreviation token ──────────────────────────────────────
    for token in re.split(r"[,/|;\s\-–—]+", location_str):
        if token.strip().upper() in _US_STATE_ABBRS:
            return True

    # ── 3. US-specific strings ───────────────────────────────────────────────
    us_signals = (
        "united states", " usa", " us ", "(us)", "(usa)",
        "u.s.a", "u.s.", "america",
    )
    for sig in us_signals:
        if sig in loc_lower:
            return True

    # ── 4. Remote / worldwide / empty ────────────────────────────────────────
    remote_signals = (
        "remote", "worldwide", "anywhere", "work from home",
        "wfh", "distributed", "global",
    )
    for sig in remote_signals:
        if sig in loc_lower:
            return True

    # ── 5. Default: pass through ──────────────────────────────────────────────
    return True


def _normalize(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


# ──────────────────────────────────────────────────────────────────────────────
# STATE EXTRACTION (powered by geonamescache — fully offline)
# ──────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _build_geo_lookups():
    """
    Build and cache all lookup tables from geonamescache.
    Called once per process; subsequent calls hit the LRU cache instantly.
    """
    gc = geonamescache.GeonamesCache(min_city_population=500)

    # --- US states ---
    us_states = gc.get_us_states()
    state_name_to_abbr: dict[str, str] = {v["name"].lower(): k for k, v in us_states.items()}
    abbr_set = frozenset(us_states.keys())

    # --- Cities (primary + alternate names, US only, pop >= 500) ---
    city_to_state_raw: dict[str, list[tuple[str, int]]] = {}
    for v in gc.get_cities().values():
        if v["countrycode"] != "US":
            continue
        state = v["admin1code"]
        pop   = v.get("population", 0)
        city_to_state_raw.setdefault(v["name"].lower(), []).append((state, pop))
        for alt in v.get("alternatenames", []):
            if alt and len(alt) > 2:
                city_to_state_raw.setdefault(alt.lower(), []).append((state, pop))

    # When a city name appears in multiple states, keep the most populous one.
    city_map: dict[str, str] = {
        name: max(entries, key=lambda x: x[1])[0]
        for name, entries in city_to_state_raw.items()
    }

    # --- Counties (full FIPS dataset, 3,235 entries) ---
    _COUNTY_SUFFIXES = (
        " county", " parish", " borough",
        " census area", " municipality", " city and borough",
    )
    county_multi: dict[str, list[str]] = {}
    for c in gc.get_us_counties():
        name = c["name"].lower()
        for suffix in _COUNTY_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        name = name.strip()
        county_multi.setdefault(name, []).append(c["state"])

    # Unambiguous = county name maps to exactly one state across the whole US.
    county_unambiguous: dict[str, str] = {
        k: v[0] for k, v in county_multi.items() if len(v) == 1
    }

    return state_name_to_abbr, abbr_set, city_map, county_unambiguous, county_multi


_RE_COUNTY_SUFFIX = re.compile(
    r"([A-Za-z .'\-]+?)\s+(?:County|Parish|Borough|Municipality)\b",
    re.IGNORECASE,
)
_RE_STATE_US = re.compile(
    r"([A-Za-z ]+),\s*(?:US|USA|U\.S\.A?\.?)\b",
    re.IGNORECASE,
)


def extract_state(location: str, description: str = "") -> Optional[str]:
    """
    Return a 2-letter US state abbreviation or None.
    Resolution order (first match wins):
        1. 2-letter abbreviation token in location string
        2. Full state name in location string
        3. "State, US/USA" pattern in location string
        4. Unambiguous county name in location string
        5. City / alternate city name in location string
        6. Ambiguous county + city context for disambiguation
        7. Full state name in first 2,000 chars of description
        8. 2-letter abbreviation in first 500 chars of description (last resort)
    """
    state_name_to_abbr, abbr_set, city_map, county_unambiguous, county_multi = (
        _build_geo_lookups()
    )

    loc  = location or ""
    desc = description or ""

    # ── 1. 2-letter abbreviation token ─────────────────────────────────────
    for token in re.split(r"[,/|;\s]+", loc):
        t = token.strip().upper()
        if t in abbr_set:
            return t

    # ── 2. Full state name ─────────────────────────────────────────────────
    loc_norm = _normalize(loc)
    for name, abbr in state_name_to_abbr.items():
        if re.search(r"\b" + re.escape(name) + r"\b", loc_norm):
            return abbr

    # ── 3. "State, US/USA" pattern ────────────────────────────────────────
    m = _RE_STATE_US.search(loc)
    if m:
        candidate = m.group(1).strip().lower()
        if candidate in state_name_to_abbr:
            return state_name_to_abbr[candidate]

    # ── 4. Unambiguous county name ────────────────────────────────────────
    for m in _RE_COUNTY_SUFFIX.finditer(loc):
        county = _normalize(m.group(1))
        if county in county_unambiguous:
            return county_unambiguous[county]

    # ── 5. City name lookup (each comma-segment + full string) ────────────
    segments = [s.strip() for s in loc_norm.split(",")]
    for seg in [loc_norm] + segments:
        seg = seg.strip()
        if seg and seg in city_map:
            return city_map[seg]

    # ── 6. Ambiguous county + city disambiguation ──────────────────────────
    for m in _RE_COUNTY_SUFFIX.finditer(loc):
        county = _normalize(m.group(1))
        if county in county_multi:
            candidates = set(county_multi[county])
            for seg in segments:
                seg = seg.strip()
                if seg and seg in city_map and city_map[seg] in candidates:
                    return city_map[seg]
            if len(candidates) == 1:
                return next(iter(candidates))

    # ── 7. State name in description ───────────────────────────────────────
    if desc:
        desc_norm = _normalize(desc[:2000])
        for name, abbr in state_name_to_abbr.items():
            if re.search(r"\b" + re.escape(name) + r"\b", desc_norm):
                return abbr

    # ── 8. Abbreviation in description (last resort) ────────────────────────
    if desc:
        abbr_pattern = re.compile(
            r"(?<![A-Za-z])("
            + "|".join(sorted(abbr_set, key=len, reverse=True))
            + r")(?![A-Za-z])"
        )
        m = abbr_pattern.search(desc[:500])
        if m:
            return m.group(1).upper()

    return None


# ──────────────────────────────────────────────────────────────────────────────
# SKILLS EXTRACTION (role-specific: Analyst / Data Scientist)
# ──────────────────────────────────────────────────────────────────────────────

_SKILLS_RAW: list[tuple[str, list[str]]] = [
    # ── Core Languages ────────────────────────────────────────────────────
    ("Python",          [r"python"]),
    ("SQL",             [r"\bsql\b", r"structured\s+query\s+language"]),
    ("DAX",             [r"\bdax\b"]),
    ("M (Power Query)", [r"power\s*query\b", r"\bm\s+language\b"]),
    ("VBA",             [r"\bvba\b"]),
    ("MDX",             [r"\bmdx\b"]),
    ("Scala",           [r"\bscala\b"]),
    
    # ── Excel ─────────────────────────────────────────────────────────────
    ("Excel",           [r"\bexcel\b", r"microsoft\s+excel", r"ms\s+excel"]),
    ("VLOOKUP",         [r"\bv?hlookup\b", r"\bvlookup\b"]),
    ("INDEX MATCH",     [r"\bindex[\s/]+match\b"]),
    ("XLOOKUP",         [r"\bxlookup\b"]),
    ("Pivot Tables",    [r"pivot\s+table", r"pivottable"]),
    ("Advanced Excel",  [r"advanced\s+excel", r"excel\s+advanced"]),
    ("Conditional Formatting", [r"conditional\s+formatting"]),
    ("Macros",          [r"\bmacros?\b(?!\s*economics)"]),
    ("Power Pivot",     [r"power\s*pivot"]),
    ("Excel Dashboards",[r"excel\s+dashboard"]),
    
    # ── BI & Visualization ────────────────────────────────────────────────
    ("Tableau",         [r"tableau"]),
    ("Power BI",        [r"power[\s\-]?bi"]),
    ("Looker",          [r"\blooker\b(?!\s*studio)"]),
    ("Looker Studio",   [r"looker\s+studio", r"google\s+data\s+studio", r"data\s+studio"]),
    ("Qlik",            [r"qlik(?:view|sense|\.com)?"]),
    ("Metabase",        [r"metabase"]),
    ("Grafana",         [r"grafana"]),
    ("Superset",        [r"apache\s*superset", r"\bsuperset\b"]),
    ("Sisense",         [r"sisense"]),
    ("MicroStrategy",   [r"microstrategy"]),
    ("TIBCO Spotfire",  [r"spotfire"]),
    ("SAP BusinessObjects", [r"sap\s*bo\b", r"business\s*objects"]),
    ("Domo",            [r"\bdomo\b"]),
    ("ThoughtSpot",     [r"thoughtspot"]),
    
    # ── Databases & Querying ──────────────────────────────────────────────
    ("PostgreSQL",      [r"postgres(?:ql)?"]),
    ("MySQL",           [r"mysql"]),
    ("SQL Server",      [r"sql\s+server", r"\bmssql\b", r"\bt-sql\b", r"\btsql\b"]),
    ("Oracle DB",       [r"\boracle\b(?:\s*db|\s*database|\s*sql)?"]),
    ("SQLite",          [r"sqlite"]),
    ("BigQuery",        [r"big\s*query", r"\bgbq\b"]),
    ("Snowflake",       [r"snowflake"]),
    ("Redshift",        [r"redshift"]),
    ("Databricks",      [r"databricks"]),
    ("Teradata",        [r"teradata"]),
    ("Hive",            [r"\bhive\s*ql\b", r"\bhive\b"]),
    ("MongoDB",         [r"mongo(?:db)?"]),
    ("NoSQL",           [r"\bnosql\b"]),
    
    # ── Cloud Platforms ───────────────────────────────────────────────────
    ("AWS",             [r"\baws\b", r"amazon\s+web\s+services"]),
    ("Azure",           [r"microsoft\s+azure", r"\bazure\b"]),
    ("GCP",             [r"\bgcp\b", r"google\s+cloud"]),
    
    # ── ETL / Data Pipeline ───────────────────────────────────────────────
    ("dbt",             [r"\bdbt\b", r"data\s+build\s+tool"]),
    ("Airflow",         [r"\bairflow\b"]),
    ("Fivetran",        [r"fivetran"]),
    ("Stitch",          [r"\bstitch\b(?:\s*data)?"]),
    ("Informatica",     [r"informatica"]),
    ("Talend",          [r"\btalend\b"]),
    ("SSIS",            [r"\bssis\b"]),
    
    # ── Statistics & Analytics ────────────────────────────────────────────
    ("Statistics",      [r"\bstatistics\b", r"\bstatistical\s+analysis\b"]),
    ("Probability",     [r"\bprobability\b"]),
    ("A/B Testing",     [r"a\s*/\s*b\s+test", r"\bsplit\s+test", r"hypothesis\s+test"]),
    ("Regression Analysis", [r"(?:linear|logistic|multiple)\s+regression", r"regression\s+analysis"]),
    ("Time Series",     [r"time[\s\-]+series", r"\barima\b", r"\bsarima\b", r"\bprophet\b"]),
    ("Forecasting",     [r"\bforecasting\b", r"demand\s+forecast"]),
    ("Bayesian Analysis",[r"bayesian", r"\bbayes\b"]),
    ("Clustering",      [r"\bclustering\b", r"\bk[\s\-]?means\b"]),
    ("Cohort Analysis", [r"cohort\s+analysis"]),
    ("Funnel Analysis", [r"funnel\s+analysis"]),
    ("Causal Inference",[r"causal\s+inference", r"causal\s+analysis"]),
    ("Survival Analysis",[r"survival\s+analysis", r"churn\s+model"]),
    ("Multivariate Analysis",[r"multivariate\s+analysis", r"\bmanova\b", r"\banova\b"]),
    ("Monte Carlo",     [r"monte\s+carlo"]),
    ("Optimization",    [r"\boptimization\b", r"linear\s+programming"]),
    
    # ── Machine Learning & AI (Data Scientist) ────────────────────────────
    ("Machine Learning",[r"machine\s+learning", r"\bml\b(?=\s+model|\s+pipeline)"]),
    ("Deep Learning",   [r"deep\s+learning", r"neural\s+net(?:work)?"]),
    ("NLP",             [r"\bnlp\b", r"natural\s+language\s+processing"]),
    ("Computer Vision", [r"computer\s+vision", r"image\s+recognition"]),
    ("Generative AI",   [r"gen(?:erative)?\s*ai", r"\bllm\b", r"large\s+language\s+model", r"chatgpt"]),
    ("Feature Engineering",[r"feature\s+engineering", r"feature\s+selection"]),
    ("Model Validation",[r"model\s+validat", r"cross[\s\-]+validation"]),
    ("scikit-learn",    [r"scikit[\s\-]*learn", r"\bsklearn\b"]),
    ("TensorFlow",      [r"tensor\s*flow"]),
    ("PyTorch",         [r"py\s*torch"]),
    ("Keras",           [r"\bkeras\b"]),
    ("XGBoost",         [r"xgboost", r"xg\s*boost"]),
    ("LightGBM",        [r"light\s*gbm", r"lightgbm"]),
    ("MLflow",          [r"mlflow"]),
    
    # ── Python Libraries ──────────────────────────────────────────────────
    ("Pandas",          [r"\bpandas\b"]),
    ("NumPy",           [r"\bnumpy\b"]),
    ("Matplotlib",      [r"matplotlib"]),
    ("Seaborn",         [r"seaborn"]),
    ("Plotly",          [r"plotly"]),
    ("SciPy",           [r"scipy"]),
    ("Statsmodels",     [r"statsmodels"]),
    ("Jupyter",         [r"jupyter(?:\s+notebook|\s+lab)?"]),
    
    # ── R Ecosystem ───────────────────────────────────────────────────────
    ("RStudio",         [r"rstudio"]),
    ("ggplot2",         [r"ggplot2?"]),
    ("tidyverse",       [r"tidyverse", r"\bdplyr\b", r"\btidyr\b"]),
    ("Shiny",           [r"\bshiny\b(?:\s+app)?"]),
    
    # ── Marketing Analytics ───────────────────────────────────────────────
    ("Google Analytics",[r"google\s+analytics", r"\bga4\b"]),
    ("Adobe Analytics", [r"adobe\s+analytics", r"\bomniture\b"]),
    ("Google Ads",      [r"google\s+ads", r"google\s+adwords"]),
    ("Facebook Ads",    [r"facebook\s+ads", r"meta\s+ads"]),
    ("SEO",             [r"\bseo\b", r"search\s+engine\s+optimization"]),
    ("SEM",             [r"\bsem\b(?=\s+analyst|\s+specialist)", r"search\s+engine\s+marketing"]),
    ("Web Analytics",   [r"web\s+analytics", r"digital\s+analytics"]),
    ("Marketing Mix Modeling",[r"marketing\s+mix\s+model", r"\bmmm\b"]),
    ("Attribution Modeling",[r"attribution\s+model", r"multi[\s\-]?touch\s+attribution"]),
    ("Customer Segmentation",[r"customer\s+segmentation"]),
    ("Salesforce",      [r"salesforce", r"\bsfdc\b"]),
    ("HubSpot",         [r"hubspot"]),
    ("Marketo",         [r"marketo"]),
    ("Mailchimp",       [r"mailchimp"]),
    ("CRM",             [r"\bcrm\b", r"customer\s+relationship\s+management"]),
    
    # ── Product Analytics ──────────────────────────────────────────────────
    ("Mixpanel",        [r"mixpanel"]),
    ("Amplitude",       [r"amplitude"]),
    ("Pendo",           [r"\bpendo\b"]),
    ("Heap",            [r"\bheap\b(?:\s+analytics)?"]),
    ("FullStory",       [r"fullstory"]),
    ("Hotjar",          [r"hotjar"]),
    ("Segment",         [r"\bsegment\b(?:\s+cdp|\s+io)?"]),
    ("Customer Data Platform",[r"\bcdp\b", r"customer\s+data\s+platform"]),
    ("User Research",   [r"user\s+research", r"usability\s+test"]),
    ("Retention Analysis",[r"retention\s+analysis", r"retention\s+rate"]),
    ("DAU/MAU",         [r"\bdau\b", r"\bmau\b", r"\bwau\b"]),
    ("OKRs",            [r"\bokrs?\b", r"objectives\s+and\s+key\s+results"]),
    ("KPIs",            [r"\bkpis?\b", r"key\s+performance\s+indicator"]),
    
    # ── Financial Analytics ───────────────────────────────────────────────
    ("Financial Modeling",[r"financial\s+model", r"\bdcf\b"]),
    ("Financial Analysis",[r"financial\s+analysis"]),
    ("Valuation",       [r"\bvaluation\b", r"equity\s+valuation"]),
    ("FP&A",            [r"\bfp&a\b", r"financial\s+planning"]),
    ("Variance Analysis",[r"variance\s+analysis", r"budget\s+vs"]),
    ("P&L Management",  [r"p(?:rofit)?\s*(?:&|and)\s*l(?:oss)?", r"\bp&l\b"]),
    ("SAP",             [r"\bsap\b"]),
    ("Oracle Financials",[r"oracle\s+financials", r"oracle\s+erp"]),
    ("ERP",             [r"\berp\b", r"enterprise\s+resource\s+planning"]),
    ("QuickBooks",      [r"quickbooks"]),
    ("NetSuite",        [r"netsuite"]),
    ("Bloomberg",       [r"bloomberg"]),
    ("FactSet",         [r"factset"]),
    ("GAAP",            [r"\bgaap\b"]),
    ("IFRS",            [r"\bifrs\b"]),
    ("Risk Analysis",   [r"risk\s+analysis", r"risk\s+model"]),
    
    # ── Operations Analytics ───────────────────────────────────────────────
    ("Process Improvement",[r"process\s+improvement"]),
    ("Lean",            [r"\blean\b(?:\s+six\s+sigma)?"]),
    ("Six Sigma",       [r"six\s+sigma", r"\bdmaic\b"]),
    ("Supply Chain",    [r"supply\s+chain"]),
    ("Inventory Management",[r"inventory\s+management"]),
    ("Demand Planning", [r"demand\s+planning", r"demand\s+forecasting"]),
    ("Workforce Analytics",[r"workforce\s+analytics", r"hr\s+analytics"]),
    
    # ── Soft Skills & Methodologies ───────────────────────────────────────
    ("Agile",           [r"\bagile\b"]),
    ("Scrum",           [r"\bscrum\b"]),
    ("Data Storytelling",[r"data\s+storytelling", r"data\s+narrativ"]),
    ("Problem Solving", [r"problem[\s\-]+solving"]),
    ("Critical Thinking",[r"critical\s+thinking"]),
    ("Requirements Gathering",[r"requirements?\s+gathering"]),
    ("Stakeholder Management",[r"stakeholder\s+management"]),
    ("Project Management",[r"project\s+management", r"\bpmp\b"]),
    ("Jira",            [r"\bjira\b"]),
    ("Confluence",      [r"confluence"]),
    ("Data Governance", [r"data\s+governance", r"data\s+quality"]),
    ("Git",             [r"\bgit\b"]),
    ("GitHub",          [r"github"]),
]

# Compile all patterns (case-insensitive)
_R_PATTERN = re.compile(r"(?<!\w)R(?!\w)")   # strict case-sensitive

_SKILL_PATTERNS: list[tuple[str, re.Pattern]] = []
for _canonical, _patterns in _SKILLS_RAW:
    _combined = "|".join(f"(?:{p})" for p in _patterns)
    _SKILL_PATTERNS.append((_canonical, re.compile(_combined, re.IGNORECASE)))


# ──────────────────────────────────────────────────────────────────────────────
# REMOTE STATUS EXTRACTION (weighted scoring model)
# ──────────────────────────────────────────────────────────────────────────────

_REMOTE_SIGNALS: list[tuple[re.Pattern, int]] = [
    # Strong remote (+3)
    (re.compile(
        r"\b(?:fully?\s*remote|100\s*%\s*remote|remote[\s\-]*only|"
        r"entirely\s*remote|permanently\s*remote|remote[\s\-]*first)\b", re.I), +3),
    # Moderate remote (+2)
    (re.compile(
        r"\b(?:work(?:ing)?\s*(?:from|at)\s*home|wfh|telecommut(?:e|ing)|"
        r"distributed\s*team|location[\s\-]*independent|work\s*anywhere|"
        r"anywhere\s*in\s*(?:the\s*)?(?:us|usa|world)|no\s*office\s*required|"
        r"remote\s*work\s*(?:allowed|available|option|eligible))\b", re.I), +2),
    # General remote mention (+1)
    (re.compile(r"\bremote\b", re.I), +1),
    # Hybrid keyword (0, tracked separately)
    (re.compile(
        r"\b(?:hybrid|partial(?:ly)?\s*remote|remote[\s/\-]*hybrid|"
        r"flexible\s*work(?:ing)?|mix(?:ed)?\s*(?:of\s*)?(?:remote|office|on[\s\-]?site))\b",
        re.I), 0),
    # "X days in office per week" → explicit hybrid
    (re.compile(
        r"\b\d+\s*(?:days?\s*(?:per\s*week\s*)?(?:in[\s\-]?office|on[\s\-]?site|in\s*person)|"
        r"(?:in[\s\-]?office|on[\s\-]?site)\s*days?\s*per\s*week)\b", re.I), +2),
    # On-site (-2)
    (re.compile(
        r"\b(?:in[\s\-]person|on[\s\-]?site)\b|"
        r"\bin[\s\-]office\b(?!\s*day)(?!\s*\d)|"
        r"\b(?:must\s+(?:be\s+)?(?:located|based|reside|live)\s+(?:in|near|within)|"
        r"relocation\s+(?:required|assistance|package)|"
        r"authorized\s+to\s+work\s+in)\b|"
        r"(?:must\s+)?report\s+to\s+(?:(?:our|the)\s+)?\w+(?:[\s\-]\w+)?\s+office\b",
        re.I), -2),
    # Soft on-site (-1)
    (re.compile(
        r"\b(?:office[\s\-]based|our\s*(?:\w+\s+)*office|headquarters|hq|"
        r"commut(?:e|ing)|badge\s*access|parking\s*(?:provided|available)|"
        r"campus|building\s*access)\b", re.I), -1),
    # Job-type on-site (-2)
    (re.compile(
        r"\b(?:on[\s\-]?call|shift\s*work|night\s*shift|day\s*shift|"
        r"warehouse|field\s*(?:work|engineer|technician)|"
        r"travel\s*(?:required|up\s*to\s*\d))\b", re.I), -2),
]

_RE_HYBRID_EXPLICIT = re.compile(
    r"\b(?:hybrid|partial(?:ly)?\s*remote|flexible\s*work(?:ing)?)\b", re.I
)



# Global counter for debugging
JOBS_PROCESSED = 0


def load_environment() -> None:
    """Load environment variables from .env and set Google credentials."""
    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(dotenv_path=dotenv_path)

    credentials_path = os.path.join(os.path.dirname(__file__), "credentials.json")
    if os.path.isfile(credentials_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path





def _extract_api_remote_flag(api_dict: Optional[Dict[str, Any]]) -> Optional[bool]:
    """
    Extract a structured remote/telework signal from any API's raw JSON dict.

    Returns:
        True   – API explicitly indicates fully remote or telework eligible
        False  – API explicitly indicates NOT remote / on-site required
        None   – API has no structured remote metadata (fall through to regex)
    """
    if not api_dict or not isinstance(api_dict, dict):
        return None

    # ── Arbeitnow: "remote" boolean ──────────────────────────────────────────
    if "remote" in api_dict:
        val = api_dict["remote"]
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "1")

    # ── Remotive: injected by fetcher as _remote_confirmed=True ─────────────
    if api_dict.get("_remote_confirmed") is True:
        return True

    # ── USAJobs: TeleworkEligible in UserArea.Details ────────────────────────
    user_area_details = api_dict.get("UserArea", {}).get("Details", {})
    if user_area_details:
        telework = user_area_details.get("TeleworkEligible", "")
        if isinstance(telework, str) and telework.strip():
            tl = telework.strip().lower()
            if tl in ("yes", "true", "1", "situational telework",
                      "regular telework", "ad-hoc telework"):
                return True
            if tl in ("no", "false", "0", "not applicable", "not eligible"):
                return False

        remote_ind = user_area_details.get("RemoteIndicator", "")
        if isinstance(remote_ind, str) and remote_ind.strip():
            ri = remote_ind.strip().lower()
            if ri in ("yes", "true", "1"):
                return True
            if ri in ("no", "false", "0"):
                return False

    # ── The Muse: locations array containing "Remote" ─────────────────────────
    locations = api_dict.get("locations", [])
    if isinstance(locations, list) and locations:
        location_names = [
            str(loc.get("name", "")).lower()
            for loc in locations
            if isinstance(loc, dict)
        ]
        has_remote_location = any(
            "remote" in n or "flexible" in n or "anywhere" in n
            for n in location_names
        )
        has_physical_location = any(
            not ("remote" in n or "flexible" in n or "anywhere" in n)
            and n.strip() not in ("", "us", "usa", "united states")
            for n in location_names
        )
        if has_remote_location and not has_physical_location:
            return True
        if has_remote_location and has_physical_location:
            return None

    return None


def _extract_api_tags(api_dict: Optional[Dict[str, Any]]) -> List[str]:
    """
    Extract free-text skill/category tags from any API's raw JSON dict.
    Returns a list of raw tag strings (lowercased, deduplicated).
    """
    if not api_dict or not isinstance(api_dict, dict):
        return []

    raw: list[str] = []

    # ── The Muse: tags[].name ─────────────────────────────────────────────────
    tags_list = api_dict.get("tags", [])
    if isinstance(tags_list, list):
        for tag in tags_list:
            if isinstance(tag, dict):
                name = tag.get("name") or tag.get("value") or ""
                if name:
                    raw.append(str(name).strip())
            elif isinstance(tag, str) and tag.strip():
                raw.append(tag.strip())

    # ── USAJobs: JobCategory[].Name ───────────────────────────────────────────
    for cat in api_dict.get("JobCategory", []):
        if isinstance(cat, dict):
            name = cat.get("Name", "")
            if name:
                raw.append(str(name).strip())

    # ── USAJobs: UserArea.Details.MajorDuties (list of strings) ──────────────
    major_duties = api_dict.get("UserArea", {}).get("Details", {}).get("MajorDuties", [])
    if isinstance(major_duties, list):
        for duty in major_duties:
            if isinstance(duty, str) and duty.strip():
                raw.append(duty.strip())

    # ── Deduplicate (preserve insertion order, lowercase for comparison) ──────
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)

    return result


def _merge_remote_status(api_flag: Optional[bool], regex_result: str) -> str:
    """
    Combine a structured API remote flag with a regex-derived remote status.
    Returns the merged result using truth table logic.
    """
    if api_flag is None:
        return regex_result

    if api_flag is True:
        if regex_result == "Remote":
            return "Remote"
        if regex_result == "Hybrid":
            return "Hybrid"
        if regex_result == "On-site":
            return "Hybrid"
        return "Remote"

    # api_flag is False
    if regex_result == "Remote":
        return "Hybrid"
    if regex_result in ("Hybrid", "On-site"):
        return regex_result
    return "On-site"


def _merge_skill_tokens(regex_csv: Optional[str], api_tags: List[str]) -> Optional[str]:
    """
    Merge a comma-separated regex result with a list of raw API tag strings.
    Returns a sorted, comma-separated string or None.
    """
    regex_set: set[str] = set()
    if regex_csv:
        for item in regex_csv.split(","):
            item = item.strip()
            if item:
                regex_set.add(item)

    api_set: set[str] = set()
    if api_tags:
        tags_blob = " ".join(api_tags)
        api_csv = extract_skills(tags_blob)
        if api_csv:
            for item in api_csv.split(","):
                item = item.strip()
                if item:
                    api_set.add(item)

    merged = regex_set | api_set
    return ", ".join(sorted(merged)) if merged else None


def _merge_benefits_tokens(regex_csv: Optional[str], api_tags: List[str]) -> Optional[str]:
    """
    Merge regex-derived benefits with any benefit phrases found in API tags.
    Same strategy as _merge_skill_tokens but uses extract_benefits().
    """
    regex_set: set[str] = set()
    if regex_csv:
        for item in regex_csv.split(","):
            item = item.strip()
            if item:
                regex_set.add(item)

    api_set: set[str] = set()
    if api_tags:
        tags_blob = " ".join(api_tags)
        api_csv = extract_benefits(tags_blob)
        if api_csv:
            for item in api_csv.split(","):
                item = item.strip()
                if item:
                    api_set.add(item)

    merged = regex_set | api_set
    return ", ".join(sorted(merged)) if merged else None


def extract_remote_status(
    location: str,
    description: str,
    title: str = "",
) -> str:
    """
    Classify work arrangement using weighted scoring model (regex only).
    Returns: "Remote" | "Hybrid" | "On-site" | "Not Specified"
    
    Note: This is the regex-only scorer. For merged results with API metadata,
    use _merge_remote_status(api_flag, extract_remote_status(...)).
    """
    score        = 0
    hybrid_found = False
    
    # Check if 'remote' appears in title (heavily weighted)
    if re.search(r"\bremote\b", title, re.IGNORECASE):
        score += 5
    
    # Build corpus for pattern matching (location and description)
    corpus = f"{location} {description}"

    for pattern, weight in _REMOTE_SIGNALS:
        if pattern.search(corpus):
            score += weight
            if weight == 0:
                hybrid_found = True

    if _RE_HYBRID_EXPLICIT.search(corpus):
        hybrid_found = True

    if score >= 2:
        return "Remote"
    elif score <= -2:
        return "On-site"
    elif hybrid_found:
        return "Hybrid"
    elif score == 1:
        return "Remote"
    else:
        return "Not Specified"





def parse_salary_range(salary_raw: Any) -> Tuple[Optional[float], Optional[float]]:
    """
    Professional-grade salary parser handling:
    - Commas in numbers (100,000)
    - 'k' or 'K' suffix (multiply by 1000)
    - Hourly rates (/hr or per hour, multiply by 2080 for annual)
    - Ranges (80k-100k)
    - Various formats
    """
    if not salary_raw:
        return None, None
    
    try:
        salary_text = str(salary_raw).lower().strip()
        
        # Handle dictionary format (from APIs)
        if isinstance(salary_raw, dict):
            salary_min = salary_raw.get("salary_min")
            salary_max = salary_raw.get("salary_max")
            
            min_val = None
            max_val = None
            
            if salary_min:
                try:
                    min_val = float(str(salary_min).replace(",", ""))
                except (ValueError, TypeError):
                    pass
            
            if salary_max:
                try:
                    max_val = float(str(salary_max).replace(",", ""))
                except (ValueError, TypeError):
                    pass
            
            # Apply k multiplier if needed
            if min_val and min_val < 1000 and "k" in str(salary_raw).lower():
                min_val *= 1000
            if max_val and max_val < 1000 and "k" in str(salary_raw).lower():
                max_val *= 1000
            
            return min_val, max_val
        
        # Check if hourly rate
        is_hourly = "/hr" in salary_text or "per hour" in salary_text or "hourly" in salary_text
        
        # Extract all numbers (with optional commas and decimals)
        numbers_raw = re.findall(r"[\d,]+\.?\d*", salary_text.replace(",", ""))
        
        if not numbers_raw:
            return None, None
        
        # Convert to floats
        numbers = []
        for num_str in numbers_raw:
            try:
                num = float(num_str)
                # Apply 'k' multiplier if number is small and followed by 'k' in original text
                if num < 10000 and "k" in salary_text:
                    # Check if this specific number had a 'k' after it
                    if re.search(rf"{re.escape(num_str)}\s*k", salary_text):
                        num *= 1000
                numbers.append(num)
            except ValueError:
                continue
        
        if not numbers:
            return None, None
        
        # Handle hourly: multiply by 2080 (standard work year: 40 hrs/week * 52 weeks)
        if is_hourly:
            numbers = [n * 2080 for n in numbers]
        
        # Sort and extract min/max
        numbers.sort()
        salary_min = numbers[0] if len(numbers) >= 1 else None
        salary_max = numbers[-1] if len(numbers) >= 2 else None
        
        return salary_min, salary_max
    
    except Exception:
        pass
    
    return None, None


def parse_relative_date(date_string: Optional[str], api_dict: Optional[Dict[str, Any]] = None, fallback_date: Optional[str] = None) -> str:
    """
    Ironclad date parsing with FORCED FALLBACK to today's date.
    
    Steps:
    1. Check api_dict for standard date keys (created_at, date, publication_date, etc.)
    2. Parse relative formats ("3 days ago", "today", "yesterday", "30+")
    3. Parse absolute date formats
    4. FORCED FALLBACK: If nothing works, return fallback_date (today's date)
    
    NEVER returns None. Always returns YYYY-MM-DD.
    """
    # First, try to extract from API dict if provided
    if api_dict and isinstance(api_dict, dict):
        date_candidates = [
            "created_at", "created", "date", "publication_date", "posted_date",
            "update_date", "published_at", "posted", "posted_on", "date_posted"
        ]
        for key in date_candidates:
            if key in api_dict and api_dict[key]:
                try:
                    parsed = parse_relative_date(str(api_dict[key]), None, fallback_date)
                    if parsed:
                        return parsed
                except Exception:
                    continue
    
    # If no date_string provided, use fallback immediately
    if not date_string or date_string == "":
        return fallback_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    date_string = str(date_string).lower().strip()
    
    try:
        # Handle immediate/current dates
        if "today" in date_string or "just now" in date_string or "just posted" in date_string:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        if "yesterday" in date_string:
            return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Handle relative date formats with "ago"
        if "ago" in date_string:
            parts = date_string.split()
            for i, part in enumerate(parts):
                # Extract number (handle "30+" format)
                num_str = part.rstrip("+").rstrip(".")
                if num_str.replace("-", "").isdigit():
                    num = int(num_str)
                    # Look for time unit in next part
                    if i + 1 < len(parts):
                        unit = parts[i + 1]
                        if "day" in unit:
                            target_date = datetime.now(timezone.utc) - timedelta(days=num)
                        elif "week" in unit:
                            target_date = datetime.now(timezone.utc) - timedelta(weeks=num)
                        elif "month" in unit:
                            target_date = datetime.now(timezone.utc) - timedelta(days=num * 30)
                        elif "hour" in unit:
                            target_date = datetime.now(timezone.utc) - timedelta(hours=num)
                        else:
                            continue
                        return target_date.strftime("%Y-%m-%d")
        
        # Handle patterns like "30+" without "ago"
        if "+" in date_string:
            num_str = date_string.split("+")[0].strip().rstrip(".")
            if num_str.isdigit():
                num = int(num_str)
                # Default to days
                target_date = datetime.now(timezone.utc) - timedelta(days=num)
                return target_date.strftime("%Y-%m-%d")
        
        # Try to parse as a standard date (without fuzzy first for accuracy)
        try:
            parsed_date = date_parser.parse(date_string, fuzzy=False, ignoretz=True)
            return parsed_date.strftime("%Y-%m-%d")
        except Exception:
            # Try with fuzzy parsing
            parsed_date = date_parser.parse(date_string, fuzzy=True, ignoretz=True)
            return parsed_date.strftime("%Y-%m-%d")
    
    except Exception:
        pass
    
    # FORCED FALLBACK: If we reach here, return today's date
    return fallback_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_bigquery_client() -> bigquery.Client:
    """Get BigQuery client using service account credentials from env or secret."""
    credentials_json = os.getenv("GOOGLE_CREDENTIALS")
    if credentials_json:
        try:
            creds_dict = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(creds_dict)
            return bigquery.Client(project=PROJECT_ID, credentials=credentials)
        except Exception as e:
            print(f"[BigQuery] Failed to load credentials from GOOGLE_CREDENTIALS: {e}")
    return bigquery.Client(project=PROJECT_ID)


def strict_inclusion_filter(job_title: Optional[str]) -> bool:
    """Check if job_title contains one of the target roles (case-insensitive)."""
    if not job_title:
        return False
    job_title_lower = job_title.lower()
    return any(role.lower() in job_title_lower for role in ROLES)


def strip_html(text: Optional[str]) -> Optional[str]:
    """Strip HTML tags from description text."""
    if not text:
        return text
    try:
        return BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()
    except Exception:
        return re.sub(r"<[^>]+>", "", text).strip()


def format_salary_raw(value: Any) -> Optional[str]:
    """Format salary as JSON string or keep as-is."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def get_date_retrieved() -> str:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_company_name(company: Any) -> Optional[str]:
    """Extract company name from various formats."""
    if isinstance(company, dict):
        return company.get("display_name") or company.get("name") or company.get("company_name")
    if isinstance(company, str):
        return company
    return None


def safe_date_posted(date_value: Any) -> Optional[str]:
    """Convert date to DATE string (YYYY-MM-DD) for BigQuery. Return None if invalid."""
    if date_value is None or date_value == "":
        return None
    try:
        parsed_date = pd.to_datetime(date_value)
        return parsed_date.strftime("%Y-%m-%d")
    except Exception:
        return None


def build_row(
    job_title,
    company,
    location,
    description,
    salary_raw,
    job_url,
    date_posted=None,
    api_dict=None,
):
    """
    Build a standardized job row for BigQuery (17 columns).
    Returns None if the job is filtered out.
    
    Changes vs. original:
        • Calls _is_us_location() as a post-fetch US guard
        • Calls _extract_api_remote_flag() + _merge_remote_status()
        • Calls _extract_api_tags() + _merge_skill_tokens() / _merge_benefits_tokens()
        • Uses the updated extract_state() that handles multi-location strings
    """
    global JOBS_PROCESSED

    # ── Role inclusion filter ─────────────────────────────────────────────────
    if not strict_inclusion_filter(job_title):
        return None

    # ── Location normalisation ────────────────────────────────────────────────
    location_str = location
    if isinstance(location, dict):
        location_str = location.get("display_name") or location.get("name")
    elif not isinstance(location, str):
        location_str = None

    # ── POST-FETCH US GUARD ───────────────────────────────────────────────────
    # Reject jobs whose location string clearly references a non-US country.
    if not _is_us_location(location_str):
        return None

    JOBS_PROCESSED += 1

    # ── Description cleaning ──────────────────────────────────────────────────
    cleaned_description = strip_html(description)

    # ── Geography ─────────────────────────────────────────────────────────────
    # extract_state now handles multi-location strings
    state = extract_state(location_str or "", cleaned_description or "")

    # ── Structured metadata from API ──────────────────────────────────────────
    api_remote_flag = _extract_api_remote_flag(api_dict)
    api_tags        = _extract_api_tags(api_dict)

    # ── Regex extraction ──────────────────────────────────────────────────────
    regex_skills   = extract_skills(cleaned_description)
    regex_benefits = extract_benefits(cleaned_description)
    regex_remote   = extract_remote_status(
        location_str or "", cleaned_description or "", job_title or ""
    )

    # ── MERGE ─────────────────────────────────────────────────────────────────
    # Remote: structured flag + regex truth table
    remote_status = _merge_remote_status(api_remote_flag, regex_remote)

    # Skills: union of regex hits and canonicalized API tags
    skills = _merge_skill_tokens(regex_skills, api_tags)

    # Benefits: union of regex hits and any benefit phrases in API tags
    benefits = _merge_benefits_tokens(regex_benefits, api_tags)

    # ── Remaining extractions ─────────────────────────────────────────────────
    education   = extract_education(cleaned_description)
    industry    = extract_industry(safe_company_name(company), cleaned_description or "")
    soft_skills = extract_soft_skills(cleaned_description)

    # ── Salary ───────────────────────────────────────────────────────────────
    salary_min, salary_max = parse_salary_range(salary_raw)

    # ── Dates ─────────────────────────────────────────────────────────────────
    today_date    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_posted_str = parse_relative_date(date_posted, api_dict, today_date)

    # ── Debug ─────────────────────────────────────────────────────────────────
    if JOBS_PROCESSED % 10 == 0:
        print(
            f"[DEBUG] Job #{JOBS_PROCESSED}: "
            f"${salary_min or 'N/A'} - ${salary_max or 'N/A'} | "
            f"State: {state or 'N/A'} | "
            f"Remote: {remote_status} (api={api_remote_flag}, regex={regex_remote}) | "
            f"Skills: {(skills or 'N/A')[:60]}"
        )

    return {
        "job_title":      job_title,
        "company":        safe_company_name(company),
        "industry":       industry,
        "location":       location_str,
        "city":           None,
        "state":          state,
        "description":    cleaned_description,
        "salary_min":     salary_min,
        "salary_max":     salary_max,
        "job_url":        job_url,
        "skills":         skills,
        "soft_skills":    soft_skills,
        "education":      education,
        "remote_status":  remote_status,
        "benefits":       benefits,
        "date_retrieved": get_date_retrieved(),
        "date_posted":    date_posted_str,
    }


def get_existing_urls(client: bigquery.Client) -> Set[str]:
    """Fetch all existing job_url values from BigQuery to avoid duplicates."""
    try:
        query = f"SELECT DISTINCT job_url FROM `{TABLE_ID}` WHERE job_url IS NOT NULL"
        results = client.query(query).result()
        return {row.job_url for row in results}
    except Exception as e:
        print(f"[Dedup] Error fetching existing URLs: {e}")
        return set()


def fetch_adzuna(role: str, existing_urls: Set[str]) -> List[Dict[str, Any]]:
    """Fetch jobs from Adzuna API for a specific role with pagination."""
    try:
        app_id = os.getenv("ADZUNA_APP_ID")
        app_key = os.getenv("ADZUNA_APP_KEY")
        if not app_id or not app_key:
            return []

        rows = []
        for page in range(1, 11):
            response = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/us/search/{page}",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": 100,
                    "what": role,
                    "full_description": 1,
                    "content-type": "full",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            page_results = data.get("results", [])
            if not page_results:
                break

            for item in page_results:
                location_value = None
                if isinstance(item.get("location"), dict):
                    location_value = item.get("location", {}).get("display_name")
                else:
                    location_value = item.get("location")

                job_url = item.get("redirect_url") or item.get("url")
                if job_url and job_url not in existing_urls:
                    row = build_row(
                        job_title=item.get("title"),
                        company=item.get("company"),
                        location=location_value,
                        description=item.get("description"),
                        salary_raw={
                            "salary_min": item.get("salary_min"),
                            "salary_max": item.get("salary_max"),
                            "salary_currency": item.get("salary_currency"),
                        },
                        job_url=job_url,
                        date_posted=item.get("posted_date"),
                        api_dict=item,
                    )
                    if row:
                        rows.append(row)
                        existing_urls.add(job_url)

            time.sleep(1)

        print(f"[Adzuna] {role}: {len(rows)} new jobs (pages 1-{min(10, page)}).")
        return rows
    except Exception as e:
        print(f"[Adzuna] {role}: Error: {e}")
        return []


def fetch_jooble(role: str, existing_urls: Set[str]) -> List[Dict[str, Any]]:
    """
    Fetch jobs from Jooble API for a specific role with pagination.
    
    US filter  : "location": "United States" already in payload.
                 Additionally, build_row calls _is_us_location() to catch
                 any international jobs Jooble slips through anyway.
    Tags       : Jooble standard response has no tags array.
    Remote flag: No structured flag; regex only.
    """
    try:
        api_key = os.getenv("JOOBLE_API_KEY")
        if not api_key:
            return []

        rows = []
        for page in range(1, 11):
            response = requests.post(
                f"https://jooble.org/api/{api_key}?full_description=true",
                json={
                    "keywords": role,
                    "location": "United States",   # enforce US at request level
                    "page": page,
                },
                timeout=30,
            )

            if response.status_code in [400, 403]:
                break

            response.raise_for_status()
            data = response.json()
            page_results = data.get("jobs", [])
            if not page_results:
                break

            for item in page_results:
                # ── Additional post-fetch US guard ──────────────────────────────
                # Jooble may still return international listings; reject them.
                job_location = item.get("location", "")
                if not _is_us_location(job_location):
                    continue

                job_url = item.get("url")
                if job_url and job_url not in existing_urls:
                    row = build_row(
                        job_title=item.get("title"),
                        company=item.get("company"),
                        location=job_location,
                        description=item.get("description"),
                        salary_raw=item.get("salary"),
                        job_url=job_url,
                        date_posted=item.get("update_date") or item.get("posted_date"),
                        api_dict=item,
                    )
                    if row:
                        rows.append(row)
                        existing_urls.add(job_url)

            time.sleep(1)

        print(f"[Jooble] {role}: {len(rows)} new jobs.")
        return rows
    except Exception as e:
        print(f"[Jooble] {role}: Error: {e}")
        return []


def fetch_usajobs(role: str, existing_urls: Set[str]) -> List[Dict[str, Any]]:
    """Fetch jobs from USA Jobs API for a specific role with pagination."""
    try:
        api_key = os.getenv("USAJOBS_API_KEY")
        email = os.getenv("USAJOBS_EMAIL")
        if not api_key or not email:
            return []

        rows = []
        for page in range(1, 11):
            response = requests.get(
                "https://data.usajobs.gov/api/search",
                params={
                    "ResultsPerPage": 100,
                    "Page": page,
                    "Keyword": role,
                },
                headers={
                    "User-Agent": email,
                    "Authorization-Key": api_key,
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            page_results = data.get("SearchResult", {}).get("SearchResultItems", [])
            if not page_results:
                break

            for item in page_results:
                descriptor = item.get("MatchedObjectDescriptor", {})
                location_values = []
                for location in descriptor.get("PositionLocation", []):
                    location_name = location.get("LocationName")
                    if location_name:
                        location_values.append(location_name)

                job_url = descriptor.get("PositionURI")
                if job_url and job_url not in existing_urls:
                    # Deep parse USAJobs UserArea.Details for comprehensive description
                    details = descriptor.get("UserArea", {}).get("Details", {})
                    description_parts = []
                    if details.get("JobSummary"):
                        description_parts.append(f"Job Summary:\n{details.get('JobSummary')}")
                    if details.get("MajorDuties"):
                        # MajorDuties may be a list
                        duties = details.get("MajorDuties")
                        if isinstance(duties, list):
                            description_parts.append(f"Major Duties:\n" + "\n".join(duties))
                        else:
                            description_parts.append(f"Major Duties:\n{duties}")
                    if details.get("Education"):
                        description_parts.append(f"Education:\n{details.get('Education')}")
                    if details.get("Requirements"):
                        description_parts.append(f"Requirements:\n{details.get('Requirements')}")
                    
                    # Concatenate all parts or fallback
                    full_description = "\n\n".join(description_parts) if description_parts else (
                        descriptor.get("QualificationSummary")
                        or descriptor.get("PositionSummary")
                    )
                    
                    row = build_row(
                        job_title=descriptor.get("PositionTitle"),
                        company=descriptor.get("OrganizationName"),
                        location=", ".join(location_values) if location_values else None,
                        description=full_description,
                        salary_raw=descriptor.get("PositionRemuneration"),
                        job_url=job_url,
                        date_posted=descriptor.get("PublicationStartDate"),
                        api_dict=descriptor,
                    )
                    if row:
                        rows.append(row)
                        existing_urls.add(job_url)

            time.sleep(1)

        print(f"[USAJobs] {role}: {len(rows)} new jobs (pages 1-{min(10, page)}).")
        return rows
    except Exception as e:
        print(f"[USAJobs] {role}: Error: {e}")
        return []


def fetch_themuse(role: str, existing_urls: Set[str]) -> List[Dict[str, Any]]:
    """
    Fetch jobs from The Muse API for a specific role with pagination.
    
    US filter  : Added "location": "USA" request parameter.
                 build_row also calls _is_us_location() as secondary guard.
    Remote flag: _extract_api_remote_flag() reads locations[].name for
                 "Remote" / "Flexible" entries.
    Tags       : _extract_api_tags() reads tags[].name – The Muse's rich
                 skill tag array (e.g. "Python", "SQL", "Product Analytics").
    """
    try:
        rows = []
        for page in range(0, 10):
            response = requests.get(
                "https://www.themuse.com/api/public/jobs",
                params={
                    "page": page,
                    "search_query": role,
                    "location": "USA",    # ← NEW: restrict to US listings
                    "descending": "true",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            page_results = data.get("results", [])
            if not page_results:
                break

            for item in page_results:
                locations = [
                    loc.get("name")
                    for loc in item.get("locations", [])
                    if loc.get("name")
                ]
                job_url = item.get("refs", {}).get("landing_page")

                if job_url and job_url not in existing_urls:
                    row = build_row(
                        job_title=item.get("name"),
                        company=item.get("company", {}).get("name"),
                        location=", ".join(locations) if locations else None,
                        description=item.get("contents"),
                        salary_raw=item.get("salary"),
                        job_url=job_url,
                        date_posted=item.get("published_at"),
                        # Full item passed so _extract_api_tags reads tags[].name
                        # and _extract_api_remote_flag reads locations[]
                        api_dict=item,
                    )
                    if row:
                        rows.append(row)
                        existing_urls.add(job_url)

            time.sleep(1)

        print(f"[The Muse] {role}: {len(rows)} new jobs.")
        return rows
    except Exception as e:
        print(f"[The Muse] {role}: Error: {e}")
        return []


# Locations that indicate a job is open to US-based candidates.
# Remotive's candidate_required_location values are free-text; this is an
# allow-list of common values we accept.
_REMOTIVE_US_ALLOW = re.compile(
    r"""
    \b(?:
        worldwide | anywhere | global | us(?:a)?
      | united\s+states
      | north\s+america
      | canada\s+or\s+us | us\s+or\s+canada
      | americas?
      | english[\s\-]speaking
      | cet[\s\-\+\d]*      # timezone refs that include US hours
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def fetch_remotive(role: str, existing_urls: Set[str]) -> List[Dict[str, Any]]:
    """
    Fetch jobs from Remotive API for a specific role with pagination.
    
    US filter  : Remotive is a remote-only board; no physical country filter
                 exists in the API.  We allow jobs where
                 candidate_required_location matches _REMOTIVE_US_ALLOW
                 (Worldwide, USA, North America, etc.) OR is empty/None.
                 Jobs restricted to "Europe", "UK", "Germany", etc. are
                 dropped via _is_us_location() + the allow-list check below.
    Remote flag: Every Remotive job is remote by definition.  We inject
                 _remote_confirmed=True into api_dict so
                 _extract_api_remote_flag() returns True.
    Tags       : _extract_api_tags() reads tags[] (list of strings).
    """
    try:
        rows   = []
        offset = 0
        for _ in range(10):
            response = requests.get(
                "https://remotive.com/api/remote-jobs",
                params={
                    "search": role,
                    "limit":  100,
                    "offset": offset,
                },
                timeout=30,
            )
            response.raise_for_status()
            data         = response.json()
            page_results = data.get("jobs", [])
            if not page_results:
                break

            for item in page_results:
                candidate_location = item.get("candidate_required_location") or ""

                # ── US allow-list filter ───────────────────────────────────────────
                # Accept: empty/None, "Worldwide", "USA", "North America", etc.
                # Reject: "Europe", "Germany", "UK", "APAC", etc.
                if candidate_location:
                    loc_lower = candidate_location.lower().strip()
                    # Explicit reject: known non-US-inclusive regions
                    if any(term in loc_lower for term in (
                        "europe", "germany", "uk", "united kingdom", "france",
                        "spain", "italy", "poland", "netherlands", "brazil",
                        "latam", "latin america", "apac", "asia", "africa",
                        "australia", "new zealand", "india", "emea",
                    )):
                        continue
                    # Accept only if it matches our allow-list OR is ambiguous
                    if not _REMOTIVE_US_ALLOW.search(candidate_location):
                        # Unknown region string – be conservative and skip
                        continue

                job_url = item.get("url")
                if job_url and job_url not in existing_urls:
                    # Inject remote confirmed flag for _extract_api_remote_flag
                    item["_remote_confirmed"] = True

                    row = build_row(
                        job_title=item.get("title"),
                        company=item.get("company_name"),
                        location=candidate_location or "Remote",
                        description=item.get("description"),
                        salary_raw=item.get("salary"),
                        job_url=job_url,
                        date_posted=item.get("publication_date"),
                        api_dict=item,
                    )
                    if row:
                        rows.append(row)
                        existing_urls.add(job_url)

            offset += 100
            time.sleep(1)

        print(f"[Remotive] {role}: {len(rows)} new jobs.")
        return rows
    except Exception as e:
        print(f"[Remotive] {role}: Error: {e}")
        return []


def fetch_arbeitnow(role: str, existing_urls: Set[str]) -> List[Dict[str, Any]]:
    """
    Fetch jobs from Arbeitnow API for a specific role with pagination.
    
    US filter  : Added "location": "United States" request parameter.
                 build_row also calls _is_us_location() as secondary guard
                 (Arbeitnow is EU-focused so leakage is common).
    Remote flag: item["remote"] boolean read by _extract_api_remote_flag().
    Tags       : _extract_api_tags() reads tags[] (list of strings).
    """
    try:
        rows = []
        for page in range(1, 11):
            response = requests.get(
                "https://www.arbeitnow.com/api/job-board-api",
                params={
                    "search": role,
                    "page": page,
                    "limit": 100,
                    "location": "United States",   # ← NEW: US filter at request level
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            page_results = data.get("data", [])
            if not page_results:
                break

            for item in page_results:
                # ── Secondary post-fetch US guard ────────────────────────────────
                # Arbeitnow may still return EU jobs; reject them.
                job_location = item.get("location", "")
                if not _is_us_location(job_location):
                    continue

                job_url = item.get("url")
                if job_url and job_url not in existing_urls:
                    row = build_row(
                        job_title=item.get("title"),
                        company=item.get("company_name"),
                        location=job_location,
                        description=item.get("description"),
                        salary_raw=item.get("salary"),
                        job_url=job_url,
                        date_posted=(
                            item.get("publication_date") or item.get("posted_date")
                        ),
                        # Full item passed so _extract_api_remote_flag reads
                        # item["remote"] and _extract_api_tags reads item["tags"]
                        api_dict=item,
                    )
                    if row:
                        rows.append(row)
                        existing_urls.add(job_url)

            time.sleep(1)

        print(f"[Arbeitnow] {role}: {len(rows)} new jobs.")
        return rows
    except Exception as e:
        print(f"[Arbeitnow] {role}: Error: {e}")
        return []
        return []


def ensure_table_exists(client: bigquery.Client) -> None:
    """Create BigQuery table with the 17-column Smart ETL schema if it does not exist."""
    schema = [
        bigquery.SchemaField("job_title", "STRING"),
        bigquery.SchemaField("company", "STRING"),
        bigquery.SchemaField("industry", "STRING"),
        bigquery.SchemaField("location", "STRING"),
        bigquery.SchemaField("city", "STRING"),
        bigquery.SchemaField("state", "STRING"),
        bigquery.SchemaField("description", "STRING"),
        bigquery.SchemaField("salary_min", "FLOAT64"),
        bigquery.SchemaField("salary_max", "FLOAT64"),
        bigquery.SchemaField("job_url", "STRING"),
        bigquery.SchemaField("skills", "STRING"),
        bigquery.SchemaField("soft_skills", "STRING"),
        bigquery.SchemaField("education", "STRING"),
        bigquery.SchemaField("remote_status", "STRING"),
        bigquery.SchemaField("benefits", "STRING"),
        bigquery.SchemaField("date_retrieved", "TIMESTAMP"),
        bigquery.SchemaField("date_posted", "DATE"),
    ]
    table = bigquery.Table(TABLE_ID, schema=schema)
    client.create_table(table, exists_ok=True)
    print(f"[BigQuery] Table {TABLE_ID} ensured with Smart ETL schema (17 columns).")


def load_rows_to_bigquery(rows: List[Dict[str, Any]], client: bigquery.Client) -> None:
    """Load job rows into BigQuery with the 17-column Smart ETL schema."""
    if not rows:
        print("[BigQuery] No new rows to load.")
        return

    try:
        job_config = bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("job_title", "STRING"),
                bigquery.SchemaField("company", "STRING"),
                bigquery.SchemaField("industry", "STRING"),
                bigquery.SchemaField("location", "STRING"),
                bigquery.SchemaField("city", "STRING"),
                bigquery.SchemaField("state", "STRING"),
                bigquery.SchemaField("description", "STRING"),
                bigquery.SchemaField("salary_min", "FLOAT64"),
                bigquery.SchemaField("salary_max", "FLOAT64"),
                bigquery.SchemaField("job_url", "STRING"),
                bigquery.SchemaField("skills", "STRING"),
                bigquery.SchemaField("soft_skills", "STRING"),
                bigquery.SchemaField("education", "STRING"),
                bigquery.SchemaField("remote_status", "STRING"),
                bigquery.SchemaField("benefits", "STRING"),
                bigquery.SchemaField("date_retrieved", "TIMESTAMP"),
                bigquery.SchemaField("date_posted", "DATE"),
            ],
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )

        load_job = client.load_table_from_json(rows, TABLE_ID, job_config=job_config)
        load_job.result()
        print(f"[BigQuery] Loaded {len(rows)} rows into {TABLE_ID}.")
    except Exception as e:
        print(f"[BigQuery] Error loading data: {e}")


def main() -> None:
    """Main pipeline: Smart ETL job data pipeline with NLP extraction."""
    global JOBS_PROCESSED
    JOBS_PROCESSED = 0  # Reset counter for this run
    
    load_environment()
    client = get_bigquery_client()
    ensure_table_exists(client)

    print("=" * 70)
    print("Starting Smart ETL Job Data Pipeline")
    print("=" * 70)
    print(f"Target roles: {', '.join(ROLES)}")
    print("\nSmart ETL Features:")
    print("  • Salary parsing: min/max extraction with k-suffix & hourly conversion")
    print("  • Location parsing: city/state extraction with state code fallback")
    print("  • Skills detection: 140+ hard skills (R, Python, SQL, Excel, Power BI, Tableau, AWS, Azure, Spark, Snowflake, Looker, etc.)")
    print("  • Soft skills detection: 17 soft-skill categories (Communication, Leadership, Collaboration, Problem Solving, etc.)")
    print("  • Industry classification: 40+ industry mappings with keyword fallback")
    print("  • Education detection: Bachelor's, Master's, PhD")
    print("  • Remote status detection: Remote, Hybrid, On-site, Not Specified")
    print("  • Benefits detection: 401(k), Health Insurance, PTO, Bonus, Stock/Equity, Remote/Flexible, Learning & Dev, Commuter")
    print("  • Date parsing: 'today', 'yesterday', '3 days ago', '30+' → YYYY-MM-DD")
    print("  • Debugging: Sample debug output every 10 jobs")
    print("  • BigQuery schema: 17 columns (expanded from 15 with industry + soft_skills)")
    print()

    existing_urls = get_existing_urls(client)
    print(f"[Dedup] Found {len(existing_urls)} existing URLs in BigQuery.")

    all_rows: List[Dict[str, Any]] = []

    for role in ROLES:
        print(f"\n--- Fetching '{role}' ---")
        all_rows.extend(fetch_adzuna(role, existing_urls))
        all_rows.extend(fetch_jooble(role, existing_urls))
        all_rows.extend(fetch_usajobs(role, existing_urls))
        all_rows.extend(fetch_themuse(role, existing_urls))
        all_rows.extend(fetch_remotive(role, existing_urls))
        all_rows.extend(fetch_arbeitnow(role, existing_urls))

    print(f"\n{'=' * 70}")
    print(f"Total new rows collected: {len(all_rows)}")
    print(f"{'=' * 70}")
    load_rows_to_bigquery(all_rows, client)
    print("Smart ETL Pipeline complete.")


if __name__ == "__main__":
    main()
