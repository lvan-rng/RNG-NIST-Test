#!/usr/bin/env python3
"""
Stress Test Suite for nist_analysis.py
=======================================
Verifies that every assessment rule follows BOTH:
  - NIST SP 800-22 Rev. 1a guidelines (Section 4.2)
  - RNG Labs internal policy (bilateral CI checks, Bernoulli sub-proportion)

Test groups:
  T1  assess_95()             — bilateral 95% CI boundary values
  T2  assess_99()             — bilateral 99% CI boundary values
  T3  check_proportion()      — NIST Bernoulli proportion threshold + ceil
  T4  check_sub_proportion()  — sub-proportion Bernoulli for K=148,8,18
  T5  row_passes()            — combined p-value + proportion decision
  T6  CI consistency          — 99% CI has ~1% failure rate, matching α=0.01
  T7  build_results()         — full result structure for MULTI_PROPORTION tests
  T8  find_failing_rows()     — escalation trigger logic
  T9  apply_decision_matrix() — all three verdict outcomes
  T10 End-to-end simulation   — CSPRNG-like data through full pipeline
"""

import math
import sys
import os
import re
import traceback

# ─── Import the module under test ────────────────────────────────────────────
# Add the script's directory to sys.path so we can import it directly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nist_analysis import (
    assess_95,
    assess_99,
    check_proportion,
    check_sub_proportion,
    row_passes,
    build_results,
    find_failing_rows,
    apply_decision_matrix,
    ALPHA,
    MULTI_PROPORTION_TESTS,
)

# ─── Test infrastructure ──────────────────────────────────────────────────────

_pass = 0
_fail = 0
_errors = []


def ok(condition, name, detail=""):
    global _pass, _fail
    if condition:
        print(f"  ✓  {name}")
        _pass += 1
    else:
        msg = f"  ✗  FAIL: {name}"
        if detail:
            msg += f"\n       {detail}"
        print(msg)
        _fail += 1
        _errors.append(name)


def section(title):
    print(f"\n{'═' * 70}")
    print(f"  {title}")
    print('═' * 70)


# ─── T1: assess_95() ─────────────────────────────────────────────────────────

section("T1 — assess_95(): bilateral 95% CI (PASS if 0.025 ≤ p ≤ 0.975)")

ok(assess_95(0.5)    == "PASS", "T1-01  midpoint 0.5 passes")
ok(assess_95(0.025)  == "PASS", "T1-02  lower boundary 0.025 passes (inclusive)")
ok(assess_95(0.975)  == "PASS", "T1-03  upper boundary 0.975 passes (inclusive)")
ok(assess_95(0.0249) == "FAIL", "T1-04  0.0249 fails (below lower boundary)")
ok(assess_95(0.9751) == "FAIL", "T1-05  0.9751 fails (above upper boundary)")
ok(assess_95(0.0)    == "FAIL", "T1-06  p=0.0 fails (extreme low)")
ok(assess_95(1.0)    == "FAIL", "T1-07  p=1.0 fails (extreme high)")
ok(assess_95(0.00088) == "FAIL","T1-08  p=0.00088 fails (real 4GB template-10 value)")
ok(assess_95(0.98345) == "FAIL","T1-09  p=0.98345 fails (real 4GB template-22 value)")
ok(assess_95(None)   == "N/A",  "T1-10  None returns N/A (no data)")
ok(assess_95(0.01)   == "FAIL", "T1-11  p=0.01 fails 95% check (below 0.025)")
ok(assess_95(0.99)   == "FAIL", "T1-12  p=0.99 fails 95% check (above 0.975)")

# ─── T2: assess_99() ─────────────────────────────────────────────────────────

section("T2 — assess_99(): bilateral 99% CI (PASS if 0.005 ≤ p ≤ 0.995)")

