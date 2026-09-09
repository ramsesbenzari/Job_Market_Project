import os
import re
import time
import logging
import httpx
import json
import base64
from urllib.parse import urlparse
from google.cloud import bigquery
from google.oauth2 import service_account
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("cloud_pipeline")

PROJECT_ID = os.getenv("GCP_PROJECT_ID")
DATASET_ID = "data_job_market"
TABLE_NAME = "us_job_data"
TABLE_ID = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_NAME}"

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
JSEARCH_HEADERS = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": "jsearch.p.rapidapi.com",
}

# JSearch v5 (2026): /search became /search-v2, page-by-page looping was replaced
# by a single call with num_pages, results moved from data[] to data.jobs[], and
# remote_jobs_only was renamed work_from_home.
JSEARCH_SEARCH_URL = "https://jsearch.p.rapidapi.com/search-v2"
JSEARCH_DETAILS_URL = "https://jsearch.p.rapidapi.com/job-details"
SEARCH_NUM_PAGES = "6"
# CATCH-UP RUN: set to "week" to backfill the gap. Change back to "3days"
# after the catch-up run succeeds — that is the steady-state value.
SEARCH_DATE_POSTED = "today"

try:
    sa_key_b64 = os.getenv("GCP_SA_KEY_B64")
    if sa_key_b64:
        raw_json_bytes = base64.b64decode(sa_key_b64)
        info = json.loads(raw_json_bytes.decode("utf-8"))
        credentials = service_account.Credentials.from_service_account_info(info)
        bq_client = bigquery.Client(project=PROJECT_ID, credentials=credentials)
    else:
        bq_client = bigquery.Client(project=PROJECT_ID)
    log.info("Authenticated BigQuery via Secure Base64 Token.")
except Exception as e:
    log.error(f"Failed to initialize BigQuery client: {e}")
    raise

ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


def is_us_job(job):
    """US-only guard. Strips punctuation/case so U.S.A., u-s-a, usa all match.
    Missing country -> False (fail closed: dashboard must be 100% US)."""
    raw = str(job.get("job_country") or "")
    norm = re.sub(r"[^A-Z]", "", raw.upper())
    return norm in {"US", "USA", "UNITEDSTATES", "UNITEDSTATESOFAMERICA"}


def calculate_completeness(job):
    score = 0
    for field in ["job_description", "job_salary", "job_highlights", "job_required_skills"]:
        if job.get(field):
            score += 1
    return score


def get_stable_id(job):
    return job.get("job_uid") or job.get("job_id")


def fetch_known_job_ids():
    try:
        query = f"""
        SELECT DISTINCT job_id
        FROM `{TABLE_ID}`
        WHERE job_id IS NOT NULL
        """
        result = bq_client.query(query).result()
        known = {row.job_id for row in result if row.job_id}
        log.info(f"Cross-run dedup: loaded {len(known)} known ids (all-time).")
        return known
    except Exception as e:
        log.warning(f"Cross-run dedup query failed (proceeding without dedup): {e}")
        return set()


def derive_source_api(job):
    publisher = job.get("job_publisher")
    if publisher:
        return publisher
    url = job.get("job_apply_link", "")
    if not url:
        return "Unknown"
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        domain_map = {
            "linkedin.com": "LinkedIn", "indeed.com": "Indeed",
            "ziprecruiter.com": "ZipRecruiter", "glassdoor.com": "Glassdoor",
            "monster.com": "Monster", "dice.com": "Dice",
            "simplyhired.com": "SimplyHired", "careerbuilder.com": "CareerBuilder",
            "lensa.com": "Lensa", "learn4good.com": "Learn4Good",
            "jobleads.com": "JobLeads", "bebee.com": "beBee",
            "talent.com": "Talent.com", "jooble.org": "Jooble",
            "whatjobs.com": "WhatJobs", "theladders.com": "TheLadders",
            "jobilize.com": "Jobilize", "tealhq.com": "Teal",
            "builtinnyc.com": "Built In NYC", "adzuna.com": "Adzuna",
            "dailyremote.com": "DailyRemote", "jobgether.com": "Jobgether",
            "snagajob.com": "Snagajob",
        }
        for known, clean in domain_map.items():
            if known in domain:
                return clean
        if domain.startswith("jobs.") or domain.startswith("careers."):
            return "Company Career Page"
        return domain
    except Exception:
        return "Unknown"


