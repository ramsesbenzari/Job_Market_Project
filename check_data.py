import json

with open("emergency_backup.json", "r", encoding="utf-8") as f:
    all_jobs = json.load(f)

print(f"Total records in JSON: {len(all_jobs)}")

# Inspect the first 3 jobs
for i in range(min(3, len(all_jobs))):
    job = all_jobs[i]
    title = job.get("job_title", "NO TITLE")
    desc = job.get("description", "")
    
    print(f"\n=== JOB {i+1}: {title} ===")
    print(f"Description Character Length: {len(desc)}")
    print("Excerpt:")
    print(desc[:600] + "\n[... Truncated ...]")