ok(assess_99(0.5)    == "PASS", "T2-01  midpoint 0.5 passes")
ok(assess_99(0.005)  == "PASS", "T2-02  lower boundary 0.005 passes (inclusive)")
ok(assess_99(0.995)  == "PASS", "T2-03  upper boundary 0.995 passes (inclusive)")
ok(assess_99(0.0049) == "FAIL", "T2-04  0.0049 fails (below lower boundary)")
ok(assess_99(0.9951) == "FAIL", "T2-05  0.9951 fails (above upper boundary)")
ok(assess_99(0.0)    == "FAIL", "T2-06  p=0.0 fails (extreme low)")
ok(assess_99(1.0)    == "FAIL", "T2-07  p=1.0 fails (extreme high)")
ok(assess_99(0.00088) == "FAIL","T2-08  p=0.00088 fails 99% (real 4GB template-10)")
ok(assess_99(0.98345) == "PASS","T2-09  p=0.98345 passes 99% (real 4GB template-22)")
ok(assess_99(0.99425) == "PASS","T2-10  p=0.99425 passes 99% (0.99425 < 0.995 upper bound)")
ok(assess_99(None)   == "N/A",  "T2-11  None returns N/A")
# 95% and 99% relationship: 99% is always equal-or-more-permissive
ok(assess_99(0.02)   == "PASS", "T2-12  p=0.02 passes 99% (in [0.005,0.995])")
ok(assess_95(0.02)   == "FAIL", "T2-13  same p=0.02 FAILS 95% (confirms 99% is broader)")
ok(assess_99(0.98)   == "PASS", "T2-14  p=0.98 passes 99% (in [0.005,0.995])")
ok(assess_95(0.98)   == "FAIL", "T2-15  same p=0.98 FAILS 95% (confirms 99% is broader)")

# ─── T3: check_proportion() ──────────────────────────────────────────────────

section("T3 — check_proportion(): NIST Bernoulli threshold with ceil rounding")

# At m=100, α=0.01:
#   min_pass_float = (0.99 - 3*sqrt(0.99*0.01/100)) * 100 = 96.015
#   min_pass = ceil(96.015) = 97
ok(check_proportion("97/100") == True,  "T3-01  97/100 passes (exactly at threshold)")
ok(check_proportion("96/100") == False, "T3-02  96/100 fails  (one below threshold)")
ok(check_proportion("100/100") == True, "T3-03  100/100 passes (perfect)")
ok(check_proportion("98/100") == True,  "T3-04  98/100 passes")

# At m=200, α=0.01:
#   min_pass_float = (0.99 - 3*sqrt(0.99*0.01/200)) * 200 = 193.779
#   min_pass = ceil(193.779) = 194
ok(check_proportion("194/200") == True,  "T3-05  194/200 passes at m=200 (exactly at threshold)")
ok(check_proportion("193/200") == False, "T3-06  193/200 fails  at m=200 (one below threshold)")

# At m=300, α=0.01:
#   min_pass_float = (0.99 - 3*sqrt(0.99*0.01/300)) * 300 = 291.830
#   min_pass = ceil(291.830) = 292
ok(check_proportion("292/300") == True,  "T3-07  292/300 passes at m=300 (exactly at threshold)")
ok(check_proportion("291/300") == False, "T3-08  291/300 fails  at m=300 (one below threshold)")

# Edge cases
ok(check_proportion("") == True,         "T3-09  empty string returns True (no data)")
ok(check_proportion("N/A") == True,      "T3-10  'N/A' returns True (no data)")
ok(check_proportion("----") == True,     "T3-11  '----' returns True (no data)")
ok(check_proportion("badformat") == True,"T3-12  unparseable returns True (safe default)")
ok(check_proportion("0/100") == False,   "T3-13  0/100 fails (nothing passed)")

# Verify ceiling behaviour explicitly (the number 96.015 must ceil to 97, not round to 96)
raw_min = (0.99 - 3.0 * math.sqrt(0.99 * 0.01 / 100)) * 100
ok(raw_min < 97,               "T3-14  min_pass_float=96.015 < 97 (ceil needed)")
ok(math.ceil(raw_min) == 97,   "T3-15  ceil(96.015) = 97 (ceiling applied correctly)")

# ─── T4: check_sub_proportion() ──────────────────────────────────────────────

section("T4 — check_sub_proportion(): Bernoulli for K=148, K=8, K=18 at α=0.01")

# ── Test 8 (K=148) ────────────────────────────────────────────────────────
# min_pass_float = (0.99 - 3*sqrt(0.99*0.01/148)) * 148 = 142.889
# min_pass = ceil(142.889) = 143  →  max 5 failures
pass8, min8, _ = check_sub_proportion(143, 148)
ok(pass8 == True,   "T4-01  Test8: 143/148 passes (exactly at threshold)")
ok(min8  == 143,    "T4-02  Test8: min_required = 143")

pass8b, _, _ = check_sub_proportion(142, 148)
ok(pass8b == False, "T4-03  Test8: 142/148 fails  (one below threshold)")