def jsearch_details(per_search_job_id):
    """Fetch JSearch native enrichment. Uses PER-SEARCH job_id (required by
    Job Details endpoint post-2026). Returns {} on failure -> Gemini fallback.
    v5 may return either a list or a single object under 'data'."""
    if not per_search_job_id:
        return {}
    params = {"job_id": per_search_job_id, "country": "us"}
    try:
        r = httpx.get(JSEARCH_DETAILS_URL,
                      headers=JSEARCH_HEADERS, params=params, timeout=30.0)
        if r.status_code == 200:
            data = r.json().get("data")
            if isinstance(data, list):
                return data[0] if data else {}
            if isinstance(data, dict):
                return data
    except Exception as e:
        log.warning(f"job-details failed for {per_search_job_id}: {e}")
    return {}


def map_work_arrangement(val):
    if not val:
        return None
    v = str(val).strip().lower()
    if "remote" in v:
        return "Remote"
    if "hybrid" in v:
        return "Hybrid"
    if "onsite" in v or "on-site" in v or "on site" in v:
        return "On-site"
    return None


def map_education_level(edu_required):
    if not isinstance(edu_required, dict):
        return None
    lvl = edu_required.get("level")
    if not lvl:
        return None
    v = str(lvl).strip().lower()
    if "phd" in v or "doctor" in v:
        return "Doctorate"
    if "master" in v:
        return "Master's Degree"
    if "bachelor" in v:
        return "Bachelor's Degree"
    if "associate" in v:
        return "Associate's Degree"
    if "high school" in v or v == "hs":
        return "High School"
    return None


