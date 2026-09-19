WITH raw_skills AS (
  SELECT
    job_url, city_state, state_clean, role_category,
    skill_type,
    LOWER(TRIM(skill)) AS skill_raw
  FROM `data-job-market.data_job_market.v_dashboard`,
  UNNEST([
    STRUCT(tools AS raw, 'tool' AS skill_type),
    STRUCT(hard_skills AS raw, 'hard_skill' AS skill_type),
    STRUCT(soft_skills AS raw, 'soft_skill' AS skill_type)
  ]) AS s,
  UNNEST(SPLIT(s.raw, ',')) AS skill
  WHERE s.raw IS NOT NULL AND s.raw != '' AND TRIM(skill) != ''
),
merged AS (
  SELECT
    job_url, city_state, state_clean, role_category, skill_type,
    CASE
      -- ===== TOOLS =====
      WHEN skill_raw IN ('microsoft excel','ms excel','ms-excel','microsoft office excel')
        THEN 'excel'
      WHEN skill_raw IN ('powerbi','microsoft power bi','ms power bi','microsoft powerbi',
                         'power-bi','power bi desktop','power bi service')
        THEN 'power bi'
      WHEN skill_raw IN ('microsoft powerpoint','ms powerpoint','power point',
                         'microsoft power point','microsoft office powerpoint','ms power point')
        THEN 'powerpoint'
      WHEN skill_raw IN ('microsoft word','ms word','microsoft office word','ms-word')
        THEN 'word'
      WHEN skill_raw IN ('microsoft office suite','ms office','ms office suite',
                         'microsoft office 365','office 365','ms office 365',
                         'microsoft office products','microsoft office professional suite',
                         'microsoft office applications')
        THEN 'microsoft office'
      WHEN skill_raw IN ('microsoft azure','ms azure') THEN 'azure'
      WHEN skill_raw IN ('microsoft outlook','ms outlook') THEN 'outlook'
      WHEN skill_raw IN ('access','ms access') THEN 'microsoft access'
      WHEN skill_raw IN ('microsoft visio','ms visio') THEN 'visio'
      WHEN skill_raw IN ('microsoft sql server','ms sql server') THEN 'sql server'
      WHEN skill_raw IN ('teams','ms teams') THEN 'microsoft teams'
      WHEN skill_raw IN ('copilot','ms copilot') THEN 'microsoft copilot'
      WHEN skill_raw IN ('ms project') THEN 'microsoft project'
      WHEN skill_raw IN ('microsoft power platform') THEN 'power platform'
      WHEN skill_raw IN ('apache spark') THEN 'spark'

      -- ===== HARD SKILLS =====
      WHEN skill_raw = 'data analytics' THEN 'data analysis'
      WHEN skill_raw = 'root-cause analysis' THEN 'root cause analysis'
      WHEN skill_raw = 'time-series analysis' THEN 'time series analysis'
      WHEN skill_raw = 'software development life cycle' THEN 'software development lifecycle'

      -- ===== SOFT SKILLS =====
      WHEN skill_raw IN ('problem-solving','problem-solving skills','problem solving skills',
                         'problem-solving abilities','problem-solving mindset')
        THEN 'problem solving'
      WHEN skill_raw IN ('detail-oriented','detail oriented','detail orientation',
                         'attention to details','meticulous attention to detail',
                         'exceptional attention to detail','high attention to detail')
        THEN 'attention to detail'
      WHEN skill_raw IN ('analytical skills','analytical','analytical mindset',
                         'analytical abilities','analytical ability',
                         'analytical rigor','analytical reasoning')
        THEN 'analytical thinking'
      WHEN skill_raw IN ('mentorship','mentor') THEN 'mentoring'
      WHEN skill_raw IN ('organization','organization skills','organized',
                         'organizational','organizational abilities')
        THEN 'organizational skills'
      WHEN skill_raw = 'communication skills' THEN 'communication'
      WHEN skill_raw = 'communication (written)' THEN 'written communication'
      WHEN skill_raw = 'communication (verbal)' THEN 'verbal communication'
      WHEN skill_raw = 'time-management' THEN 'time management'
      WHEN skill_raw = 'presentation' THEN 'presentation skills'
      WHEN skill_raw = 'multi-tasking' THEN 'multitasking'
      WHEN skill_raw = 'decision-making' THEN 'decision making'
      WHEN skill_raw = 'proactive mindset' THEN 'proactive'
      WHEN skill_raw = 'collaborative mindset' THEN 'collaborative'
      ELSE skill_raw
    END AS skill_merged
  FROM raw_skills
)
SELECT DISTINCT
  job_url, city_state, state_clean, role_category, skill_type,
  CASE
    WHEN skill_merged IN ('sql','r','aws','sap','sas','erp','gcp','crm','vba','spss',
                          'ssrs','ssis','s3','ai','dax','wms','html','xml','css','json',
                          'etl','elt','sdlc','uat','nlp','api','apis','ga4','bi','kpi',
                          'ux','ui','qa','vpn','oltp','olap')
      THEN UPPER(skill_merged)
    WHEN skill_merged = 'power bi' THEN 'Power BI'
    WHEN skill_merged = 'sql server' THEN 'SQL Server'
    WHEN skill_merged = 'mysql' THEN 'MySQL'
    WHEN skill_merged = 'nosql' THEN 'NoSQL'
    WHEN skill_merged = 'postgresql' THEN 'PostgreSQL'
    WHEN skill_merged = 'numpy' THEN 'NumPy'
    WHEN skill_merged = 'mlops' THEN 'MLOps'
    WHEN skill_merged = 'llms' THEN 'LLMs'
    WHEN skill_merged = 'dbt' THEN 'dbt'
    WHEN skill_merged = 'javascript' THEN 'JavaScript'
    WHEN skill_merged = 'powerpoint' THEN 'PowerPoint'
    WHEN skill_merged = 'sharepoint' THEN 'SharePoint'
    ELSE INITCAP(skill_merged)
  END AS skill
FROM merged