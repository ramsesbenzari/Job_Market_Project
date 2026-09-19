WITH state_map AS (
  SELECT code, name FROM UNNEST([
    STRUCT('al' AS code,'Alabama' AS name), ('ak','Alaska'), ('az','Arizona'), ('ar','Arkansas'),
    ('ca','California'), ('co','Colorado'), ('ct','Connecticut'), ('de','Delaware'),
    ('fl','Florida'), ('ga','Georgia'), ('hi','Hawaii'), ('id','Idaho'), ('il','Illinois'),
    ('in','Indiana'), ('ia','Iowa'), ('ks','Kansas'), ('ky','Kentucky'), ('la','Louisiana'),
    ('me','Maine'), ('md','Maryland'), ('ma','Massachusetts'), ('mi','Michigan'),
    ('mn','Minnesota'), ('ms','Mississippi'), ('mo','Missouri'), ('mt','Montana'),
    ('ne','Nebraska'), ('nv','Nevada'), ('nh','New Hampshire'), ('nj','New Jersey'),
    ('nm','New Mexico'), ('ny','New York'), ('nc','North Carolina'), ('nd','North Dakota'),
    ('oh','Ohio'), ('ok','Oklahoma'), ('or','Oregon'), ('pa','Pennsylvania'),
    ('ri','Rhode Island'), ('sc','South Carolina'), ('sd','South Dakota'), ('tn','Tennessee'),
    ('tx','Texas'), ('ut','Utah'), ('vt','Vermont'), ('va','Virginia'), ('wa','Washington'),
    ('wv','West Virginia'), ('wi','Wisconsin'), ('wy','Wyoming'),
    ('dc','District of Columbia'), ('pr','Puerto Rico')
  ])
),
prep AS (
  SELECT
    job_id, TRIM(job_title) AS job_title, TRIM(company) AS company,
    TRIM(industry) AS industry, TRIM(city) AS city, TRIM(state) AS state,
    description, job_url, source_api, salary_min, salary_max,
    tools, hard_skills, soft_skills, remote_status, employment_type,
    benefits, education, date_posted, date_retrieved,
    CASE WHEN job_id IS NULL THEN 'legacy_etl' ELSE 'cloud_pipeline' END AS data_origin,
    REGEXP_REPLACE(LOWER(TRIM(COALESCE(state,''))), r'[^a-z]', '') AS state_key,
    REGEXP_REPLACE(
      REGEXP_REPLACE(
        REGEXP_REPLACE(LOWER(TRIM(COALESCE(city,''))), r'^st\.? ', 'saint '),
        r'^mt\.? ', 'mount '),
      r'^ft\.? ', 'fort ') AS city_key
  FROM `data-job-market.data_job_market.us_job_data`
  WHERE job_title IS NOT NULL AND job_title != ''
),
base AS (
  SELECT
    prep.*,
    CASE
      WHEN city_key IN ('','remote','unspecified','unknown','various','n/a','na','home',
                        'headquarters','virtual','all','central','university',
                        'various conus sites','multiple locations','il','ny')
        THEN 'Unspecified'
      WHEN city_key IN ('new york city','nyc') THEN 'New York'
      WHEN city_key IN ('washington dc','washington d.c.','washington, dc','washington, d.c.')
        THEN 'Washington'
      WHEN city_key IN ('mclean','mc lean','tysons (mclean)') THEN 'McLean'
      WHEN city_key = 'boise city' THEN 'Boise'
      WHEN city_key IN ('rtp','research triangle park') THEN 'Research Triangle Park'
      WHEN city_key IN ('apg','aberdeen proving grounds','aberdeen proving ground')
        THEN 'Aberdeen Proving Ground'
      WHEN REGEXP_CONTAINS(city_key, r',| or |/') THEN 'Multiple cities'
      ELSE INITCAP(city_key)
    END AS city_clean,
    CASE
      WHEN industry IS NULL OR TRIM(industry) = ''
        OR UPPER(TRIM(industry)) IN ('N/A','NA','UNKNOWN','UNSPECIFIED') THEN 'Unspecified'
      WHEN TRIM(industry) = 'Information Technology' THEN 'Technology'
      WHEN TRIM(industry) = 'Financial Services' THEN 'Finance'
      WHEN TRIM(industry) IN ('Aerospace and Defense','Defense and Aerospace',
                              'Defense and Government Contracting','Defense and Intelligence')
        THEN 'Defense'
      ELSE TRIM(industry)
    END AS industry_clean,
    CASE
      WHEN source_api IS NULL OR TRIM(source_api) = '' THEN 'Unknown'
      WHEN LOWER(TRIM(source_api)) = 'bebee' THEN 'beBee'
      ELSE TRIM(source_api)
    END AS platform_clean
  FROM prep
),
joined AS (
  SELECT base.*, m.name AS mapped_state, m.code AS mapped_code
  FROM base
  LEFT JOIN state_map m
    ON base.state_key = m.code
    OR base.state_key = REGEXP_REPLACE(LOWER(m.name), r'[^a-z]', '')
),
classified AS (
  SELECT
    * EXCEPT(state_key, mapped_state, mapped_code, city_key),
    CASE
      WHEN state IS NULL OR state = ''
        OR state_key IN ('remote','unspecified','unknown','various','multiplestates','na')
        THEN 'Unspecified'
      WHEN REGEXP_CONTAINS(state, r',| or |/|;') THEN 'Multi-state'
      WHEN state_key IN ('us','usa','unitedstates','unitedstatesofamerica') THEN 'Nationwide'
      WHEN state_key = 'washingtondc' THEN 'District of Columbia'
      WHEN mapped_state IS NOT NULL THEN mapped_state
      ELSE 'Non-US'
    END AS state_clean,
    CASE
      WHEN state IS NULL OR state = ''
        OR state_key IN ('remote','unspecified','unknown','various','multiplestates','na')
        THEN 'Unspecified'
      WHEN REGEXP_CONTAINS(state, r',| or |/|;') THEN 'Multi-state'
      WHEN state_key IN ('us','usa','unitedstates','unitedstatesofamerica') THEN 'Nationwide'
      WHEN state_key = 'washingtondc' THEN 'DC'
      WHEN mapped_code IS NOT NULL THEN UPPER(mapped_code)
      ELSE 'Non-US'
    END AS state_abbr
  FROM joined
)
SELECT
  classified.*,
  CASE
    WHEN city_clean = 'Unspecified' THEN 'Unspecified'
    ELSE CONCAT(city_clean, ', ', state_abbr)
  END AS city_state,
  -- Consolidates ~1,467 raw Gemini industry values into a short list.
  -- Order matters: Fintech before Finance, Pharma before Healthcare,
  -- Telecom and Insurtech before Technology. Do not reorder these branches.
  CASE
    WHEN industry_clean = 'Unspecified' THEN 'Unspecified'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'fintech|financial technology|finance and technology') THEN 'Fintech'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'insurance|insurtech') THEN 'Insurance'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'financ|banking|\bbank\b|investment|capital market|wealth|asset management|accounting|private equity|venture capital') THEN 'Finance'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'pharma|biotech|life science|clinical research') THEN 'Pharma & Biotech'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'health|hospital|medical|clinical|patient') THEN 'Healthcare'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'defen[sc]e|aerospace|military|national security|government contract') THEN 'Defense & Aerospace'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'government|public sector|federal|municipal') THEN 'Government'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'staffing|recruit|talent acquisition|human resources|human capital') THEN 'Staffing & Recruiting'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'consult|professional services|advisory|business services|business process outsourcing|market research') THEN 'Consulting'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'education|university|college|academic|edtech|school') THEN 'Education'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'telecom|wireless') THEN 'Telecommunications'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'technolog|software|saas|\bit\b|cloud|internet|computer|cyber|semiconductor|artificial intelligence|data analytics|data and analytics|business intelligence|data cent|information services|^analytics$|^tech$|^ai$') THEN 'Technology'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'retail|e.?commerce|consumer goods|consumer packaged|consumer products|consumer electronics|^electronics$|apparel|grocery|luxury goods|beauty|pet products|tobacco') THEN 'Retail & Consumer'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'logistic|supply chain|transportation|freight|shipping|warehous|distribution|wholesale') THEN 'Logistics & Supply Chain'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'automotive|vehicle|mobility|powersports') THEN 'Automotive'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'manufactur|industrial|chemical|machinery|packaging|building materials') THEN 'Manufacturing'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'energy|utilit|oil|\bgas\b|renewable|solar|electric power|\bhvac\b') THEN 'Energy & Utilities'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'media|entertainment|gaming|video game|publishing|broadcast|film|music|sports') THEN 'Media & Entertainment'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'marketing|advertis|ad.?tech') THEN 'Marketing & Advertising'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'real estate|construction|property') THEN 'Real Estate & Construction'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'hospitality|travel|hotel|restaurant|food|beverage|tourism|airline|aviation') THEN 'Hospitality & Travel'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'legal|\blaw\b') THEN 'Legal'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'non.?profit|\bngo\b|charity|social services') THEN 'Nonprofit'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'agricultur|farming|agribusiness') THEN 'Agriculture'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'environmental|waste|recycling|mining') THEN 'Environmental & Resources'
    WHEN REGEXP_CONTAINS(LOWER(industry_clean), r'engineering|research and development|\br&d\b') THEN 'Engineering & R&D'
    ELSE 'Other'
  END AS industry_group,
  CASE
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'data scientist|data science') THEN 'Data Scientist'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'supply chain|logistic|procure|purchas|inventory|transportation|distribution|allocation|category management') THEN 'Supply Chain / Logistics'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\brisk\b|fraud') THEN 'Risk Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bcredit\b') THEN 'Credit Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'financ|fp&?a|account|treasury|budget|billing|investment|\bcost\b') THEN 'Finance Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'pricing|revenue') THEN 'Pricing / Revenue Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bsales\b') THEN 'Sales Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'marketing') THEN 'Marketing Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'compliance|quality|governance|\baudit') THEN 'Compliance / Quality Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'market research|research analyst') THEN 'Research / Market Research'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'product (analyst|manager|owner)') THEN 'Product Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'health|clinical|medical') THEN 'Healthcare Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'operation|\bops\b') THEN 'Operations Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'business intelligence|\bbi\b') THEN 'BI Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'business analyst|business systems|business process|business planning|business transformation|business development|business technology') THEN 'Business Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'data analy') THEN 'Data Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'analytics|insights|reporting') THEN 'Analytics / Insights'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'systems? analyst|requirements analyst') THEN 'Systems Analyst'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\banalyst\b') THEN 'Specialized Analyst'
    ELSE 'Uncategorized'
  END AS role_category,
  CASE
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bmanager\b|\bdirector\b|\bhead of\b|\bvp\b|vice president|\bchief\b') THEN 'Manager / Director+'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\blead\b|\bprincipal\b|\bstaff\b') THEN 'Lead / Principal'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bsenior\b|\bsr\b|\bii\b|\biii\b') THEN 'Senior'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bintern\b|internship') THEN 'Intern'
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bjunior\b|\bjr\b|entry.level|entry level|\bassociate\b|analyst i\b|level i\b') THEN 'Entry / Junior'
    ELSE 'Mid'
  END AS seniority,
  CASE
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bmanager\b|\bdirector\b|\bhead of\b|\bvp\b|vice president|\bchief\b') THEN 6
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\blead\b|\bprincipal\b|\bstaff\b') THEN 5
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bsenior\b|\bsr\b|\bii\b|\biii\b') THEN 4
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bintern\b|internship') THEN 1
    WHEN REGEXP_CONTAINS(LOWER(job_title), r'\bjunior\b|\bjr\b|entry.level|entry level|\bassociate\b|analyst i\b|level i\b') THEN 2
    ELSE 3
  END AS seniority_order,
  CASE
    WHEN REGEXP_CONTAINS(LOWER(education), r'phd|ph\.?d|doctora|doctor of|juris doctor|law degree|\bm\.?d\b') THEN 'Doctorate'
    WHEN REGEXP_CONTAINS(LOWER(education), r'master|graduate degree|advanced degree') THEN "Master's"
    WHEN REGEXP_CONTAINS(LOWER(education), r'bachelor|undergraduate|university degree|\bbsn\b') THEN "Bachelor's"
    WHEN REGEXP_CONTAINS(LOWER(education), r'associate|two.year|vocational|technical degree|diploma|some college') THEN 'Associate / Technical'
    WHEN REGEXP_CONTAINS(LOWER(education), r'high school|\bged\b') THEN 'High School'
    ELSE 'Unspecified'
  END AS education_clean,
  CASE
    WHEN LOWER(employment_type) LIKE '%contract%' OR LOWER(employment_type) LIKE '%freelance%' THEN 'Contract'
    WHEN LOWER(employment_type) LIKE '%full%' THEN 'Full-time'
    WHEN LOWER(employment_type) LIKE '%part%' THEN 'Part-time'
    WHEN LOWER(employment_type) LIKE '%intern%' THEN 'Internship'
    ELSE 'Unspecified'
  END AS employment_type_clean,
  CASE
    WHEN salary_min IS NOT NULL AND salary_max IS NOT NULL THEN (salary_min + salary_max) / 2
    WHEN salary_min IS NOT NULL THEN salary_min
    WHEN salary_max IS NOT NULL THEN salary_max
    ELSE NULL
  END AS salary_midpoint,
  CASE
    WHEN (salary_min IS NOT NULL OR salary_max IS NOT NULL)
     AND ((COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 BETWEEN 20000 AND 400000)
    THEN TRUE ELSE FALSE
  END AS salary_is_valid,
  CASE
    WHEN salary_min IS NULL AND salary_max IS NULL THEN 'Unknown'
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 60000 THEN '< 60k'
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 90000 THEN '60k-90k'
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 120000 THEN '90k-120k'
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 150000 THEN '120k-150k'
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 <= 400000 THEN '150k-400k'
    ELSE 'Outlier (>400k)'
  END AS salary_band,
  -- numeric order so Power BI sorts salary bands by value, not alphabetically
  CASE
    WHEN salary_min IS NULL AND salary_max IS NULL THEN 0
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 60000 THEN 1
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 90000 THEN 2
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 120000 THEN 3
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 < 150000 THEN 4
    WHEN (COALESCE(salary_min, salary_max) + COALESCE(salary_max, salary_min))/2 <= 400000 THEN 5
    ELSE 6
  END AS salary_band_order
FROM classified
WHERE state_clean != 'Non-US'
  AND city_clean NOT IN ('Bangkok','Mexico City','Bangalore','Bengaluru','Shanghai District',
                         'Napoli','Zug','Brussels','Amsterdam','Glasgow','Singapore','Novara',
                         'San Donato Milanese','Piove Di Sacco','Cernusco Sul Naviglio',
                         'Borgaro Torinese','Turbigo','Mailand','Alajuela','Europe','Portugal')
  AND NOT (state_clean = 'Unspecified'
           AND city_clean IN ('Milan','Milano','Rome','Athens','Parma','Madrid','London',
                              'Lisbon','Poland','Toronto','Turin','Kingston','Zapata',
                              'New Brunswick','Hydes','Essex Fells'))
  AND NOT REGEXP_CONTAINS(LOWER(job_title), r'ai trainer|ai model trainer|model trainer|ai training|data annotat|ai annotat|data label')
  AND NOT REGEXP_CONTAINS(LOWER(job_title), r'\bnurse\b|\brn\b|technologist|technician|\bcna\b|\blpn\b|physician|paramedic|midwife|driver|warehouse|picking|packing|biologist|histotech|cytotech|phlebotom|radiolog|sonograph|therapist|behavior analyst|behaviour analyst|\bbcba\b')
  AND NOT (REGEXP_CONTAINS(LOWER(job_title), r'engineer|developer|architect|programmer') AND NOT REGEXP_CONTAINS(LOWER(job_title), r'\banalyst\b'))
  AND REGEXP_CONTAINS(LOWER(job_title), r'\banalyst\b|analytics|insights|business intelligence|\bbi\b|data scien|\banalysis\b')
-- Deduplication on posting URL. Two rows sharing the same job_url are the same
-- posting collected twice; the most recently collected copy is kept.
-- Earlier versions matched on company + title + city, which was too coarse:
-- large employers post several distinct requisitions with the same title in the
-- same city, and that rule deleted them.
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY job_url
  ORDER BY date_retrieved DESC
) = 1