pass8c, _, _ = check_sub_proportion(148, 148)
ok(pass8c == True,  "T4-04  Test8: 148/148 passes (perfect)")

pass8d, _, _ = check_sub_proportion(144, 148)
ok(pass8d == True,  "T4-05  Test8: 144/148 passes (6 failures = within max 5 check: threshold is 143 so 143+ pass)")

# Verify the float boundary: 142.889 must NOT round to 143 via normal rounding
raw_min8 = (0.99 - 3.0 * math.sqrt(0.99 * 0.01 / 148)) * 148
ok(raw_min8 < 143,               "T4-06  Test8: raw float 142.889 < 143 (confirms ceil needed)")
ok(round(raw_min8) == 143,       "T4-07  Test8: round() also gives 143 (ok in this case)")
ok(math.ceil(raw_min8) == 143,   "T4-08  Test8: ceil() gives 143")

# ── Test 12 (K=8) ─────────────────────────────────────────────────────────
# min_pass_float = (0.99 - 3*sqrt(0.99*0.01/8)) * 8 = 7.076
# min_pass = ceil(7.076) = 8  →  max 0 failures allowed
pass12, min12, _ = check_sub_proportion(8, 8)
ok(pass12 == True,  "T4-09  Test12: 8/8 passes (exactly at threshold)")
ok(min12  == 8,     "T4-10  Test12: min_required = 8 (ceiling of 7.076)")

pass12b, _, _ = check_sub_proportion(7, 8)
ok(pass12b == False,"T4-11  Test12: 7/8 fails  (one below threshold — 0 failures allowed)")

# The critical ceiling trap for Test 12: round(7.076) = 7 (normal rounding rounds DOWN)
raw_min12 = (0.99 - 3.0 * math.sqrt(0.99 * 0.01 / 8)) * 8
ok(raw_min12 > 7.0,              "T4-12  Test12: raw float 7.076 > 7.0 (ceil mandatory)")
ok(round(raw_min12) == 7,        "T4-13  Test12: round() gives 7 — WRONG (would allow count=7 to pass)")
ok(math.ceil(raw_min12) == 8,    "T4-14  Test12: ceil()  gives 8 — CORRECT (7 < 7.076 must fail)")

# ── Test 13 (K=18) ────────────────────────────────────────────────────────
# min_pass_float = (0.99 - 3*sqrt(0.99*0.01/18)) * 18 = 16.554
# min_pass = ceil(16.554) = 17  →  max 1 failure allowed
pass13, min13, _ = check_sub_proportion(17, 18)
ok(pass13 == True,  "T4-15  Test13: 17/18 passes (exactly at threshold)")
ok(min13  == 17,    "T4-16  Test13: min_required = 17")

pass13b, _, _ = check_sub_proportion(16, 18)
ok(pass13b == False,"T4-17  Test13: 16/18 fails  (two failures, only 1 allowed)")

pass13c, _, _ = check_sub_proportion(18, 18)
ok(pass13c == True, "T4-18  Test13: 18/18 passes (perfect)")

# summary string format
_, _, summ = check_sub_proportion(145, 148)
ok("145/148" in summ, "T4-19  summary string contains 'N/M' format")
ok("min" in summ,     "T4-20  summary string contains 'min' keyword")

# Edge
p_edge, _, _ = check_sub_proportion(0, 0)
ok(p_edge == True,  "T4-21  K=0 returns True (no sub-results, no data)")

# ─── T5: row_passes() ────────────────────────────────────────────────────────

section("T5 — row_passes(): definitive pass/fail using 99% CI + proportion")

def make_row(p, prop="99/100"):
    return {"p_value": p, "proportion": prop}

# Clearly good p-values (deep in the middle)
ok(row_passes(make_row(0.5)),    "T5-01  p=0.5, 99/100 → PASS (both OK)")
ok(row_passes(make_row(0.3)),    "T5-02  p=0.3, 99/100 → PASS")
ok(row_passes(make_row(0.7)),    "T5-03  p=0.7, 99/100 → PASS")

# p-values that FAIL 95% but PASS 99% — must NOT fail overall_pass
ok(row_passes(make_row(0.02)),   "T5-04  p=0.02 passes 99% (0.02 > 0.005) — PASS despite 95% FAIL")
ok(row_passes(make_row(0.98)),   "T5-05  p=0.98 passes 99% (0.98 < 0.995) — PASS despite 95% FAIL")
ok(row_passes(make_row(0.026)),  "T5-06  p=0.026 passes 99% (just above 95% boundary) — PASS")

