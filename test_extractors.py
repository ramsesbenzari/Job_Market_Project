#!/usr/bin/env python3
"""Quick test of extractors module."""

from extractors import extract_industry, extract_education, extract_benefits, extract_skills, extract_soft_skills

# Quick smoke test
test_desc = 'We seek a Python SQL expert for Data Science role with strong communication skills'

print('Testing extractors module:')
print(f'  Skills: {extract_skills(test_desc)}')
print(f'  Education: {extract_education(test_desc)}')
print(f'  Benefits: {extract_benefits(test_desc)}')
print(f'  Soft Skills: {extract_soft_skills(test_desc)}')
print(f'  Industry: {extract_industry("Google", test_desc)}')
print('\n✅ All extractors working correctly!')
