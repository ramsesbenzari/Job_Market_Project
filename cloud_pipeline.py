import os
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

# Initialize Environment and Logging
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("cloud_pipeline")

# Infrastructure Constants
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
DATASET_ID = "data_job_market"
TABLE_NAME = "us_job_data"
TABLE_ID = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_NAME}"

# Initialize Google Cloud Clients with bulletproof Base64 Decoding
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

# Initialize Gemini Client
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


def calculate_completeness(job):
    score = 0
    fields_to_check = ["job_description", "job_salary", "job_highlights", "job_required_skills"]
    for field in fields_to_check:
        if job.get(field):
            score += 1
    return score


def get_stable_id(job):
    """Return the STABLE, globally-unique job identifier.

    RapidAPI/JSearch change (2026): the `job_id` field is now longer and
    regenerated on every Search call, so it is NO LONGER stable across runs.
    The constant, globally-unique value now lives in `job_uid` (it holds the
    exact value `job_id` held previously). We key dedup on `job_uid`, falling
    back to `job_id` only if `job_uid` is missing on a record.
    """
    return job.get("job_uid") or job.get("job_id")


def fetch_known_job_ids():
    """Query BigQuery for stable job ids from the last 30 days to enable
    cross-run deduplication. Returns a set of id strings already in the table.

    Note: the `job_id` COLUMN in BigQuery now stores stable job_uid values
    (see get_stable_id). Older rows may hold legacy ids; per JSearch, job_uid
    equals the previous job_id value, so old and new remain compatible.

    Failure mode: if the query fails, returns an empty set and the pipeline
    proceeds without dedup. Logged as a warning but not fatal.
    """
    try:
        query = f"""
        SELECT DISTINCT job_id 
        FROM `{TABLE_ID}` 
        WHERE job_id IS NOT NULL 
          AND date_retrieved >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
        """
        result = bq_client.query(query).result()
        known_ids = {row.job_id for row in result if row.job_id}
        log.info(f"Cross-run dedup: loaded {len(known_ids)} known ids from last 30 days.")
        return known_ids
    except Exception as e:
        log.warning(f"Cross-run dedup query failed (proceeding without dedup): {e}")
        return set()


def derive_source_api(job):
    """Determine which job board this listing came from.
    Prefer JSearch's job_publisher field, fall back to URL domain parsing."""
    
    publisher = job.get("job_publisher")
    if publisher:
        return publisher
    
    url = job.get("job_apply_link", "")
    if not url:
        return "Unknown"
    
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        
        domain_map = {
            "linkedin.com": "LinkedIn",
            "indeed.com": "Indeed",
            "ziprecruiter.com": "ZipRecruiter",
            "glassdoor.com": "Glassdoor",
            "monster.com": "Monster",
            "dice.com": "Dice",
            "simplyhired.com": "SimplyHired",
            "careerbuilder.com": "CareerBuilder",
            "lensa.com": "Lensa",
            "learn4good.com": "Learn4Good",
            "jobleads.com": "JobLeads",
            "bebee.com": "beBee",
            "talent.com": "Talent.com",
            "jooble.org": "Jooble",
            "whatjobs.com": "WhatJobs",
            "theladders.com": "TheLadders",
            "jobilize.com": "Jobilize",
            "tealhq.com": "Teal",
            "builtinnyc.com": "Built In NYC",
            "adzuna.com": "Adzuna",
            "dailyremote.com": "DailyRemote",
            "jobgether.com": "Jobgether",
            "snagajob.com": "Snagajob",
        }
        
        for known, clean_name in domain_map.items():
            if known in domain:
                return clean_name
        
        if domain.startswith("jobs.") or domain.startswith("careers."):
            return "Company Career Page"
        
        return domain
        
    except Exception:
        return "Unknown"


def process_single_job_with_retry(job):
    # 4.5s throttle = ~13 RPM theoretical, safely under the 15 RPM limit for Flash Lite 3.1
    time.sleep(4.5)
    
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
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        structured_data = json.loads(response.text)
        
        if not isinstance(structured_data, dict):
            log.warning(f"Gemini returned non-dict for job {get_stable_id(job)}, skipping.")
            return None, "fallback_raw"
        
        for list_field in ["tools", "hard_skills", "soft_skills", "benefits"]:
            if isinstance(structured_data.get(list_field), list):
                structured_data[list_field] = ", ".join(str(item) for item in structured_data[list_field])
        
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
        
        # STABLE ID FIX: store job_uid (stable) in the job_id column, not the
        # now-unstable per-search job_id. Fallback preserves old behavior if missing.
        structured_data["job_id"] = get_stable_id(job)
        structured_data["date_retrieved"] = time.strftime("%Y-%m-%d")
        structured_data["job_url"] = job.get("job_apply_link")
        structured_data["source_api"] = derive_source_api(job)
        
        return structured_data, "enriched"
        
    except Exception as gemini_err:
        if "429" in str(gemini_err) or "Quota" in str(gemini_err):
            log.warning("Gemini API Free Rate Limit spiked. Shifting row into raw fallback state.")
            return None, "exhausted_raw"
        
        log.warning(f"Failed to enrich job {get_stable_id(job)}: {gemini_err}")
        return None, "fallback_raw"