# p-values that FAIL 99% (and therefore fail overall)
ok(not row_passes(make_row(0.004)), "T5-07  p=0.004 fails 99% (< 0.005) → FAIL")
ok(not row_passes(make_row(0.996)), "T5-08  p=0.996 fails 99% (> 0.995) → FAIL")
ok(not row_passes(make_row(0.0)),   "T5-09  p=0.0   fails 99% → FAIL")
ok(not row_passes(make_row(1.0)),   "T5-10  p=1.0   fails 99% → FAIL")
ok(not row_passes(make_row(0.00088)), "T5-11  p=0.00088 fails (real 4GB template-10) → FAIL")
ok(row_passes(make_row(0.99425)),     "T5-12  p=0.99425 passes 99% CI (< 0.995 upper bound) → PASS")
ok(not row_passes(make_row(0.00027)), "T5-13  p=0.00027 fails (real 5GB template-147) → FAIL")
ok(row_passes(make_row(0.99146)),     "T5-14  p=0.99146 passes 99% CI (< 0.995 upper bound) → PASS")

# Proportion failures override even if p-value is fine
ok(not row_passes(make_row(0.5, "96/100")), "T5-15  p=0.5 but 96/100 proportion fails → FAIL")
ok(not row_passes(make_row(0.5, "0/100")),  "T5-16  p=0.5 but 0/100 proportion fails → FAIL")
ok(row_passes(make_row(0.5, "97/100")),     "T5-17  p=0.5 and 97/100 proportion → PASS")

# None p-value (NIST couldn't compute — treat as pass)
ok(row_passes(make_row(None)),              "T5-18  p=None → PASS (no data, can't assess)")

# p-values at exact 99% boundaries
ok(row_passes(make_row(0.005)),             "T5-19  p=0.005 (exactly at 99% lower bound) → PASS")
ok(row_passes(make_row(0.995)),             "T5-20  p=0.995 (exactly at 99% upper bound) → PASS")
ok(not row_passes(make_row(0.0049)),        "T5-21  p=0.0049 (just below lower bound) → FAIL")
ok(not row_passes(make_row(0.9951)),        "T5-22  p=0.9951 (just above upper bound) → FAIL")

# ─── T6: CI consistency ──────────────────────────────────────────────────────

section("T6 — CI consistency: 99% has ~1% failure rate, matching α=0.01")

# Simulate 10000 uniform p-values and verify ~1% fail the 99% bilateral check
import random
random.seed(42)
N_SIM = 100_000
fail_95_count = sum(1 for _ in range(N_SIM) if assess_95(random.random()) == "FAIL")
fail_99_count = sum(1 for _ in range(N_SIM) if assess_99(random.random()) == "FAIL")

actual_95_rate = fail_95_count / N_SIM
actual_99_rate = fail_99_count / N_SIM

# Allow ±0.5% tolerance around expected rates
ok(abs(actual_95_rate - 0.05) < 0.005,
   f"T6-01  95% bilateral failure rate ≈ 5% (got {actual_95_rate*100:.2f}%)",
   f"Expected 5.00% ± 0.5%, got {actual_95_rate*100:.2f}%")

ok(abs(actual_99_rate - 0.01) < 0.003,
   f"T6-02  99% bilateral failure rate ≈ 1% (got {actual_99_rate*100:.2f}%)",
   f"Expected 1.00% ± 0.3%, got {actual_99_rate*100:.2f}%")

# KEY CONSISTENCY CHECK:
# check_sub_proportion uses α=0.01 (1% failure rate).
# row_passes uses assess_99 (~1% failure rate).
# → These are calibrated to the same failure rate.
ok(abs(actual_99_rate - ALPHA) < 0.003,
   f"T6-03  99% failure rate ({actual_99_rate*100:.2f}%) matches ALPHA={ALPHA*100:.0f}% — consistent",
   f"Must match ALPHA={ALPHA}; difference={abs(actual_99_rate - ALPHA):.4f}")

# For Test 8 (K=148), verify that with 1% per-template failure rate,
# the expected number of failures (1.48) is WELL BELOW the threshold of 5.
# This means a perfect CSPRNG will pass the sub-proportion test ~99.6% of the time.
def binom_cdf(k, n, p):
    """P(X <= k) for X ~ Binomial(n, p)"""
    result = 0.0
    coef = 1.0
    for i in range(k + 1):
        if i > 0:
            coef *= (n - i + 1) / i
        result += coef * (p ** i) * ((1 - p) ** (n - i))
    return result

