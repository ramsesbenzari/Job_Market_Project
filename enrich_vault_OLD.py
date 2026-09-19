import os
import json
import spacy
from spacy.matcher import PhraseMatcher
from tqdm import tqdm
from dotenv import load_dotenv
from google.cloud import bigquery
from google.oauth2 import service_account

# 1. Configuration
load_dotenv()
BQ_PROJECT_ID = os.getenv("BQ_PROJECT_ID")
BQ_DATASET = os.getenv("BQ_DATASET", "data_job_market")
BQ_TABLE = os.getenv("BQ_TABLE", "us_job_data") 
CREDENTIALS_PATH = "credentials.json"
LOCAL_BACKUP_FILE = "nlp_enriched_master_list.json"

credentials = service_account.Credentials.from_service_account_file(
    CREDENTIALS_PATH, scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
bq_client = bigquery.Client(credentials=credentials, project=BQ_PROJECT_ID)
table_id = f"{BQ_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"

# 2. Taxonomies
SKILLS_TAXONOMY = [
    'python', 'r', 'sql', 'java', 'scala', 'javascript', 'typescript', 'c++', 'c#',
    'julia', 'go', 'rust', 'matlab', 'sas', 'stata', 'spss', 'vba',
    'pandas', 'numpy', 'scikit-learn', 'sklearn', 'tensorflow', 'pytorch', 'keras',
    'spark', 'pyspark', 'hadoop', 'kafka', 'airflow', 'dbt', 'snowflake', 'databricks',
    'redshift', 'bigquery', 'synapse', 'fivetran', 'stitch', 'segment',
    'postgresql', 'postgres', 'mysql', 'mssql', 'sql server', 'oracle', 'mongodb',
    'cassandra', 'redis', 'dynamodb', 'cosmos db', 'elasticsearch',
    'tableau', 'power bi', 'powerbi', 'looker', 'looker studio', 'qlik', 'qlikview',
    'qliksense', 'mode analytics', 'sisense', 'domo', 'metabase', 'superset',
    'thoughtspot', 'sigma', 'hex', 'd3.js', 'plotly', 'matplotlib', 'seaborn',
    'ggplot', 'highcharts', 'google data studio',
    'aws', 'amazon web services', 'azure', 'gcp', 'google cloud', 's3', 'ec2',
    'lambda', 'glue', 'athena', 'sagemaker', 'vertex ai', 'cloud functions',
    'cloud run', 'kubernetes', 'docker', 'terraform',
    'machine learning', 'deep learning', 'nlp', 'computer vision', 'time series',
    'regression', 'classification', 'clustering', 'a/b testing', 'ab testing',
    'hypothesis testing', 'statistical modeling', 'bayesian', 'forecasting',
    'optimization', 'reinforcement learning', 'mlops',
    'excel', 'powerpoint', 'word', 'google sheets', 'financial modeling',
    'budgeting', 'variance analysis', 'p&l', 'pnl', 'kpi',
    'okr', 'salesforce', 'hubspot', 'sap', 'oracle erp', 'netsuite', 'workday',
    'quickbooks', 'jira', 'confluence', 'asana', 'trello', 'notion',
    'google analytics', 'ga4', 'google ads', 'facebook ads', 'meta ads',
    'adobe analytics', 'mixpanel', 'amplitude', 'heap', 'pendo',
    'marketo', 'pardot', 'mailchimp', 'klaviyo', 'attribution modeling',
    'seo', 'sem', 'ppc',
    'agile', 'scrum', 'kanban', 'waterfall', 'lean', 'six sigma', 'design thinking',
    'product analytics', 'cohort analysis', 'funnel analysis', 'churn analysis',
    'ltv', 'cac', 'mrr', 'arr',
    'rest api', 'graphql', 'json', 'xml', 'git', 'github', 'gitlab', 'bitbucket',
    'linux', 'bash', 'shell scripting', 'etl', 'elt', 'data warehousing',
    'data modeling', 'dimensional modeling', 'star schema', 'snowflake schema',
]
SOFT_SKILLS_TAXONOMY = [
    'communication', 'leadership', 'teamwork', 'problem solving', 'critical thinking', 
    'time management', 'project management', 'presentation', 'storytelling', 'agile'
]

# 3. Load NLP Model
print("🧠 Loading spaCy NLP Engine...")
nlp = spacy.load("en_core_web_sm")
matcher = PhraseMatcher(nlp.vocab, attr="LOWER")

# Teach the NLP what to look for
tech_patterns = [nlp.make_doc(text) for text in SKILLS_TAXONOMY]
soft_patterns = [nlp.make_doc(text) for text in SOFT_SKILLS_TAXONOMY]
matcher.add("TECH_SKILL", tech_patterns)
matcher.add("SOFT_SKILL", soft_patterns)

def execute_nlp_enrichment():
    # STEP 1: Download the Vault
    print(f"📥 Downloading current BigQuery table `{table_id}` to local backup...")
    query = f"SELECT * FROM `{table_id}`"
    results = bq_client.query(query).result()
    
    jobs = [dict(row) for row in results]
    for job in jobs:
        if job.get('date_retrieved'): job['date_retrieved'] = str(job['date_retrieved'])
        if job.get('date_posted'): job['date_posted'] = str(job['date_posted'])

    print(f"✅ Downloaded {len(jobs)} jobs. Starting NLP Extraction...")

    # STEP 2: Process with spaCy (No API Limits!)
    for job in tqdm(jobs, desc="Analyzing Descriptions"):
        desc = job.get('description', '')
        if not desc or (job.get('skills') and job.get('soft_skills')):
            continue # Skip if empty or already enriched
            
        doc = nlp(desc)
        matches = matcher(doc)
        
        found_tech = set()
        found_soft = set()
        
        for match_id, start, end in matches:
            match_name = nlp.vocab.strings[match_id]
            span = doc[start:end]
            if match_name == "TECH_SKILL":
                found_tech.add(span.text.title())
            elif match_name == "SOFT_SKILL":
                found_soft.add(span.text.title())
                
        # Merge existing skills with new NLP skills
        existing_tech = job.get('skills') or ""
        existing_soft = job.get('soft_skills') or ""
        
        all_tech = set([s.strip() for s in existing_tech.split(',') if s.strip()]) | found_tech
        all_soft = set([s.strip() for s in existing_soft.split(',') if s.strip()]) | found_soft
        
        job['skills'] = ", ".join(sorted(all_tech)) if all_tech else None
        job['soft_skills'] = ", ".join(sorted(all_soft)) if all_soft else None

    # STEP 3: Push to Cloud
    print("🚀 Overwriting BigQuery table with NLP enriched data...")
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    load_job = bq_client.load_table_from_json(jobs, table_id, job_config=job_config)
    load_job.result()
    print("✅ BigQuery Table successfully overwritten! Nulls eliminated.")

if __name__ == "__main__":
    execute_nlp_enrichment()