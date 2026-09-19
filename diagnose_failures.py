import json

with open("emergency_backup.json", "r", encoding="utf-8") as f:
    all_jobs = json.load(f)

print("Searching for data extraction failures...")
failures = 0

for i, job in enumerate(all_jobs):
    desc = job.get("description", "").lower()
    
    # Check if a dollar sign exists but we know it returned null
    if "$" in desc and "salary" in desc:
        print(f"\n[!] Potential Failure in Job {i}: {job.get('job_title')}")
        print("The text contains a '$' but the database is getting null.")
        # Print the surrounding text of the dollar sign
        idx = desc.find("$")
        print(f"Context: ... {job.get('description')[idx-50:idx+100]} ...")
        failures += 1
        if failures >= 3:
            break