K8 = 148
_, min8_req, _ = check_sub_proportion(K8, K8)
max_allowed = K8 - min8_req  # = 148 - 143 = 5

# P(failures <= max_allowed) for perfect RNG at 1% failure rate
prob_pass_99 = binom_cdf(max_allowed, K8, 0.01)
prob_pass_95 = binom_cdf(max_allowed, K8, 0.05)

ok(prob_pass_99 > 0.99,
   f"T6-04  Test8 sub-proportion PASS probability with 99% CI = {prob_pass_99*100:.1f}% (must be >99%)",
   f"A perfect CSPRNG must almost always pass Test 8 sub-proportion")

ok(prob_pass_95 < 0.50,
   f"T6-05  Test8 sub-proportion PASS probability with 95% CI = {prob_pass_95*100:.1f}% (must be <50%)",
   f"OLD BEHAVIOUR: using 95% CI would fail Test 8 most of the time for any CSPRNG")

ok(prob_pass_99 > prob_pass_95,
   "T6-06  99% CI gives higher pass probability than 95% CI (99% is more appropriate)")

# Verify for Test 12 (K=8)
K12 = 8
_, min12_req, _ = check_sub_proportion(K12, K12)
max_allowed12 = K12 - min12_req
prob_pass12_99 = binom_cdf(max_allowed12, K12, 0.01)
prob_pass12_95 = binom_cdf(max_allowed12, K12, 0.05)

ok(prob_pass12_99 > 0.90,
   f"T6-07  Test12 sub-proportion PASS probability with 99% CI = {prob_pass12_99*100:.1f}% (must be >90%)")

ok(prob_pass12_99 > prob_pass12_95,
   "T6-08  Test12: 99% CI gives higher pass probability than 95% CI")

# ─── T7: build_results() ─────────────────────────────────────────────────────

section("T7 — build_results(): multi-proportion test structure (Tests 8, 12, 13)")

def make_parsed_row(test_dir, sub_idx, p_value, proportion):
    """Create a mock parsed row as parse_report() would produce."""
    from nist_analysis import TEST_DIRS
    test_idx = TEST_DIRS.index(test_dir) if test_dir in TEST_DIRS else 0
    return {
        "test_dir": test_dir,
        "test_idx": test_idx,
        "sub_idx": sub_idx,
        "p_value": p_value,
        "proportion": proportion,
        "bins": [10, 10, 10, 10, 10, 10, 10, 10, 10, 10],
    }


# ── Test 8: 148 templates, 11 of which fail 95% but only 3 fail 99%
#    (simulating the 4GB run data)
# Templates failing 99% bilateral: 10 (p=0.00088), 56 (p=0.00130), 68 (p=0.00498)
# Templates failing 95% only (pass 99%): 22,48,84,100,101,115,116,135

fail_99_vals = {10: 0.00088, 56: 0.00130, 68: 0.00498}   # 3 templates fail 99%
fail_95_only = {22: 0.98345, 48: 0.02199, 84: 0.98789,   # 8 templates fail 95% only
                100: 0.02199, 101: 0.01179, 115: 0.98345,
                116: 0.01559, 135: 0.98789}

rows_test8 = []
for i in range(1, 149):
    if i in fail_99_vals:
        p = fail_99_vals[i]
    elif i in fail_95_only:
        p = fail_95_only[i]
    else:
        p = 0.5  # healthy midpoint
    rows_test8.append(make_parsed_row("NonOverlappingTemplate", i, p, "99/100"))

results8 = build_results(rows_test8)

ok(8 in results8,   "T7-01  Test 8 present in results")
td8 = results8[8]
ok(len(td8["rows"]) == 148, "T7-02  Test 8 has 148 sub-results")

# overall_pass should use 99% bilateral (3 failures, threshold 143 → 145/148 PASS)
ok(td8["overall_pass"] == True,
   "T7-03  Test 8 overall_pass=True: 145/148 pass 99% CI (≥143 threshold)",
   f"Got overall_pass={td8['overall_pass']}, summary={td8.get('sub_proportion_summary','?')}")

