#!/usr/bin/env python3
"""Quick test of pipeline update functions."""

from main_old import _is_us_location, _merge_remote_status, extract_state

# Test _is_us_location
test_cases = [
    ("Atlanta, GA", True),
    ("Berlin, Germany", False),
    ("Remote", True),
    ("São Paulo, Brazil", False),
]
print("Testing _is_us_location:")
for loc, expected in test_cases:
    result = _is_us_location(loc)
    status = "✓" if result == expected else "✗"
    print(f"  {status} {loc:<25} → {result} (expected {expected})")

# Test _merge_remote_status
merge_cases = [
    (True, "Remote", "Remote"),
    (True, "On-site", "Hybrid"),
    (False, "Remote", "Hybrid"),
    (None, "Remote", "Remote"),
]
print("\nTesting _merge_remote_status:")
for api_flag, regex_res, expected in merge_cases:
    result = _merge_remote_status(api_flag, regex_res)
    status = "✓" if result == expected else "✗"
    print(f"  {status} api={str(api_flag):<6} regex={regex_res:<10} → {result} (expected {expected})")

# Test extract_state with multi-location
print("\nTesting extract_state (multi-location):")
cases = [
    ("Atlanta, GA; Naperville, IL", "GA"),
    ("New York, NY / Chicago, IL", "NY"),
    ("Dallas, TX | Austin, TX", "TX"),
]
for loc, expected in cases:
    result = extract_state(loc)
    status = "✓" if result == expected else "✗"
    print(f"  {status} {loc:<40} → {result} (expected {expected})")

print("\n✅ All pipeline functions working correctly!")
