import json

d = json.load(open("comparison_output.json"))[:12]

for j in d:
    js = j["jsearch_native"]
    gm = j["gemini"]
    print("\n" + j["job_title"][:55])
    print(f"  JS: remote={js.get('work_arrangement')} | sen={js.get('seniority_level')} | tech={js.get('required_technologies')} | edu={js.get('education_required')}")
    print(f"  GM: remote={gm.get('remote_status')} | sen={gm.get('seniority')} | tools={gm.get('tools')} | edu={gm.get('education')}")