# overall_pass_99 should also be PASS (same level as overall_pass)
ok(td8["overall_pass_99"] == "PASS",
   "T7-04  Test 8 overall_pass_99='PASS'")

# overall_pass_95 should be FAIL (11 failures at 95% level → 137/148 < 143)
ok(td8["overall_pass_95"] == "FAIL",
   "T7-05  Test 8 overall_pass_95='FAIL' (11 failures at 95% level — informational warning)",
   f"Got overall_pass_95={td8['overall_pass_95']}")

# The 3 templates failing 99% should have overall_pass=False
rows_by_idx = {r["sub_idx"]: r for r in td8["rows"]}
for t_idx in [10, 56, 68]:
    ok(rows_by_idx[t_idx]["overall_pass"] == False,
       f"T7-06  template-{t_idx} overall_pass=False (p={fail_99_vals[t_idx]}, fails 99% CI)")

# The 8 templates failing 95% ONLY should still have overall_pass=True
for t_idx in [22, 48, 84, 100]:
    r = rows_by_idx[t_idx]
    ok(r["overall_pass"] == True,
       f"T7-07  template-{t_idx} overall_pass=True (p={fail_95_only[t_idx]}, fails 95% but passes 99%)")
    ok(r["pass_95"] == "FAIL",
       f"T7-08  template-{t_idx} pass_95='FAIL' (displayed as warning)")
    ok(r["pass_99"] == "PASS",
       f"T7-09  template-{t_idx} pass_99='PASS' (passes the definitive 99% level)")

# ── Test 8 with 5+ templates failing 99% (should FAIL sub-proportion)
# Put 6 templates with p < 0.005
rows_test8_fail = []
for i in range(1, 149):
    if i <= 6:
        p = 0.001  # fails 99%
    else:
        p = 0.5    # passes everything
    rows_test8_fail.append(make_parsed_row("NonOverlappingTemplate", i, p, "99/100"))

results8_fail = build_results(rows_test8_fail)
ok(results8_fail[8]["overall_pass"] == False,
   "T7-10  Test 8 with 6 failing 99% templates → overall FAIL (142/148 < 143)")

# ── Test 12 (K=8): simulate 4GB data (all 8 states pass)
excursion_pvals_4gb = {
    1: 0.20407600, 2: 0.39245600, 3: 0.36414600, 4: 0.45279900,
    5: 0.26445800, 6: 0.95731900, 7: 0.84858800, 8: 0.36414600,
}
rows_test12_4gb = [
    make_parsed_row("RandomExcursions", i, excursion_pvals_4gb[i], "62/63")
    for i in range(1, 9)
]
results12_4gb = build_results(rows_test12_4gb)
ok(results12_4gb[12]["overall_pass"] == True,
   "T7-11  Test 12 (4GB data): 8/8 pass 99% → PASS")

# ── Test 12 (K=8): simulate 5GB data (x=+2 has p=0.01125 — passes 99%)
excursion_pvals_5gb = {
    1: 0.40709100, 2: 0.23276000, 3: 0.35048500, 4: 0.32418000,
    5: 0.77276000, 6: 0.01125000, 7: 0.80433700, 8: 0.03517400,
}
rows_test12_5gb = [
    make_parsed_row("RandomExcursions", i, excursion_pvals_5gb[i], "66/66")
    for i in range(1, 9)
]
results12_5gb = build_results(rows_test12_5gb)
ok(results12_5gb[12]["overall_pass"] == True,
   "T7-12  Test 12 (5GB data): x=+2 p=0.01125 passes 99% CI → 8/8 → PASS",
   f"p=0.01125 is in [0.005, 0.995], so passes 99% bilateral check")

# Under old 95% logic, x=+2 would fail: verify it DOES fail 95%
ok(assess_95(0.01125) == "FAIL",
   "T7-13  x=+2 p=0.01125 FAILS 95% CI — appears as warning in report but does not fail test")

# ── Test 13 (K=18): simulate 5GB data (all pass)
rows_test13 = [
    make_parsed_row("RandomExcursionsVariant", i, 0.5, "66/66")
    for i in range(1, 19)
]
results13 = build_results(rows_test13)
ok(results13[13]["overall_pass"] == True,
   "T7-14  Test 13 (18 sub-results, all healthy): PASS")

# ─── T8: find_failing_rows() ─────────────────────────────────────────────────

section("T8 — find_failing_rows(): escalation trigger logic")