def process_single_job_with_retry(job):
    time.sleep(4.5)  # ~13 RPM, under 15 RPM Flash Lite limit
    raw_description = job.get("job_description", "")
    if not raw_description:
        return None, "fallback_raw"

    upstream_hints = {
        "employer_name": job.get("employer_name"),
        "job_title": job.get("job_title"),
        "job_city": job.get("job_city"),
        "job_state": job.get("job_state"),
        "job_country": job.get("job_country"),
        "job_employment_type": job.get("job_employment_type"),
        "job_min_salary": job.get("job_min_salary"),
        "job_max_salary": job.get("job_max_salary"),
        "job_is_remote": job.get("job_is_remote"),
        "job_posted_at_datetime_utc": job.get("job_posted_at_datetime_utc"),
        "job_highlights": job.get("job_highlights"),
        "employer_company_type": job.get("employer_company_type"),
    }

    prompt = f"""You are a data analyst assistant. Analyze the following job posting and return ALL fields as JSON.
Fill every field with your best inference from the description and the upstream hints. Only use null for truly unknowable values.

CRITICAL: If the UPSTREAM API STRUCTURED DATA contains job_min_salary or job_max_salary, you MUST use those exact numbers - do not return null for salary if upstream provides them.

UPSTREAM API STRUCTURED DATA (use as source of truth where available):
{json.dumps(upstream_hints, default=str)}

JOB DESCRIPTION:
{raw_description}

Return JSON with exactly these fields:
- job_title (string): clean canonical title
- company (string): employer name
- industry (string): infer from company + description, e.g. "Healthcare", "Finance", "Technology", "Retail"
- city (string): city name only
- state (string): full state name, e.g. "California", not "CA"
- description (string): a 1-2 sentence summary of the role's purpose
- salary_min (float or null): annual USD; use upstream value if available; convert hourly (x2080) or monthly (x12) if needed
- salary_max (float or null): annual USD; use upstream value if available; if only one figure given, use it for both
- tools (list of strings): software/platforms, e.g. ["Python", "SQL", "Tableau", "AWS"]
- hard_skills (list of strings): technical competencies, e.g. ["Data Modeling", "ETL", "Statistical Analysis"]
- soft_skills (list of strings): interpersonal/cognitive, e.g. ["Communication", "Problem Solving"]
- remote_status (string): one of "Remote", "On-site", "Hybrid", or "Unspecified"
- employment_type (string): one of "Full-time", "Part-time", "Contract", "Internship", or "Unspecified"
- benefits (list of strings): e.g. ["Health Insurance", "401k", "PTO", "Stock Options"]
- education (string): minimum required, e.g. "Bachelor's Degree", "Master's Degree", "High School", "Unspecified"
- date_posted (string): ISO date YYYY-MM-DD from the posted_at_datetime field, or today if missing
"""

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        structured_data = json.loads(response.text)
        if not isinstance(structured_data, dict):
            log.warning(f"Gemini returned non-dict for {get_stable_id(job)}, skipping.")
            return None, "fallback_raw"

        for list_field in ["tools", "hard_skills", "soft_skills", "benefits"]:
            if isinstance(structured_data.get(list_field), list):
                structured_data[list_field] = ", ".join(str(i) for i in structured_data[list_field])

        if structured_data.get("salary_min") is None and job.get("job_min_salary") is not None:
            try:
                structured_data["salary_min"] = float(job.get("job_min_salary"))
            except (ValueError, TypeError):
                pass
        if structured_data.get("salary_max") is None and job.get("job_max_salary") is not None:
            try:
                structured_data["salary_max"] = float(job.get("job_max_salary"))
            except (ValueError, TypeError):
                pass

        # HYBRID OVERLAY: JSearch native fields win where present (remote_status, education)
        details = jsearch_details(job.get("job_id"))  # per-search id
        if details:
            ra = map_work_arrangement(details.get("work_arrangement"))
            if ra:
                structured_data["remote_status"] = ra
            edu = map_education_level(details.get("education_required"))
            if edu:
                structured_data["education"] = edu

        structured_data["job_id"] = get_stable_id(job)
        structured_data["date_retrieved"] = time.strftime("%Y-%m-%d")
        structured_data["job_url"] = job.get("job_apply_link")
        structured_data["source_api"] = derive_source_api(job)

        return structured_data, "enriched"

    except Exception as gemini_err:
        if "429" in str(gemini_err) or "Quota" in str(gemini_err):
            log.warning("Gemini free rate limit spiked. Row shifted to raw fallback state.")
            return None, "exhausted_raw"
        log.warning(f"Failed to enrich {get_stable_id(job)}: {gemini_err}")
        return None, "fallback_raw"


