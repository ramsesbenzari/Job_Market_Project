import os
import time
import httpx
from dotenv import load_dotenv

load_dotenv()

HEADERS = {
    "x-rapidapi-key": os.getenv("RAPIDAPI_KEY"),
    "x-rapidapi-host": "jsearch.p.rapidapi.com",
}

QUERIES = [
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


def fetch_page(client, q, page, retries=2):
    """Fetch one page with retry on timeout. Returns list of jobs (may be empty)."""
    params = {"query": q, "page": str(page), "date_posted": "week", "country": "us"}
    for attempt in range(retries + 1):
        try:
            r = client.get("https://jsearch.p.rapidapi.com/search",
                           headers=HEADERS, params=params, timeout=90.0)
            if r.status_code == 200:
                return r.json().get("data", [])
            else:
                print(f"   [{q} p{page}] status {r.status_code}")
                return []
        except Exception as e:
            if attempt < retries:
                print(f"   [{q} p{page}] timeout, retrying ({attempt+1})...")
                time.sleep(2.0)
            else:
                print(f"   [{q} p{page}] failed after retries: {e}")
                return []
    return []


def harvest_cumulative(max_pages):
    """Harvest up to max_pages, return unique job_uids and raw count."""
    unique_ids = set()
    raw_count = 0
    calls = 0
    with httpx.Client() as client:
        for q in QUERIES:
            for page in range(1, max_pages + 1):
                data = fetch_page(client, q, page)
                calls += 1
                if not data:
                    break  # no more results for this query, stop paging it
                for job in data:
                    raw_count += 1
                    uid = job.get("job_uid") or job.get("job_id")
                    if uid:
                        unique_ids.add(uid)
                time.sleep(1.2)
    return unique_ids, raw_count, calls


def main():
    print("Supply test: unique-job yield at increasing page depths\n")
    prev_unique = 0
    for depth in [4, 6, 8]:
        print(f"--- {depth} pages per query ---")
        ids, raw, calls = harvest_cumulative(depth)
        u = len(ids)
        gained = u - prev_unique
        dup_pct = (1 - u / raw) * 100 if raw else 0
        print(f"  API calls:        {calls}")
        print(f"  Raw jobs:         {raw}")
        print(f"  UNIQUE jobs:      {u}")
        print(f"  Duplicate rate:   {dup_pct:.0f}%")
        if prev_unique:
            print(f"  NEW unique vs previous depth: +{gained}")
        print()
        prev_unique = u


if __name__ == "__main__":
    main()