# If Test 8 overall_pass=True → should NOT be in failing (no escalation needed)
failing8_pass = find_failing_rows(results8)
ok(8 not in failing8_pass,
   "T8-01  Test 8 with overall_pass=True → NOT in failing dict (no escalation)")

# If Test 8 overall_pass=False → should be in failing with sentinel [0]
failing8_fail = find_failing_rows(results8_fail)
ok(8 in failing8_fail,
   "T8-02  Test 8 with overall_pass=False → IS in failing dict")
ok(failing8_fail.get(8) == [0],
   "T8-03  Test 8 failing entry uses sentinel [0] (whole-test escalation)")

# Test 12 pass: not in failing
failing12_4gb = find_failing_rows(results12_4gb)
ok(12 not in failing12_4gb,
   "T8-04  Test 12 passing → NOT in failing dict")

# Simulate a standard test (test 1) failing
rows_test1_fail = [make_parsed_row("Frequency", 1, 0.001, "97/100")]  # p=0.001 fails 99%
results_t1 = build_results(rows_test1_fail)
failing_t1 = find_failing_rows(results_t1)
ok(1 in failing_t1,
   "T8-05  Standard test (Test 1) with p=0.001 → IS in failing dict")
ok(failing_t1.get(1) == [1],
   "T8-06  Standard test failing entry contains the sub_idx (not sentinel 0)")

# Standard test passing
rows_test1_pass = [make_parsed_row("Frequency", 1, 0.5, "99/100")]
results_t1_pass = build_results(rows_test1_pass)
failing_t1_pass = find_failing_rows(results_t1_pass)
ok(1 not in failing_t1_pass,
   "T8-07  Standard test (Test 1) with p=0.5 → NOT in failing dict")

# ─── T9: apply_decision_matrix() ─────────────────────────────────────────────

section("T9 — apply_decision_matrix(): FAIL/SUSPECT/PASS verdicts")

def row(pass_flag):
    return {"overall_pass": pass_flag}

# Three-outcome matrix: Baseline always FAIL (that's why we escalate)
# | Baseline | m=200 | m=300 | Verdict |
# | FAIL     | PASS  | PASS  | PASS    |
# | FAIL     | FAIL  | PASS  | SUSPECT |
# | FAIL     | FAIL  | FAIL  | FAIL    |

ok(apply_decision_matrix(row(False), row(True),  row(True))  == "PASS",
   "T9-01  FAIL/PASS/PASS → PASS")
ok(apply_decision_matrix(row(False), row(False), row(True))  == "SUSPECT",
   "T9-02  FAIL/FAIL/PASS → SUSPECT")
ok(apply_decision_matrix(row(False), row(False), row(False)) == "FAIL",
   "T9-03  FAIL/FAIL/FAIL → FAIL")

# Edge: m200 or m300 is None (escalation run produced no data)
ok(apply_decision_matrix(row(False), None, row(True))   == "SUSPECT",
   "T9-04  FAIL/None/PASS → SUSPECT (missing m200 treated as fail)")
ok(apply_decision_matrix(row(False), None, None)        == "FAIL",
   "T9-05  FAIL/None/None → FAIL")
ok(apply_decision_matrix(row(False), row(True), None)   == "FAIL",
   "T9-06  FAIL/PASS/None → FAIL (m300 missing = treated as fail)")

# ─── T10: End-to-end simulation ──────────────────────────────────────────────

section("T10 — End-to-end: simulated CSPRNG-like data through full pipeline")

# Simulate a good CSPRNG run: 148 templates for Test 8, where p-values are
# drawn from U[0,1] but we control the random seed for reproducibility.
# Expected outcome: nearly always PASS under 99% bilateral check.

import random

def simulate_test8_run(seed, n_fail_99=0, n_fail_95_only=0):
    """
    Build fake Test 8 results with controlled failures.
    n_fail_99: templates that fail the 99% bilateral check (p < 0.005)
    n_fail_95_only: templates that fail 95% but pass 99% (0.005 ≤ p < 0.025)
    """
    rng = random.Random(seed)
    rows = []
    for i in range(1, 149):
        if i <= n_fail_99:
            p = 0.001       # fails 99% bilateral
        elif i <= n_fail_99 + n_fail_95_only:
            p = 0.015       # fails 95% but passes 99%
        else:
            p = rng.uniform(0.05, 0.95)  # clearly healthy
        rows.append(make_parsed_row("NonOverlappingTemplate", i, p, "99/100"))
    return build_results(rows)