def main():
    start_time = time.time()

    queries = [
        "Data Analyst in USA", "Business Analyst in USA",
        "Business Intelligence Analyst in USA", "BI Analyst in USA",
        "Data Scientist in USA", "Marketing Analyst in USA",
        "Finance Analyst in USA", "Healthcare Analyst in USA",
        "Operations Analyst in USA", "Product Analyst in USA",
        "Risk Analyst in USA", "Logistics Analyst in USA",
        "Supply Chain Analyst in USA",
    ]

    known_job_ids = fetch_known_job_ids()

    # In-run dedup: same job if stable id matches OR (company, title, city) matches.
    dedup_store = {}   # canonical key -> chosen job
    uid_index = {}     # stable id -> canonical key
    comp_index = {}    # (company, title, city) -> canonical key

    metrics = {"harvested": 0, "skipped_non_us": 0, "skipped_cross_run": 0,
               "processed": 0, "enriched": 0, "fallback_raw": 0,
               "exhausted_raw": 0, "loaded": 0}

    log.info("Launching hybrid daily cloud harvest pipeline (JSearch-details + Gemini)...")

    with httpx.Client() as client:
        for q in queries:
            try:
                params = {"query": q, "num_pages": SEARCH_NUM_PAGES,
                          "date_posted": SEARCH_DATE_POSTED,
                          "country": "us", "work_from_home": "false"}
                response = client.get(JSEARCH_SEARCH_URL,
                                      headers=JSEARCH_HEADERS, params=params, timeout=60.0)
                if response.status_code != 200:
                    log.warning(f"JSearch returned {response.status_code} on '{q}'")
                else:
                    payload = response.json().get("data") or {}
                    # v5 nests results under data.jobs; tolerate a bare list too.
                    job_data_list = payload.get("jobs", []) if isinstance(payload, dict) else payload
                    for job in job_data_list:
                        metrics["harvested"] += 1
                        # US-ONLY GUARD: JSearch's country param is a hint, not a
                        # filter. Verify job_country ourselves before anything else.
                        if not is_us_job(job):
                            metrics["skipped_non_us"] += 1
                            continue
                        uid = get_stable_id(job)
                        if uid in known_job_ids:
                            metrics["skipped_cross_run"] += 1
                            continue
                        company_clean = str(job.get("employer_name", "")).strip().lower()
                        title_clean = str(job.get("job_title", "")).strip().lower()
                        city_clean = str(job.get("job_city", "")).strip().lower()
                        comp = (company_clean, title_clean, city_clean)
                        existing = (uid_index.get(uid) if uid else None) or comp_index.get(comp)
                        if existing is None:
                            canonical = uid if uid else comp
                            dedup_store[canonical] = job
                            if uid:
                                uid_index[uid] = canonical
                            comp_index[comp] = canonical
                        else:
                            if calculate_completeness(job) > calculate_completeness(dedup_store[existing]):
                                dedup_store[existing] = job
                            if uid:
                                uid_index[uid] = existing
                            comp_index[comp] = existing
                time.sleep(1.2)
            except Exception as e:
                log.warning(f"API download anomaly on '{q}': {e}")

    job_list = list(dedup_store.values())
    total_to_process = len(job_list)
    log.info(f"Dedup complete. Processing {total_to_process} unique net-new items...")

    buffer = []
    consecutive_exhaustions = 0
    for job in job_list:
        metrics["processed"] += 1
        row, status = process_single_job_with_retry(job)
        metrics[status] += 1
        if row:
            buffer.append(row)
        consecutive_exhaustions = consecutive_exhaustions + 1 if status == "exhausted_raw" else 0
        if consecutive_exhaustions >= 3:
            log.critical("Consecutive rate limits. Aborting loop early to safeguard data.")
            break

    if buffer:
        backup_filename = f"enriched_backup_{int(time.time())}.json"
        try:
            with open(backup_filename, "w") as f:
                json.dump(buffer, f, indent=2, default=str)
            log.info(f"Backup written: {backup_filename} ({len(buffer)} rows)")
        except Exception as backup_err:
            log.error(f"Failed to write backup file: {backup_err}")

    if buffer:
        chunk_size = 50
        for i in range(0, len(buffer), chunk_size):
            chunk = buffer[i:i + chunk_size]
            try:
                job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
                load_job = bq_client.load_table_from_json(chunk, TABLE_ID, job_config=job_config)
                load_job.result()
                metrics["loaded"] += len(chunk)
            except Exception as bq_err:
                log.error(f"BigQuery rejection on batch {i}-{i+chunk_size}: {bq_err}")

    elapsed = int(time.time() - start_time)
    summary = (
        f"METRICS_SUMMARY: harvested={metrics['harvested']}, "
        f"skipped_non_us={metrics['skipped_non_us']}, "
        f"skipped_cross_run={metrics['skipped_cross_run']}, "
        f"unique={total_to_process}, enriched={metrics['enriched']}, "
        f"fallback={metrics['fallback_raw']}, exhausted={metrics['exhausted_raw']}, "
        f"loaded={metrics['loaded']}, runtime_seconds={elapsed}"
    )
    print(summary)
    log.info(summary)


if __name__ == "__main__":
    main()