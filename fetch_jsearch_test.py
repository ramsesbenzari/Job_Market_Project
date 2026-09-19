"""
fetch_jsearch_test.py - REAL-TIME HIGH-ACCURACY JOB COLLECTOR
"""
import json
import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("jsearch_collector")

API_KEY = os.getenv("RAPIDAPI_KEY")
URL = "https://jsearch.p.rapidapi.com/search"

def main():
    if not API_KEY:
        log.error("Missing RAPIDAPI_KEY inside your .env file!")
        return

    headers = {
        "x-rapidapi-key": API_KEY,
        "x-rapidapi-host": "jsearch.p.rapidapi.com"
    }
    
    params = {
        "query": "Data Analyst in USA",
        "page": "1",
        "num_pages": "1",
        "date_posted": "week"
    }

    log.info("Sending requests to JSearch API Gateway...")
    
    try:
        with httpx.Client() as client:
            response = client.get(URL, headers=headers, params=params, timeout=15.0)
            
            if response.status_code != 200:
                log.error(f"API Rejected Connection. Status: {response.status_code} | Msg: {response.text}")
                return
                
            raw_data = response.json()
            job_listings = raw_data.get("data", [])
            
            log.info(f"Successfully harvested {len(job_listings)} live jobs from the network layer.")
            
            output_file = "jsearch_test_source.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(job_listings, f, ensure_ascii=False, indent=4)
                
            log.info(f"Target data committed to disk: {output_file}")
            
    except Exception as e:
        log.error(f"Network transaction crashed: {e}")

if __name__ == "__main__":
    main()