# Scenario A: Perfect RNG — no 99% failures → must PASS
results_perfect = simulate_test8_run(seed=1, n_fail_99=0, n_fail_95_only=0)
ok(results_perfect[8]["overall_pass"] == True,
   "T10-01  Perfect RNG (0 failures at any level) → PASS")

# Scenario B: 3 templates fail 99%, rest healthy → PASS (3 < 5 allowed)
results_3fail = simulate_test8_run(seed=2, n_fail_99=3)
ok(results_3fail[8]["overall_pass"] == True,
   "T10-02  3 templates fail 99% → PASS (145/148 ≥ 143)")

# Scenario C: 5 templates fail 99% → PASS (exactly at boundary: 143/148)
results_5fail = simulate_test8_run(seed=3, n_fail_99=5)
ok(results_5fail[8]["overall_pass"] == True,
   "T10-03  5 templates fail 99% → PASS (143/148 ≥ 143, exactly at threshold)")

# Scenario D: 6 templates fail 99% → FAIL (142/148 < 143)
results_6fail = simulate_test8_run(seed=4, n_fail_99=6)
ok(results_6fail[8]["overall_pass"] == False,
   "T10-04  6 templates fail 99% → FAIL (142/148 < 143)")

# Scenario E: 11 templates fail 95% only (as in 4GB run), 0 fail 99% → PASS
results_4gb_sim = simulate_test8_run(seed=5, n_fail_99=0, n_fail_95_only=11)
ok(results_4gb_sim[8]["overall_pass"] == True,
   "T10-05  11 templates fail 95% only (0 fail 99%) → PASS (simulates 4GB result)")
ok(results_4gb_sim[8]["overall_pass_95"] == "FAIL",
   "T10-06  But overall_pass_95='FAIL' (informational warning displayed in report)")

# Scenario F: 3 fail 99% + 8 fail 95% only → PASS (same as 4GB actual result)
results_4gb_actual = simulate_test8_run(seed=6, n_fail_99=3, n_fail_95_only=8)
ok(results_4gb_actual[8]["overall_pass"] == True,
   "T10-07  3 fail 99% + 8 fail 95% only → PASS (145/148 ≥ 143)")

# Scenario G: Test 12, 5GB data — x=+2 p=0.01125 (fails 95% only) → PASS
rows_12_5gb_sim = [
    make_parsed_row("RandomExcursions", i,
                    0.01125 if i == 6 else 0.5,   # state x=+2 → sub_idx 6
                    "66/66")
    for i in range(1, 9)
]
results_12_5gb_sim = build_results(rows_12_5gb_sim)
ok(results_12_5gb_sim[12]["overall_pass"] == True,
   "T10-08  Test 12: x=+2 p=0.01125 passes 99% → 8/8 → PASS (was false-FAIL with 95% logic)")

# Scenario H: Statistical consistency over 100 simulated runs
# With truly uniform p-values, Test 8 should pass at 99% CI almost every time.
pass_count_sim = 0
N_RUNS = 100
random.seed(999)
for run in range(N_RUNS):
    rows_run = []
    for i in range(1, 149):
        p = random.random()  # truly uniform U[0,1]
        rows_run.append(make_parsed_row("NonOverlappingTemplate", i, p, "99/100"))
    res = build_results(rows_run)
    if res[8]["overall_pass"]:
        pass_count_sim += 1

pass_rate_sim = pass_count_sim / N_RUNS
ok(pass_rate_sim >= 0.90,
   f"T10-09  Over {N_RUNS} simulated runs with uniform p-values, "
   f"Test 8 passes {pass_count_sim}/{N_RUNS} = {pass_rate_sim*100:.0f}% of the time (must be ≥90%)",
   f"Expected ~99.6% pass rate. Got {pass_rate_sim*100:.1f}%.")

# ─── Final summary ────────────────────────────────────────────────────────────

section("SUMMARY")

total = _pass + _fail
print(f"\n  Tests run   : {total}")
print(f"  PASSED      : {_pass}")
print(f"  FAILED      : {_fail}")

if _fail == 0:
    print("\n  ✓  ALL TESTS PASSED — script follows NIST SP 800-22 and RNG Labs guidelines")
else:
    print(f"\n  ✗  {_fail} TEST(S) FAILED:")
    for e in _errors:
        print(f"       - {e}")

print()
sys.exit(0 if _fail == 0 else 1)