def main():
    start_time = time.time()
    
    queries = [
        "Data Analyst in USA",
        "Business Analyst in USA",
        "Business Intelligence Analyst in USA",
        "BI Analyst in USA",
        "Data Scientist in USA",
        "Marketing Analyst in USA",
        "Finance Analyst in USA",
        "Healthcare Analyst in USA",
        "Operations Analyst in USA",
        "Product Analyst in USA",
        "Risk Analyst in USA",
        "Logistics Analyst in USA",
        "Supply Chain Analyst in USA",
    ]
    
    headers = {
        "x-rapidapi-key": os.getenv("RAPIDAPI_KEY"),
        "x-rapidapi-host": "jsearch.p.rapidapi.com"
    }

    # CROSS-RUN DEDUP: load known stable ids from last 30 days BEFORE harvesting
    known_job_ids = fetch_known_job_ids()
    
    deduplicated_jobs = {}
    metrics = {
        "harvested": 0, 
        "skipped_cross_run": 0,
        "processed": 0, 
        "enriched": 0, 
        "fallback_raw": 0, 
        "exhausted_raw": 0, 
        "loaded": 0
    }

    log.info("Launching production-grade fortified daily cloud harvest pipeline...")

    with httpx.Client() as client:
        for q in queries:
            # 4 pages per query - balances coverage with Gemini's 500 RPD cap
            for page_num in range(1, 5):
                try:
                    params = {
                        "query": q, 
                        "page": str(page_num), 
                        "date_posted": "today", 
                        "country": "us", 
                        "remote_jobs_only": "false"
                    }
                    response = client.get("https://jsearch.p.rapidapi.com/search", headers=headers, params=params, timeout=30.0)
                    
                    if response.status_code == 200:
                        job_data_list = response.json().get("data", [])
                        if not job_data_list:
                            break
                            
                        for job in job_data_list:
                            metrics["harvested"] += 1
                            
                            # CROSS-RUN DEDUP: skip jobs already in BigQuery from previous runs.
                            # Compare on the STABLE id (job_uid) to match what we store.
                            if get_stable_id(job) in known_job_ids:
                                metrics["skipped_cross_run"] += 1
                                continue
                            
                            company_clean = str(job.get("employer_name", "")).strip().lower()
                            title_clean = str(job.get("job_title", "")).strip().lower()
                            city_clean = str(job.get("job_city", "")).strip().lower()
                            dedup_tuple = (company_clean, title_clean, city_clean)

                            if dedup_tuple not in deduplicated_jobs:
                                deduplicated_jobs[dedup_tuple] = job
                            else:
                                current_score = calculate_completeness(deduplicated_jobs[dedup_tuple])
                                incoming_score = calculate_completeness(job)
                                if incoming_score > current_score:
                                    deduplicated_jobs[dedup_tuple] = job
                    
                    time.sleep(1.2)
                    
                except Exception as e:
                    log.warning(f"API download path anomaly on track '{q}' Page {page_num}: {e}")
                    break

    job_list = list(deduplicated_jobs.values())
    total_to_process = len(job_list)
    log.info(f"Deduplication step completed. Processing {total_to_process} unique items sequentially...")

    buffer = []
    consecutive_exhaustions = 0
    
    for job in job_list:
        metrics["processed"] += 1
        processed_row, status = process_single_job_with_retry(job)
        
        metrics[status] += 1
        if processed_row:
            buffer.append(processed_row)
            
        if status == "exhausted_raw":
            consecutive_exhaustions += 1
        else:
            consecutive_exhaustions = 0

        if consecutive_exhaustions >= 3:
            log.critical("Consecutive rate limits exhausted. Aborting loop processing early to safeguard data logs.")
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
                log.error(f"BigQuery validation rejection on batch window {i}-{i+chunk_size}: {bq_err}")

    elapsed = int(time.time() - start_time)
    summary_string = (
        f"METRICS_SUMMARY: harvested={metrics['harvested']}, "
        f"skipped_cross_run={metrics['skipped_cross_run']}, "
        f"unique={total_to_process}, "
        f"enriched={metrics['enriched']}, "
        f"fallback={metrics['fallback_raw']}, "
        f"exhausted={metrics['exhausted_raw']}, "
        f"loaded={metrics['loaded']}, "
        f"runtime_seconds={elapsed}"
    )
    print(summary_string)
    log.info(summary_string)


if __name__ == "__main__":
    main()