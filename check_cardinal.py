import json

with open("emergency_backup.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# Scan for the specific record
found = False
for job in data:
    company_name = str(job.get("company", ""))
    if "Cardinal" in company_name or "CTS" in company_name:
        print("\n=== RAW TEXT ENTIRELY AS IT EXISTS IN JSON ===")
        print(f"Title: {job.get('job_title')}")
        print(f"Company: {job.get('company')}")
        print(f"Description:\n{job.get('description')}")
        print("=============================================\n")
        found = True
        break

if not found:
    print("\n[!] Could not find a job with 'Cardinal' or 'CTS' in the company field inside emergency_backup.json.\n")