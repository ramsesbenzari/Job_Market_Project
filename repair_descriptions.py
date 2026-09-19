"""
repair_descriptions.py - LIVE DEEP CRAWLER FOR UNTRUNCATED JOB DATA
"""
import json
import logging
import os
import time
import concurrent.futures
import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("deep_scraper")

INPUT_FILE = "emergency_backup.json"
OUTPUT_FILE = "emergency_backup_fixed.json"

# Standard desktop browser headers to prevent immediate request rejection
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive"
}

def fetch_live_full_text(url: str, current_idx: int, total: int) -> str:
    if not url or "Missing Link" in url or not url.startswith("http"):
        return ""
        
    # Standardize jooble redirect structures if encountered
    try:
        with httpx.Client(headers=HEADERS, timeout=12.0, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code != 200:
                return ""
                
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Remove script, css, and style tags to isolate pure textual nodes
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.extract()
                
            # Target common content container selectors used by job boards
            main_container = (
                soup.find("article") or 
                soup.find("main") or 
                soup.find("div", {"class": re.compile(r"(job|description|content|vacancy)", re.I)}) or
                soup.body
            )
            
            if main_container:
                # Extract structured text while maintaining formatting space layouts
                raw_text = main_container.get_text(separator="\n", strip=True)
                # Eliminate massive consecutive newline whitespace gaps
                clean_text = "\n".join([line.strip() for line in raw_text.splitlines() if line.strip()])
                return clean_text
            return ""
            
    except Exception as e:
        return ""

def process_job_row(job: dict, idx: int, total: int) -> dict:
    url = job.get("job_url") or job.get("url")
    live_text = fetch_live_full_text(url, idx, total)
    
    if live_text and len(live_text) > len(job.get("description", "")):
        log.info(f" -> Successfully pulled full text ({len(live_text)} chars) for row {idx}/{total}")
        job["description"] = live_text
    else:
        log.warning(f" -> Could not enhance row {idx}/{total}. Retaining existing text payload.")
        
    return job

def main():
    if not os.path.exists(INPUT_FILE):
        log.error(f"Source file {INPUT_FILE} not found.")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        all_jobs = json.load(f)
        
    total_jobs = len(all_jobs)
    log.info(f"Loaded {total_jobs} records from local file. Starting concurrent page crawler...")
    
    fixed_dataset = []
    max_workers = 10  # Safely crawl multiple endpoints concurrently without crashing local sockets
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_job_row, job, idx, total_jobs): idx for idx, job in enumerate(all_jobs, start=1)}
        
        for future in concurrent.futures.as_completed(futures):
            try:
                updated_job = future.result()
                fixed_dataset.append(updated_job)
            except Exception as exc:
                pass

    # Save out the expanded data source array
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(fixed_dataset, f, ensure_ascii=False, indent=4)
        
    log.info(f"Complete. Cleaned full-text records written directly to: {OUTPUT_FILE}")

if __name__ == "__main__":
    import re
    main()