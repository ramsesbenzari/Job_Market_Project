import os
import time
import json
import httpx
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY_TEST")  # separate test key — pipeline key untouched

HEADERS = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": "jsearch.p.rapidapi.com",
}

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Spread across varied roles so neither enricher gets "lucky" on one easy category
QUERIES = [
    "Data Analyst in USA",
    "Data Scientist in USA",
    "Finance Analyst in USA",
    "Operations Analyst in USA",
    "Marketing Analyst in USA",
    "Risk Analyst in USA",
    "Business Analyst in USA",
    "Supply Chain Analyst in USA",
]

JSEARCH_FIELDS = [
    "work_arrangement", "seniority_level", "required_experience_years",
    "required_technologies", "preferred_technologies", "job_function",
    "industry", "education_required", "visa_sponsorship",
    "relocation_required", "has_management_responsibilities",
    "ai_ml_involved", "benefits_extended", "soft_skills",
]

GEMINI_FIELDS = [
    "seniority", "remote_status", "industry", "tools",
    "hard_skills", "soft_skills", "education", "employment_type",
]


def harvest_jobs(target=100):
    jobs = []
    with httpx.Client() as client:
        for q in QUERIES:
            if len(jobs) >= target:
                break
            for page in range(1, 3):
                if len(jobs) >= target:
                    break
                params = {"query": q, "page": str(page),
                          "date_posted": "week", "country": "us"}
                try:
                    r = client.get("https://jsearch.p.rapidapi.com/search",
                                   headers=HEADERS, params=params, timeout=30.0)
                    if r.status_code == 200:
                        jobs.extend(r.json().get("data", []))
                    time.sleep(1.2)
                except Exception as e:
                    print(f"[search error] {q} p{page}: {e}")
    return jobs[:target]


def jsearch_details(job_id):
    params = {"job_id": job_id, "country": "us"}
    try:
        r = httpx.get("https://jsearch.p.rapidapi.com/job-details",
                      headers=HEADERS, params=params, timeout=30.0)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return data[0] if data else {}
    except Exception as e:
        print(f"[details error] {e}")
    return {}


def gemini_enrich(job):
    time.sleep(4.5)
    desc = job.get("job_description", "")
    if not desc:
        return {}
    prompt = f"""You are a data analyst assistant. Extract these fields as JSON from the job posting.
Return ONLY JSON with: seniority (entry/junior/mid/senior/lead/principal/manager),
remote_status (Remote/On-site/Hybrid/Unspecified), industry, tools (list),
hard_skills (list), soft_skills (list), education, employment_type.

JOB TITLE: {job.get('job_title')}
JOB DESCRIPTION:
{desc}
"""
    try:
        resp = ai_client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(resp.text)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[gemini error] {e}")
        return {}


def is_populated(val):
    return val not in (None, "", [], {}, "Unspecified", "unspecified")


def main():
    print("Harvesting ~100 jobs across varied roles...\n")
    jobs = harvest_jobs(100)
    print(f"Harvested {len(jobs)} jobs. Running BOTH enrichers on each...\n")

    results = []
    js_present = {f: 0 for f in JSEARCH_FIELDS}
    gm_present = {f: 0 for f in GEMINI_FIELDS}
    total = 0
    raw_dumped = False

    for i, job in enumerate(jobs, 1):
        jid = job.get("job_id")
        title = job.get("job_title", "?")
        if not jid:
            continue

        print(f"[{i}/{len(jobs)}] {title[:60]}")

        js = jsearch_details(jid)

        if not raw_dumped and js:
            with open("jsearch_raw_sample.json", "w") as f:
                json.dump(js, f, indent=2, default=str)
            raw_dumped = True
            print("   (saved one raw JSearch response to jsearch_raw_sample.json)")

        gm = gemini_enrich(job)
        total += 1

        for f in JSEARCH_FIELDS:
            if is_populated(js.get(f)):
                js_present[f] += 1
        for f in GEMINI_FIELDS:
            if is_populated(gm.get(f)):
                gm_present[f] += 1

        results.append({
            "job_title": title,
            "jsearch_native": {f: js.get(f) for f in JSEARCH_FIELDS},
            "gemini": {f: gm.get(f) for f in GEMINI_FIELDS},
        })

    with open("comparison_output.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print(f"JSEARCH NATIVE completeness (out of {total}):")
    print("=" * 70)
    for f in JSEARCH_FIELDS:
        n = js_present[f]
        print(f"  {f:32s}: {n}/{total}  ({n/total*100:.0f}%)" if total else f)

    print("\n" + "=" * 70)
    print(f"GEMINI completeness (out of {total}):")
    print("=" * 70)
    for f in GEMINI_FIELDS:
        n = gm_present[f]
        print(f"  {f:32s}: {n}/{total}  ({n/total*100:.0f}%)" if total else f)

    print(f"\nFull side-by-side -> comparison_output.json ({total} jobs)")
    print("One raw JSearch response -> jsearch_raw_sample.json")


if __name__ == "__main__":
    main()