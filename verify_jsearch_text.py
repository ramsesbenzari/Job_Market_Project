import json

try:
    with open("jsearch_test_source.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    if jobs:
        first_job = jobs[0]
        print("\n================ JSEARCH AUDIT PREVIEW ================")
        print(f"TITLE: {first_job.get('job_title')}")
        print(f"COMPANY: {first_job.get('employer_name')}")
        print(f"SOURCE FEED: {first_job.get('job_publisher')}")
        print(f"CHARACTER COUNT: {len(first_job.get('job_description', ''))}")
        print("\nFIRST 400 CHARACTERS OF DESCRIPTION:")
        print(first_job.get('job_description')[:400])
        print("========================================================\n")
    else:
        print("[!] File is empty.")
except FileNotFoundError:
    print("[!] jsearch_test_source.json not found yet. Run fetch_jsearch_test.py first.")