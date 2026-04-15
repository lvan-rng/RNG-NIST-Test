#!/usr/bin/env python3
"""
NIST SP 800-22 Statistical Test Suite - Automated Analysis Script
=================================================================
Automates the official NIST STS (sts-2.1.2) assess tool by piping
interactive prompts via stdin, then parses results and produces a
clean table with 95% and 99% CI pass/fail assessment.

Implements the RNG Labs NIST Failed Test Re-Assessment Strategy:
  - Baseline run at m=100 (all 15 tests)
  - If any row FAILS (uniformity p-value OR proportion):
      -> Re-run failing tests at m=200 (Level 1)
      -> Re-run failing tests at m=300 (Level 2)
  - Three-outcome decision matrix:
      FAIL / PASS / PASS  ->  PASS
      FAIL / FAIL / PASS  ->  SUSPECT
      FAIL / FAIL / FAIL  ->  FAIL

Usage:
    ./nist_analysis.py -n 1000000 -m 100 -i rng_4gb.bin
    ./nist_analysis.py -n 1000000 -m 100 -i rng_4gb.bin --assess-path /usr/local/bin/assess

Requirements:
    - Official NIST STS sts-2.1.2 compiled 'assess' binary
    - Python 3.6+
    - Input file in raw binary format (each byte = 8 bits)
"""

import argparse
import math
import os
import re
import subprocess
import sys
import shutil
import time
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MIN_SEQUENCE_LENGTH = 1_000_000
MIN_BITSTREAMS = 100
MIN_FILE_SIZE_MB = 125
MIN_FILE_SIZE_BYTES = MIN_FILE_SIZE_MB * 1024 * 1024

ESCALATION_M1 = 200
ESCALATION_M2 = 300

ALPHA = 0.01  # NIST default significance level

# Test indices with parameterized prompts in fixParameters()
PARAMETERIZED_TESTS = {2, 8, 9, 11, 14, 15}

# NIST STS test directory names (index 1-15, matching testNames[] in decls.h)
TEST_DIRS = [
    "",                          # 0 - placeholder
    "Frequency",                 # 1
    "BlockFrequency",            # 2
    "CumulativeSums",            # 3
    "Runs",                      # 4
    "LongestRun",                # 5
    "Rank",                      # 6
    "FFT",                       # 7
    "NonOverlappingTemplate",    # 8
    "OverlappingTemplate",       # 9
    "Universal",                 # 10
    "ApproximateEntropy",        # 11
    "RandomExcursions",          # 12
    "RandomExcursionsVariant",   # 13
    "Serial",                    # 14
    "LinearComplexity",          # 15
]

# Human-readable test names for the report
TEST_NAMES = [
    "",                                         # 0
    "Frequency (Monobit) Test",                 # 1
    "Frequency Test within a Block",            # 2
    "Cumulative Sums Test",                     # 3
    "Runs Test",                                # 4
    "Tests for the Longest-Run-of-Ones",        # 5
    "Binary Matrix Rank Test",                  # 6
    "Discrete Fourier Transform (Spectral)",    # 7
    "Non-overlapping Template Matching Test",   # 8
    "Overlapping Template Matching Test",       # 9
    "Maurer's Universal Statistical Test",      # 10
    "Approximate Entropy Test",                 # 11
    "Random Excursions Test",                   # 12
    "Random Excursions Variant Test",           # 13
    "Serial Test",                              # 14
    "Linear Complexity Test",                   # 15
]

# Multi-value test sub-result labels
MULTI_VALUE_LABELS = {
    3:  ["Forward", "Reverse"],
    8:  None,  # template-N (up to 148)
    12: ["x=-4", "x=-3", "x=-2", "x=-1", "x=+1", "x=+2", "x=+3", "x=+4"],
    13: ["x=-9", "x=-8", "x=-7", "x=-6", "x=-5", "x=-4", "x=-3", "x=-2", "x=-1",
         "x=+1", "x=+2", "x=+3", "x=+4", "x=+5", "x=+6", "x=+7", "x=+8", "x=+9"],
    14: ["p-value 1", "p-value 2"],
}

# Tests that produce many independent sub-results (one per template/state).
# For these, failing a small number of sub-results is statistically expected
# at α=0.01.  Their overall pass/fail is determined by whether the PROPORTION
# of passing sub-results meets the same Bernoulli threshold NIST uses for
# bitstreams — rather than requiring every single sub-result to pass.
#
#   Test  8  – Non-overlapping Template Matching  (up to 148 templates)
#   Test 12  – Random Excursions                  (8 states: ±1..±4)
#   Test 13  – Random Excursions Variant          (18 states: ±1..±9)
MULTI_PROPORTION_TESTS = {8, 12, 13}

# Canonical representative sub-result for each multi-proportion test,
# following NIST SP 800-22 Appendix B conventions:
#   Test  8 – first template (template-1, i.e. 000000001)
#   Test 12 – x = +1   (matches NIST Appendix B example)
#   Test 13 – x = -1   (matches NIST Appendix B example)
# This label is used when ONE representative row is shown in the summary table.
REPRESENTATIVE_LABELS = {
    12: "x=+1",
    13: "x=-1",
    # Test 8 has no fixed label — rows[0] (template-1) is used as the fallback
}


def get_representative_row(test_idx, rows):
    """
    Return the NIST-Appendix-B canonical representative sub-result for tests
    with many sub-results (8, 12, 13).

    Convention (mirrors NIST Appendix B):
      Test  8 : first template in result order  (template-1 = B=000000001)
      Test 12 : x = +1
      Test 13 : x = -1

    For all other tests the first row is returned unchanged.
    """
    if not rows:
        return None
    target = REPRESENTATIVE_LABELS.get(test_idx)
    if target:
        for r in rows:
            if r.get("label") == target:
                return r
    return rows[0]


# ─────────────────────────────────────────────────────────────────────────────
# P-value and proportion assessment
# ─────────────────────────────────────────────────────────────────────────────

def assess_95(p):
    """95% two-tailed: PASS if 0.025 <= p <= 0.975"""
    if p is None:
        return "N/A"
    return "PASS" if 0.025 <= p <= 0.975 else "FAIL"


def assess_99(p):
    """99% two-tailed: PASS if 0.005 <= p <= 0.995"""
    if p is None:
        return "N/A"
    return "PASS" if 0.005 <= p <= 0.995 else "FAIL"


def check_proportion(proportion_str):
    """
    Check if proportion meets the NIST minimum threshold.
    Proportion format: "passCount/sampleSize" e.g. "97/100"

    Applies the same NIST Bernoulli formula used in check_sub_proportion(),
    with ceiling rounding for the same reason (the count of passing sequences
    is always a whole number, so the threshold must be the smallest integer
    that satisfies the inequality):

        min_pass_float = (p_hat - 3 * sqrt(p_hat * alpha / m)) * m
        min_pass       = ceil(min_pass_float)
        where p_hat = 1 - alpha = 0.99

    Example at m=100, α=0.01:
        min_pass_float = 96.015  →  min_pass = ceil(96.015) = 97

    Returns True if passes, False if fails.

    Reference: NIST SP 800-22 Rev. 1a, Section 4.2.1
    """
    if not proportion_str or proportion_str in ("N/A", "------", "----"):
        return True

    match = re.match(r"(\d+)/(\d+)", proportion_str)
    if not match:
        return True

    pass_count = int(match.group(1))
    sample_size = int(match.group(2))

    if sample_size == 0:
        return True

    p_hat = 1.0 - ALPHA
    min_pass_float = (p_hat - 3.0 * math.sqrt(p_hat * ALPHA / sample_size)) * sample_size
    min_pass = math.ceil(min_pass_float)

    return pass_count >= min_pass


def row_passes(row):
    """
    Determine the definitive pass/fail for a single parsed row, combining:

      1) Uniformity p-value within the 99% bilateral CI
         (PASS if 0.005 <= p <= 0.995)

      2) Proportion of passing sequences meets the NIST minimum threshold

    WHY 99% (not 95%) drives overall_pass
    ──────────────────────────────────────
    RNG Labs applies two CI levels:
      • 95% bilateral (0.025 ≤ p ≤ 0.975): the STRICTER of the two levels,
        displayed as a visual warning column in every report.
      • 99% bilateral (0.005 ≤ p ≤ 0.995): the DEFINITIVE pass/fail level,
        aligned with NIST's significance level α = 0.01.

    The 99% bilateral window has ~1% expected failure probability for a
    perfectly uniform p-value distribution.  This matches the α = 0.01 that
    the Bernoulli sub-proportion formula in check_sub_proportion() is
    calibrated for.

    Using the 95% check (5% failure rate) here while the Bernoulli threshold
    assumes 1% would produce structurally guaranteed false positives on
    Test 8 (K=148): expected 7.4 failures vs threshold of 5, meaning a
    perfect CSPRNG would fail that sub-proportion check ~75% of the time.

    The 95% CI column continues to be computed and displayed separately as an
    informational stricter indicator; it does NOT drive escalation or the
    definitive PASS/FAIL verdict.

    Reference: NIST SP 800-22 Rev. 1a, Section 4.2; RNG Labs Assessment Policy
    """
    p = row["p_value"]
    if p is None:
        return True  # Can't assess (e.g. Random Excursions with no cycles)

    if assess_99(p) == "FAIL":
        return False

    if not check_proportion(row["proportion"]):
        return False

    return True


def check_sub_proportion(num_passing, num_total, alpha=ALPHA):
    """
    Proportion-of-sub-tests check for MULTI_PROPORTION_TESTS (tests 8, 12, 13).

    For tests that produce K independent sub-results (one per template or
    excursion state), failing a small number of sub-results is statistically
    expected even for a perfect RNG.  This function applies the same Bernoulli
    proportion formula that NIST SP 800-22 uses for bitstreams, but at the
    sub-result level:

        min_passing_float = (p_hat - 3 * sqrt(p_hat * alpha / K)) * K
        min_passing       = ceil(min_passing_float)
        where p_hat = 1 - alpha

    The continuous formula result is always rounded UP (ceiling) to the next
    integer.  This is the only mathematically correct direction: since the
    count of passing sub-tests is always a whole number, "you need >= 7.076
    sub-tests passing" is exactly equivalent to "you need >= 8 sub-tests
    passing" (the smallest integer that satisfies the inequality).  Rounding
    down would silently loosen the threshold below what the NIST formula
    requires; normal (half-up) rounding would also round 7.076 down to 7,
    creating the same problem.  Ceiling is therefore both the mathematically
    precise and the most conservative choice for integer counts.

    Computed thresholds at α=0.01:
      Test  8  (K=148) : ceil(142.889) = 143  →  max 5  failures allowed
      Test 12  (K=8)   : ceil(7.076)   = 8    →  max 0  failures allowed
      Test 13  (K=18)  : ceil(16.554)  = 17   →  max 1  failure  allowed

    Returns (passes: bool, min_required_int: int, summary_str: str).

    Reference: NIST SP 800-22 Rev. 1a, Section 4.2.1
    """
    if num_total == 0:
        return True, 0, "N/A"

    p_hat = 1.0 - alpha
    min_required_float = (p_hat - 3.0 * math.sqrt(p_hat * alpha / num_total)) * num_total
    min_required = math.ceil(min_required_float)
    passes = num_passing >= min_required
    summary = f"{num_passing}/{num_total} (min {min_required})"
    return passes, min_required, summary


# ─────────────────────────────────────────────────────────────────────────────
# Input validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_inputs(args):
    """Validate all input parameters. Exit on error."""
    errors = []
    warnings = []
    fmt = getattr(args, 'f', 1)       # default binary if attribute missing
    test_mode = getattr(args, 'test', False)

    if args.n < MIN_SEQUENCE_LENGTH:
        errors.append(
            f"Sequence length n={args.n} is below minimum {MIN_SEQUENCE_LENGTH:,}. "
            f"NIST requires n >= {MIN_SEQUENCE_LENGTH:,} for full-suite execution."
        )

    if test_mode:
        # --test bypasses the m restriction entirely (e.g. m=1 for Appendix B validation)
        if args.m < 1:
            errors.append("Number of bitstreams m must be at least 1.")
    elif fmt == 1:
        # Binary mode: enforce minimum bitstreams for statistically stable reporting
        if args.m < MIN_BITSTREAMS:
            errors.append(
                f"Number of bitstreams m={args.m} is below minimum {MIN_BITSTREAMS}. "
                f"RNG Labs requires m >= {MIN_BITSTREAMS} for stable reporting."
            )
    else:
        # ASCII mode: m < 100 is allowed (e.g. m=1 for NIST Appendix B validation)
        if args.m < 1:
            errors.append("Number of bitstreams m must be at least 1.")
        elif args.m < MIN_BITSTREAMS:
            warnings.append(
                f"m={args.m} is below the recommended minimum of {MIN_BITSTREAMS}. "
                f"Statistical reporting is only meaningful for m >= {MIN_BITSTREAMS}. "
                f"For NIST Appendix B reference validation, m=1 is intentional."
            )

    if not os.path.isfile(args.i):
        errors.append(f"Input file not found: {args.i}")
    else:
        file_size = os.path.getsize(args.i)
        file_size_mb = file_size / (1024 * 1024)

        if fmt == 1:
            # Binary: each byte = 8 bits
            # Skip the production-size floor in --test mode (reference files are small)
            if not test_mode and file_size < MIN_FILE_SIZE_BYTES:
                errors.append(
                    f"Input file size {file_size_mb:.1f} MB is below minimum {MIN_FILE_SIZE_MB} MB. "
                    f"File: {args.i}"
                )
            total_bits_available = file_size * 8
        else:
            # ASCII: each byte = 1 bit (each character is '0' or '1')
            total_bits_available = file_size

        total_bits_needed = args.n * args.m
        if total_bits_available < total_bits_needed:
            errors.append(
                f"Input file has {total_bits_available:,} bits but "
                f"n={args.n:,} x m={args.m} = {total_bits_needed:,} bits required."
            )

        # Escalation capacity check only applies to binary production runs
        # (not in --test mode, which deliberately uses small reference files)
        if fmt == 1 and not test_mode:
            escalation_bits = args.n * ESCALATION_M2
            if total_bits_available < escalation_bits:
                errors.append(
                    f"Input file has {total_bits_available:,} bits but escalation to m={ESCALATION_M2} "
                    f"requires n={args.n:,} x m={ESCALATION_M2} = {escalation_bits:,} bits."
                )

    if warnings:
        print("\n  NOTICE:")
        for w in warnings:
            print(f"  ⚠  {w}")

    if errors:
        print("\n ERROR: Input validation failed\n")
        for e in errors:
            print(f"  - {e}")
        print()
        sys.exit(1)


def resolve_assess_real_path(assess_path):
    """
    Resolve symlinks to find the real assess binary location.
    Returns the real absolute path (following all symlinks).
    """
    return os.path.realpath(assess_path)


def get_assess_working_dir(assess_path):
    """
    Get the working directory for running assess.
    The NIST STS binary needs to run from its installation directory
    (where templates/ and experiments/ directories are located).
    We resolve symlinks to find the real location.
    """
    real_path = resolve_assess_real_path(assess_path)
    return os.path.dirname(real_path)


def find_assess_binary(assess_path):
    """Locate the assess binary."""
    if assess_path:
        if os.path.isfile(assess_path) and os.access(assess_path, os.X_OK):
            return os.path.abspath(assess_path)
        print(f"\n ERROR: assess binary not found or not executable: {assess_path}")
        sys.exit(1)

    # Check PATH first (covers symlink at /usr/local/bin/assess)
    result = shutil.which("assess")
    if result:
        return os.path.abspath(result)

    search_paths = [
        "/usr/local/bin/assess",
        "/home/ubuntu/tools/sts-2.1.2/sts-2.1.2/assess",
        "/home/ubuntu/tools/sts-2.1.2/assess",
        "/home/ubuntu/tools/nist-sts/sts-2.1.2/assess",
        os.path.expanduser("~/tools/nist-sts/sts-2.1.2/assess"),
        os.path.expanduser("~/tools/sts-2.1.2/assess"),
        "./assess",
        "../assess",
    ]
    for p in search_paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.abspath(p)

    print("\n ERROR: 'assess' binary not found.")
    print("  Use --assess-path to specify the location.")
    print("  Example: --assess-path /home/ubuntu/tools/sts-2.1.2/assess")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Stdin construction for assess interactive prompts
# ─────────────────────────────────────────────────────────────────────────────

def build_test_mask(test_indices):
    """
    Build a 15-character test selection mask.
    test_indices: set of 1-based test indices (e.g., {4, 8, 12}).
    Returns e.g. "000100010001000"
    """
    mask = ['0'] * 15
    for idx in test_indices:
        mask[idx - 1] = '1'
    return ''.join(mask)


def needs_fix_parameters(test_mask):
    """
    Check if fixParameters() will prompt based on selected tests.
    fixParameters prompts only if any parameterized test is selected.
    """
    for i, c in enumerate(test_mask):
        if c == '1' and (i + 1) in PARAMETERIZED_TESTS:
            return True
    return False


def build_stdin_all_tests(input_file, m, fmt=1):
    """
    Build stdin for running all 15 tests.

    Prompt sequence:
      0              <- [0] Input File
      <filepath>     <- file path
      1              <- all tests
      0              <- fixParameters: continue (always needed since all tests include parameterized)
      <m>            <- How many bitstreams?
      <fmt>          <- [0] ASCII  or  [1] Binary (default: 1=Binary)
    """
    return "\n".join([
        "0",
        os.path.abspath(input_file),
        "1",
        "0",
        str(m),
        str(fmt),
    ]) + "\n"


def build_stdin_targeted(input_file, m, test_mask, fmt=1):
    """
    Build stdin for running specific tests only (escalation re-runs).

    Prompt sequence:
      0              <- [0] Input File
      <filepath>     <- file path
      0              <- NOT all tests
      <15 digits>    <- test selection mask
      [0]            <- fixParameters: continue (only if any parameterized test selected)
      <m>            <- How many bitstreams?
      <fmt>          <- [0] ASCII  or  [1] Binary (default: 1=Binary)
    """
    lines = [
        "0",
        os.path.abspath(input_file),
        "0",
        test_mask,
    ]
    if needs_fix_parameters(test_mask):
        lines.append("0")
    lines.extend([
        str(m),
        str(fmt),
    ])
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Run NIST STS
# ─────────────────────────────────────────────────────────────────────────────

def run_assess(assess_path, input_file, n, m, test_mask=None, label="Baseline",
               report_dir=None, fmt=1):
    """
    Run the official NIST STS assess tool by piping interactive prompts
    via stdin. Returns the assess working directory (real path, symlink resolved).

    test_mask: None for all tests, or "000000010000000" for targeted.
    report_dir: local directory to save run logs (optional).
    fmt: 0=ASCII, 1=Binary (default: 1).
    """
    # Resolve symlinks to find the real sts-2.1.2 installation directory
    # (where templates/ and experiments/ directories live)
    assess_dir = get_assess_working_dir(assess_path)

    if test_mask is None:
        stdin_input = build_stdin_all_tests(input_file, m, fmt=fmt)
        tests_desc = "all 15 tests"
    else:
        stdin_input = build_stdin_targeted(input_file, m, test_mask, fmt=fmt)
        selected = [str(i + 1) for i, c in enumerate(test_mask) if c == '1']
        tests_desc = f"tests {','.join(selected)}"

    # Use the real binary path (resolved from symlink)
    real_assess = resolve_assess_real_path(assess_path)
    cmd = [real_assess, str(n)]

    print(f"\n  [{label}] Running NIST STS: n={n:,}  m={m}  {tests_desc}")
    print(f"  [{label}] CWD: {assess_dir}")

    start_time = time.time()

    try:
        result = subprocess.run(
            cmd,
            cwd=assess_dir,
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=14400,
        )
    except subprocess.TimeoutExpired:
        print(f"  ERROR: NIST STS timed out after 4 hours ({label}).")
        sys.exit(1)
    except FileNotFoundError:
        print(f"  ERROR: Could not execute: {real_assess}")
        sys.exit(1)

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = elapsed % 60
    print(f"  [{label}] Completed in {minutes}m {seconds:.1f}s")

    if result.returncode not in (0, 1):
        print(f"  WARNING: assess returned exit code {result.returncode}")
        if result.stderr:
            print(f"  stderr: {result.stderr[:300]}")

    # Save raw log to both assess_dir and local report_dir
    log_name = f"nist_run_{label.lower().replace('=', '').replace(' ', '_')}.log"
    log_content = (
        f"Label: {label}\n"
        f"Command: {' '.join(cmd)}\n"
        f"Working dir: {assess_dir}\n"
        f"Exit code: {result.returncode}\n"
        f"Elapsed: {elapsed:.1f}s\n\n"
        f"=== STDIN ===\n{stdin_input}\n"
        f"=== STDOUT ===\n{result.stdout or '(empty)'}\n\n"
        f"=== STDERR ===\n{result.stderr or '(empty)'}"
    )

    # Save to assess dir
    with open(os.path.join(assess_dir, log_name), "w") as f:
        f.write(log_content)

    # Also save to local report dir if specified
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, log_name), "w") as f:
            f.write(log_content)

    return assess_dir


# ─────────────────────────────────────────────────────────────────────────────
# Parse finalAnalysisReport.txt
# ─────────────────────────────────────────────────────────────────────────────

def parse_report(assess_dir):
    """
    Parse finalAnalysisReport.txt into a list of row dicts.

    Each data row format:
      C1 C2 ... C10  P-VALUE  PROPORTION  STATISTICAL_TEST

    Returns list of:
      {"test_dir": str, "test_idx": int, "sub_idx": int,
       "p_value": float|None, "proportion": str, "bins": list}
    """
    report_path = os.path.join(
        assess_dir, "experiments", "AlgorithmTesting", "finalAnalysisReport.txt"
    )

    if not os.path.isfile(report_path):
        print(f"\n  ERROR: finalAnalysisReport.txt not found at: {report_path}")
        sys.exit(1)

    with open(report_path, "r") as f:
        lines = f.readlines()

    rows = []
    current_test = None
    sub_counter = 0

    for line in lines:
        stripped = line.strip()

        # Skip non-data lines
        if not stripped or stripped.startswith("-") or stripped.startswith("C1") or \
           stripped.startswith("RESULTS") or stripped.startswith("generator") or \
           stripped.startswith("The minimum") or stripped.startswith("For further") or \
           stripped.startswith("random excursion") or stripped.startswith("sample size"):
            continue

        parts = stripped.split()
        if len(parts) < 13:
            continue

        try:
            bins = [int(parts[i]) for i in range(10)]
        except ValueError:
            continue

        # P-VALUE (may have * suffix for uniformity < 0.0001)
        p_val_str = parts[10].replace("*", "").strip()
        if p_val_str == "----":
            p_value = None
        else:
            try:
                p_value = float(p_val_str)
            except ValueError:
                p_value = None

        # PROPORTION (format: "99/100", may have * suffix)
        prop_str = parts[11].replace("*", "").strip()

        # STATISTICAL TEST (remaining columns)
        test_dir = " ".join(parts[12:])

        # Look up test index
        if test_dir in TEST_DIRS:
            test_idx = TEST_DIRS.index(test_dir)
        else:
            test_idx = 0

        # Track sub-index for multi-value tests
        if test_dir != current_test:
            current_test = test_dir
            sub_counter = 1
        else:
            sub_counter += 1

        rows.append({
            "test_dir": test_dir,
            "test_idx": test_idx,
            "sub_idx": sub_counter,
            "p_value": p_value,
            "proportion": prop_str,
            "bins": bins,
        })

    return rows


def find_nonoverlay_template_index(assess_dir, template="000000001"):
    """
    Return the 0-based line index (= results.txt row) for a specific bit template
    inside the NIST STS templates/template9 file.

    File format (NIST STS 2.1.2):
        148            ← first line: count of templates
        9 000000001    ← template length + bit pattern (one per line)
        9 000000011
        ...

    The p-values in NonOverlappingTemplate/results.txt are written in the same
    order as templates in this file, so the 0-based template position equals the
    0-based index into results.txt.

    Returns 0 as the fallback if the file does not exist or the template is not
    found (the standard NIST STS 2.1.2 distribution always has "000000001" first).
    """
    template_path = os.path.join(assess_dir, "templates", "template9")
    if not os.path.isfile(template_path):
        return 0
    try:
        with open(template_path, "r") as fh:
            lines = fh.readlines()
        # First line is the count header; templates begin on line index 1
        for i, line in enumerate(lines[1:]):
            parts = line.strip().split()
            if parts and parts[-1] == template:
                return i   # i is already 0-based within the template list
    except OSError:
        pass
    return 0  # fallback: template not found, assume index 0


def read_per_stream_pvalues(assess_dir):
    """
    Read per-stream test p-values from NIST STS individual test result files.

    After assess completes, each test writes its computed p-value(s) to a
    per-test results.txt file at:
        <assess_dir>/experiments/AlgorithmTesting/<TestDir>/results.txt

    These per-stream p-values are what NIST SP 800-22 Appendix B documents.
    They differ from the P-VALUE column in finalAnalysisReport.txt, which
    holds the chi-squared uniformity value across m streams (degenerate for
    m=1).  For m=1 validation against Appendix B, always use this function.

    Parsing format (NIST STS sts-2.1.2 results.txt):
      Each line contains one raw p-value printed with %f (e.g. "0.578011").
      Multi-value tests write one p-value per line:
        CumulativeSums : 2 lines (forward, reverse)
        Serial         : 2 lines (psi^2_m, psi^2_{m-1})
        RandomExcursions        : 8 lines  (x = -4...-1, +1...+4)
        RandomExcursionsVariant : 18 lines (x = -9...-1, +1...+9)

    Returns a dict keyed by test directory name (from TEST_DIRS):
      { "Frequency": [0.578011], "CumulativeSums": [0.628308, 0.663369], ... }

    Tests with no results.txt (e.g. RandomExcursions with insufficient cycles)
    are absent from the returned dict.
    """
    result = {}
    exp_base = os.path.join(assess_dir, "experiments", "AlgorithmTesting")

    for test_dir in TEST_DIRS[1:]:   # skip placeholder at index 0
        results_path = os.path.join(exp_base, test_dir, "results.txt")
        if not os.path.isfile(results_path):
            continue

        pvalues = []
        with open(results_path, "r") as f:
            for line in f:
                # NIST STS results.txt format: one raw float per line ("0.578011")
                # Match lines that are purely a floating-point number (with
                # optional whitespace), ignoring any non-numeric lines.
                m = re.match(
                    r'^\s*([0-9]+\.[0-9]+(?:[eE][+-]?\d+)?)\s*$',
                    line
                )
                if m:
                    try:
                        pvalues.append(float(m.group(1)))
                    except ValueError:
                        pass

        if pvalues:
            # For NonOverlappingTemplate, results.txt contains 148 p-values (one
            # per template in templates/template9).  Appendix B documents the
            # p-value for template B="000000001" specifically.  Locate that
            # template's position in the templates file and expose only that
            # single value at index 0 so the --test comparison is unambiguous.
            if test_dir == "NonOverlappingTemplate":
                tmpl_idx = find_nonoverlay_template_index(assess_dir)
                if tmpl_idx < len(pvalues):
                    result[test_dir] = [pvalues[tmpl_idx]]
                else:
                    result[test_dir] = [pvalues[0]]
            else:
                result[test_dir] = pvalues

    return result


def get_sub_label(test_idx, sub_idx):
    """Get human-readable label for a sub-result row."""
    labels = MULTI_VALUE_LABELS.get(test_idx)
    if labels and sub_idx <= len(labels):
        return labels[sub_idx - 1]
    elif test_idx == 8:
        return f"template-{sub_idx}"
    else:
        return f"sub-{sub_idx}"


# ─────────────────────────────────────────────────────────────────────────────
# Build structured results from parsed rows
# ─────────────────────────────────────────────────────────────────────────────

def build_results(parsed_rows):
    """
    Group parsed rows by test and compute per-row pass/fail for both
    dimensions (uniformity p-value CI + proportion).

    Returns dict keyed by test_idx:
      { test_idx: {
          "test_idx": int,
          "test_name": str,
          "rows": [  # list of sub-result rows
            {"sub_idx": int, "label": str, "p_value": float, "proportion": str,
             "pass_95": str, "pass_99": str, "prop_pass": bool, "overall_pass": bool}
          ],
          "overall_pass_95": str,
          "overall_pass_99": str,
          "overall_pass": bool,
        }
      }
    """
    results = {}

    for row in parsed_rows:
        test_idx = row["test_idx"]
        if test_idx == 0:
            continue

        if test_idx not in results:
            results[test_idx] = {
                "test_idx": test_idx,
                "test_name": TEST_NAMES[test_idx],
                "rows": [],
            }

        is_multi = test_idx in MULTI_VALUE_LABELS
        label = get_sub_label(test_idx, row["sub_idx"]) if is_multi else None

        p95 = assess_95(row["p_value"])
        p99 = assess_99(row["p_value"])
        prop_pass = check_proportion(row["proportion"])
        overall = row_passes(row)

        results[test_idx]["rows"].append({
            "sub_idx": row["sub_idx"],
            "label": label,
            "p_value": row["p_value"],
            "proportion": row["proportion"],
            "pass_95": p95,
            "pass_99": p99,
            "prop_pass": prop_pass,
            "overall_pass": overall,
        })

    # Compute overall per-test status
    for test_idx, test_data in results.items():
        rows = test_data["rows"]

        if test_idx in MULTI_PROPORTION_TESTS:
            # ── Proportion-of-sub-tests approach ────────────────────────────
            # Tests 8, 12, 13 produce one p-value per template/excursion-state.
            # By random chance ~1% of sub-results will fail even for a perfect
            # RNG (the 99% bilateral CI has ~1% failure probability, matching
            # α = 0.01), so we apply the same Bernoulli proportion formula that
            # NIST uses for bitstreams — but now at the sub-result level.
            #
            # A single failing template out of 148 does NOT fail the whole test;
            # the question is whether *enough* sub-results pass.
            #
            # passing_both counts sub-results where overall_pass is True.
            # overall_pass uses the 99% bilateral CI (row_passes), so the
            # expected failure rate per sub-result is ~1%, consistent with
            # the α = 0.01 calibration of check_sub_proportion().
            #
            # passing_95 / passing_99 are computed separately for the
            # display-only overall_pass_95 / overall_pass_99 columns.
            total_subs = len(rows)

            # Count sub-results passing at each confidence level
            passing_both  = sum(1 for r in rows if r["overall_pass"])
            passing_95    = sum(1 for r in rows if r["pass_95"] == "PASS"
                                                 and check_proportion(r["proportion"]))
            passing_99    = sum(1 for r in rows if r["pass_99"] == "PASS"
                                                 and check_proportion(r["proportion"]))

            sub_pass_both, _, both_summary  = check_sub_proportion(passing_both, total_subs)
            sub_pass_95, _, _               = check_sub_proportion(passing_95,  total_subs)
            sub_pass_99, _, _               = check_sub_proportion(passing_99,  total_subs)

            test_data["overall_pass"]    = sub_pass_both
            test_data["overall_pass_95"] = "PASS" if sub_pass_95 else "FAIL"
            test_data["overall_pass_99"] = "PASS" if sub_pass_99 else "FAIL"

            # Store sub-proportion summary for display
            test_data["sub_proportion_summary"] = both_summary
            test_data["sub_passing_count"]      = passing_both
            test_data["sub_total_count"]        = total_subs
        else:
            # ── Standard approach: all sub-results must pass ─────────────────
            all_pass    = all(r["overall_pass"] for r in rows)
            any_fail_95 = any(r["pass_95"] == "FAIL" for r in rows)
            any_fail_99 = any(r["pass_99"] == "FAIL" for r in rows)
            test_data["overall_pass_95"] = "FAIL" if any_fail_95 else "PASS"
            test_data["overall_pass_99"] = "FAIL" if any_fail_99 else "PASS"
            test_data["overall_pass"]    = all_pass

    return results


def find_failing_rows(results):
    """
    Find all tests/rows that fail and need escalation.
    Returns: dict { test_idx: [sub_idx, ...] }

    For MULTI_PROPORTION_TESTS (8, 12, 13):
        Only add to failing if the overall proportion-of-sub-tests check
        failed.  Individual sub-row failures within the acceptable threshold
        are statistically expected and do NOT trigger escalation.
        sub_idx list is [0] (sentinel) to indicate the whole test failed.

    For all other tests:
        Any individually failing sub-row triggers escalation (existing logic).
    """
    failing = {}
    for test_idx, test_data in results.items():
        if test_idx in MULTI_PROPORTION_TESTS:
            # Only escalate if the proportion-of-sub-tests threshold is breached
            if not test_data["overall_pass"]:
                failing[test_idx] = [0]  # 0 = whole test, not a specific sub-row
        else:
            for r in test_data["rows"]:
                if not r["overall_pass"]:
                    if test_idx not in failing:
                        failing[test_idx] = []
                    failing[test_idx].append(r["sub_idx"])
    return failing


def lookup_row(results, test_idx, sub_idx):
    """Look up a specific row in results by (test_idx, sub_idx)."""
    if test_idx not in results:
        return None
    for r in results[test_idx]["rows"]:
        if r["sub_idx"] == sub_idx:
            return r
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Escalation logic
# ─────────────────────────────────────────────────────────────────────────────

def run_escalation(assess_path, input_file, n, failing_tests, baseline_results,
                   report_dir=None):
    """
    Run escalation for all failing tests:
      Level 1: m=200
      Level 2: m=300

    Returns (m200_results, m300_results) dicts.
    """
    assess_dir = get_assess_working_dir(assess_path)
    failing_test_indices = set(failing_tests.keys())
    test_mask = build_test_mask(failing_test_indices)

    test_names = [TEST_NAMES[i] for i in sorted(failing_test_indices)]
    print(f"\n  Escalation required for {len(failing_test_indices)} test(s):")
    for tn in test_names:
        print(f"    - {tn}")
    print(f"  Test mask: {test_mask}")

    # Level 1: m=200
    run_assess(assess_path, input_file, n, ESCALATION_M1, test_mask,
               f"m={ESCALATION_M1}", report_dir)
    m200_rows = parse_report(assess_dir)
    m200_results = build_results(m200_rows)

    # Level 2: m=300
    run_assess(assess_path, input_file, n, ESCALATION_M2, test_mask,
               f"m={ESCALATION_M2}", report_dir)
    m300_rows = parse_report(assess_dir)
    m300_results = build_results(m300_rows)

    return m200_results, m300_results


def apply_decision_matrix(baseline_row, m200_row, m300_row):
    """
    Apply the three-outcome decision matrix per the NIST Re-Assessment Policy.

    | Baseline | m=200 | m=300 | Verdict |
    |----------|-------|-------|---------|
    | FAIL     | PASS  | PASS  | PASS    |
    | FAIL     | FAIL  | PASS  | SUSPECT |
    | FAIL     | FAIL  | FAIL  | FAIL    |

    "PASS" at each level means both dimensions pass (uniformity CI + proportion).
    """
    m200_pass = m200_row["overall_pass"] if m200_row else False
    m300_pass = m300_row["overall_pass"] if m300_row else False

    if m200_pass and m300_pass:
        return "PASS"
    elif not m200_pass and m300_pass:
        return "SUSPECT"
    else:
        return "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Display results
# ─────────────────────────────────────────────────────────────────────────────

def print_box(text):
    """Print text inside a box."""
    width = len(text) + 4
    print("+" + "-" * width + "+")
    print("|  " + text + "  |")
    print("+" + "-" * width + "+")


def fmt_p(p):
    """Format a p-value for display."""
    if p is None:
        return "N/A"
    return f"{p:.8f}"


def print_nist_raw_report(assess_dir):
    """
    Read and display the NIST-generated finalAnalysisReport.txt verbatim.

    This is the official NIST STS output (P-VALUE + PROPORTION + histogram
    bins C1-C10 for all 15 tests).  Showing it directly avoids duplicating
    the information in a custom table and gives reviewers the unmodified
    reference output as part of the Phase 1 record.
    """
    report_path = os.path.join(
        assess_dir, "experiments", "AlgorithmTesting", "finalAnalysisReport.txt"
    )
    if not os.path.isfile(report_path):
        print(f"\n  WARNING: finalAnalysisReport.txt not found at: {report_path}")
        return

    print()
    print_box("Phase 1 — NIST Raw Output  (finalAnalysisReport.txt)")
    print()
    with open(report_path, "r") as f:
        for line in f:
            print(" ", line, end="")
    print()


def print_results_table(baseline_results, escalation_map):
    """
    Print escalation details, multi-value sub-results, summary, and final
    assessment table.

    The Phase 1 raw NIST output (finalAnalysisReport.txt) is printed
    separately via print_nist_raw_report() right after the baseline run,
    so this function no longer repeats that table.

    baseline_results: dict { test_idx: test_data }
    escalation_map: dict { (test_idx, sub_idx): {
        "baseline": row, "m200": row, "m300": row, "verdict": str
    } } or empty if no escalation.
    """
    # ── Escalation details ──
    if escalation_map:
        print()
        print_box("Escalation Details")

        # Group by test
        by_test = {}
        for (ti, si), esc in escalation_map.items():
            if ti not in by_test:
                by_test[ti] = []
            by_test[ti].append((si, esc))

        for test_idx in sorted(by_test.keys()):
            test_name = TEST_NAMES[test_idx]
            items = sorted(by_test[test_idx], key=lambda x: x[0])

            print(f"\n  [{test_idx}] {test_name}")

            EW_LVL   = 8
            EW_LABEL = 16
            EW_PVAL  = 14
            EW_PROP  = 22
            EW_95    = 8
            EW_99    = 8

            esep = "  +" + "-" * (EW_LVL + 2) + \
                   "+" + "-" * (EW_LABEL + 2) + \
                   "+" + "-" * (EW_PVAL + 2) + \
                   "+" + "-" * (EW_PROP + 2) + \
                   "+" + "-" * (EW_95 + 2) + \
                   "+" + "-" * (EW_99 + 2) + "+"

            for sub_idx, esc in items:
                is_mp = esc.get("is_multi_proportion", False)

                if is_mp:
                    # Multi-proportion test: show sub-proportion summary table
                    print(f"\n  Sub-proportion assessment (# passing sub-tests / total):")
                    print(esep)
                    print(f"  | {'Level':<{EW_LVL}} "
                          f"| {'Type':<{EW_LABEL}} "
                          f"| {'(p-value)':{EW_PVAL}} "
                          f"| {'Sub-Prop (pass/total)':>{EW_PROP}} "
                          f"| {'95% CI':>{EW_95}} "
                          f"| {'99% CI':>{EW_99}} |")
                    print(esep)

                    for level_name, row_data, summary in [
                        (f"m={esc['baseline_m']}", esc["baseline"], esc.get("baseline_summary", "")),
                        (f"m={ESCALATION_M1}",     esc["m200"],     esc.get("m200_summary", "")),
                        (f"m={ESCALATION_M2}",     esc["m300"],     esc.get("m300_summary", "")),
                    ]:
                        if row_data is None:
                            continue
                        r95 = row_data.get("pass_95", "?")
                        r99 = row_data.get("pass_99", "?")
                        overall_lbl = "PASS" if row_data.get("overall_pass") else "FAIL"
                        print(f"  | {level_name:<{EW_LVL}} "
                              f"| {'sub-proportion':<{EW_LABEL}} "
                              f"| {overall_lbl:>{EW_PVAL}} "
                              f"| {summary:>{EW_PROP}} "
                              f"| {r95:>{EW_95}} "
                              f"| {r99:>{EW_99}} |")
                else:
                    label = esc["baseline"].get("label", "")
                    if label:
                        print(f"\n  Sub-result: {label}")

                    print(esep)
                    print(f"  | {'Level':<{EW_LVL}} "
                          f"| {'Sub-Result':<{EW_LABEL}} "
                          f"| {'P-Value':>{EW_PVAL}} "
                          f"| {'Proportion':>{EW_PROP}} "
                          f"| {'95% CI':>{EW_95}} "
                          f"| {'99% CI':>{EW_99}} |")
                    print(esep)

                    for level_name, row_data in [
                        (f"m={esc['baseline_m']}", esc["baseline"]),
                        (f"m={ESCALATION_M1}",     esc["m200"]),
                        (f"m={ESCALATION_M2}",     esc["m300"]),
                    ]:
                        if row_data is None:
                            continue

                        rlabel = (row_data.get("label") or "")[:EW_LABEL]
                        rp = fmt_p(row_data["p_value"])[:EW_PVAL]
                        rprop = (row_data.get("proportion") or "")[:EW_PROP]
                        r95 = assess_95(row_data["p_value"])
                        r99 = assess_99(row_data["p_value"])
                        prop_ok = check_proportion(row_data.get("proportion", ""))
                        if not prop_ok:
                            r95 = "FAIL"  # proportion failure overrides

                        print(f"  | {level_name:<{EW_LVL}} "
                              f"| {rlabel:<{EW_LABEL}} "
                              f"| {rp:>{EW_PVAL}} "
                              f"| {rprop:>{EW_PROP}} "
                              f"| {r95:>{EW_95}} "
                              f"| {r99:>{EW_99}} |")

                print(esep)
                print(f"  Verdict: {esc['verdict']}")

    # ── Multi-value sub-results (baseline) ──
    multi_tests = [
        baseline_results[ti] for ti in sorted(baseline_results.keys())
        if ti in MULTI_VALUE_LABELS and len(baseline_results[ti]["rows"]) > 1
    ]
    if multi_tests:
        print()
        print_box("Detailed Sub-Results for Multi-Value Tests (Baseline)")

        for test_data in multi_tests:
            test_idx = test_data["test_idx"]
            test_name = test_data["test_name"]
            subs = test_data["rows"]
            is_mp = test_idx in MULTI_PROPORTION_TESTS

            pass_count_subs = sum(1 for r in subs if r["overall_pass"])
            total_subs = len(subs)

            print(f"\n  [{test_idx}] {test_name} ({total_subs} sub-results)")

            if is_mp:
                # Show the sub-proportion assessment summary at the top
                summary = test_data.get("sub_proportion_summary", f"{pass_count_subs}/{total_subs}")
                overall_verdict = "PASS" if test_data["overall_pass"] else "FAIL"
                print(f"  Sub-proportion assessment: {summary}  ->  {overall_verdict}")
                print(f"  (Individual sub-results passing below threshold "
                      f"are statistically expected at α={ALPHA})")

            SW_LABEL = 16
            SW_PVAL  = 14
            SW_PROP  = 10
            SW_95    = 8
            SW_99    = 8

            ssep = "  +" + "-" * (SW_LABEL + 2) + "+" + "-" * (SW_PVAL + 2) + \
                   "+" + "-" * (SW_PROP + 2) + "+" + "-" * (SW_95 + 2) + \
                   "+" + "-" * (SW_99 + 2) + "+"

            print(ssep)
            print(f"  | {'Sub-Result':<{SW_LABEL}} "
                  f"| {'P-Value':>{SW_PVAL}} "
                  f"| {'Proportion':>{SW_PROP}} "
                  f"| {'95% CI':>{SW_95}} "
                  f"| {'99% CI':>{SW_99}} |")
            print(ssep)

            for sr in subs:
                label = (sr.get("label") or "")[:SW_LABEL]
                sp = fmt_p(sr["p_value"])[:SW_PVAL]
                sprop = (sr.get("proportion") or "")[:SW_PROP]

                # For multi-proportion tests that PASS overall, mark failing
                # sub-results with * to show they are within the acceptable
                # statistical threshold and do not fail the test.
                # When the overall test FAILs, these are real failures — no *.
                overall_test_passes = test_data["overall_pass"]
                flag_95 = sr["pass_95"]
                flag_99 = sr["pass_99"]
                if is_mp and not sr["overall_pass"] and overall_test_passes:
                    flag_95 = flag_95 + "*" if flag_95 == "FAIL" else flag_95
                    flag_99 = flag_99 + "*" if flag_99 == "FAIL" else flag_99

                print(f"  | {label:<{SW_LABEL}} "
                      f"| {sp:>{SW_PVAL}} "
                      f"| {sprop:>{SW_PROP}} "
                      f"| {flag_95:>{SW_95}} "
                      f"| {flag_99:>{SW_99}} |")

            print(ssep)
            if is_mp and test_data["overall_pass"]:
                print(f"  * = sub-result failed, but within acceptable "
                      f"proportion threshold — does NOT fail the overall test")

    # ── Summary ──
    total = len(baseline_results)
    pass_count = 0
    fail_count = 0
    suspect_count = 0

    for test_idx in range(1, 16):
        if test_idx not in baseline_results:
            continue

        if escalation_map:
            escalated_rows = {k: v for k, v in escalation_map.items() if k[0] == test_idx}
        else:
            escalated_rows = {}

        if not escalated_rows:
            pass_count += 1
        else:
            verdicts = [v["verdict"] for v in escalated_rows.values()]
            if "FAIL" in verdicts:
                fail_count += 1
            elif "SUSPECT" in verdicts:
                suspect_count += 1
            else:
                pass_count += 1

    print()
    print_box("Summary")
    print()
    print(f"  Total tests          : {total}")
    print(f"  PASS                 : {pass_count}")
    if suspect_count > 0:
        print(f"  SUSPECT              : {suspect_count}")
    print(f"  FAIL                 : {fail_count}")
    print()

    if fail_count == 0 and suspect_count == 0:
        print("  Final Assessment: ALL TESTS PASSED")
    elif fail_count > 0:
        print("  Final Assessment: FAILURES DETECTED")
    else:
        print("  Final Assessment: SUSPECT RESULTS - Requires independent file verification")
    print()

    # ── NIST Appendix-B style final summary table ──────────────────────────
    _print_appendix_b_summary(baseline_results, escalation_map)


# ─────────────────────────────────────────────────────────────────────────────
# NIST Appendix-B style Final Summary Table
# ─────────────────────────────────────────────────────────────────────────────

def _collect_summary_rows(baseline_results, escalation_map):
    """
    Build ordered display rows for the Appendix-B style summary table.

    Columns: Test Name | Proportion | P-Value | 95% CI | 99% CI

    One row per test/sub-result is emitted:
      - Tests 3 (Cumul. Sums) and 14 (Serial):  one row per sub-result
        (Forward/Reverse; p-value 1/p-value 2) — mirrors NIST Appendix B
        which lists Cusum-Forward and Cusum-Reverse separately.
      - Tests 8, 12, 13 (multi-proportion):     ONE representative row.
          Proportion = sub-proportion  (e.g. "143/148 (min 143)")
          P-Value    = canonical sub-result p-value  (template-1 / x=+1 / x=-1)
          95% / 99%  = overall sub-proportion assessment
          NOTE marker added so the reader knows the CI reflects the
          sub-proportion rule, not just that single p-value.
      - All other tests:                         single row.

    This follows NIST SP 800-22 Rev. 1a Appendix B conventions, extended
    with RNG Labs' Proportion and 95% / 99% bilateral CI columns.
    """
    out = []

    for test_idx in range(1, 16):
        if test_idx not in baseline_results:
            continue

        td    = baseline_results[test_idx]
        rows  = td["rows"]
        is_mp = test_idx in MULTI_PROPORTION_TESTS
        is_mv = test_idx in MULTI_VALUE_LABELS and len(rows) > 1

        # Determine final verdict ----------------------------------------
        esc_map = {k: v for k, v in (escalation_map or {}).items()
                   if k[0] == test_idx}
        if not esc_map:
            verdict = "PASS"
        else:
            vds = [v["verdict"] for v in esc_map.values()]
            verdict = ("FAIL" if "FAIL" in vds
                       else "SUSPECT" if "SUSPECT" in vds
                       else "PASS")

        # ── MULTI-PROPORTION (Tests 8, 12, 13) ──────────────────────────
        if is_mp:
            rep   = get_representative_row(test_idx, rows)
            label = rep.get("label", "") if rep else ""
            name  = TEST_NAMES[test_idx] + (f" ({label})" if label else "")
            prop  = td.get("sub_proportion_summary",
                           rep.get("proportion", "") if rep else "")
            pval  = rep["p_value"] if rep else None

            # If escalated, try to pull representative row from m200 data
            if esc_map:
                first_esc = next(iter(esc_map.values()))
                # baseline_summary already stored
                prop = first_esc.get("baseline_summary", prop)
                if verdict == "PASS" and first_esc.get("m200_summary"):
                    prop = first_esc["m200_summary"]

            # CI columns assess the representative p-value directly.
            # The overall pass/fail is determined by the sub-proportion check
            # (proportion column shows X/K); the CI here shows that
            # representative sub-result's own assessment so the table is
            # internally consistent (p-value ↔ CI result).
            ci95 = assess_95(pval) if pval is not None else "N/A"
            ci99 = assess_99(pval) if pval is not None else "N/A"

            out.append({
                "name":     name,
                "prop":     prop,
                "pval":     pval,
                "ci95":     ci95,
                "ci99":     ci99,
                "verdict":  verdict,
                "note":     False,  # CI directly reflects representative p-value
            })
            continue

        # ── MULTI-VALUE NON-PROPORTION (Tests 3 and 14) ─────────────────
        if is_mv:
            for row in rows:
                label   = row.get("label", "")
                name    = TEST_NAMES[test_idx] + (f" ({label})" if label else "")
                pval    = row["p_value"]
                prop    = row.get("proportion", "")
                ci95_r  = assess_95(pval) if pval is not None else "N/A"
                ci99_r  = assess_99(pval) if pval is not None else "N/A"
                if not check_proportion(prop):
                    ci95_r = "FAIL"
                    ci99_r = "FAIL"

                # If this specific sub-row was escalated, use escalated p-val
                for (ti, si), esc in esc_map.items():
                    if esc["baseline"] and esc["baseline"].get("label") == label:
                        rep_p = (esc["m200"]["p_value"]
                                 if verdict == "PASS" and esc.get("m200")
                                 else esc["baseline"]["p_value"])
                        rep_prop = (esc["m200"].get("proportion", prop)
                                    if verdict == "PASS" and esc.get("m200")
                                    else esc["baseline"].get("proportion", prop))
                        pval  = rep_p
                        prop  = rep_prop
                        ci95_r = assess_95(pval) if pval is not None else "N/A"
                        ci99_r = assess_99(pval) if pval is not None else "N/A"
                        if not check_proportion(prop):
                            ci95_r = "FAIL"
                            ci99_r = "FAIL"
                        break

                out.append({
                    "name":    name,
                    "prop":    prop,
                    "pval":    pval,
                    "ci95":    ci95_r,
                    "ci99":    ci99_r,
                    "verdict": verdict,
                    "note":    False,
                })
            continue

        # ── STANDARD SINGLE-ROW TEST ────────────────────────────────────
        row  = rows[0] if rows else None
        pval = row["p_value"]  if row else None
        prop = row.get("proportion", "") if row else ""
        ci95 = td["overall_pass_95"]
        ci99 = td["overall_pass_99"]

        if esc_map:
            first_esc = next(iter(esc_map.values()))
            if verdict == "PASS" and first_esc.get("m200"):
                pval = first_esc["m200"]["p_value"]
                prop = first_esc["m200"].get("proportion", prop)
            elif first_esc.get("baseline"):
                pval = first_esc["baseline"]["p_value"]
                prop = first_esc["baseline"].get("proportion", prop)
            ci95 = assess_95(pval) if pval is not None else "N/A"
            ci99 = assess_99(pval) if pval is not None else "N/A"
            if not check_proportion(prop):
                ci95 = "FAIL"
                ci99 = "FAIL"

        out.append({
            "name":    TEST_NAMES[test_idx],
            "prop":    prop,
            "pval":    pval,
            "ci95":    ci95,
            "ci99":    ci99,
            "verdict": verdict,
            "note":    False,
        })

    return out


def _print_appendix_b_summary(baseline_results, escalation_map):
    """
    Print the NIST Appendix-B style final summary table to console.

    Columns: Statistical Test | Proportion | P-Value | 95% CI | 99% CI

    Why Proportion is included (not in NIST Appendix B):
      NIST Appendix B shows p-value only because it is a *validation*
      reference (confirming the tool installation gives the same result).
      NIST's operational output (finalAnalysisReport.txt, Section 5
      Figure 5-1) explicitly includes BOTH P-VALUE and PROPORTION.
      RNG Labs' assessment standard uses the Bernoulli proportion CI as
      an explicit pass/fail criterion, so it must appear in the table.

    For Tests 8, 12, 13, the P-Value column shows the canonical
    representative sub-test (template-1 / x=+1 / x=-1) following
    NIST Appendix B convention.  The 95%/99% CI columns reflect that
    representative sub-test's own p-value assessment.  The Proportion
    column shows 'X/K (min Y)' — passing sub-tests / total, min required.
    """
    summary = _collect_summary_rows(baseline_results, escalation_map)

    print()
    print_box("Final Assessment Summary  (NIST SP 800-22 Appendix B style)")
    print()

    W_NAME = 52   # wide enough for "Non-overlapping Template Matching Test (template-1)"
    W_PROP = 20
    W_PVAL = 12
    W_CI   = 8

    sep = ("+" + "-" * (W_NAME + 2)
           + "+" + "-" * (W_PROP + 2)
           + "+" + "-" * (W_PVAL + 2)
           + "+" + "-" * (W_CI + 2)
           + "+" + "-" * (W_CI + 2) + "+")

    print(sep)
    print(f"| {'Statistical Test':<{W_NAME}} "
          f"| {'Proportion':<{W_PROP}} "
          f"| {'P-Value':>{W_PVAL}} "
          f"| {'95% CI':>{W_CI}} "
          f"| {'99% CI':>{W_CI}} |")
    print(sep)

    for r in summary:
        name_col = r["name"]
        if len(name_col) > W_NAME:
            name_col = name_col[:W_NAME - 1] + "…"

        prop_col = (r["prop"] or "")[:W_PROP]
        pval_col = fmt_p(r["pval"])[:W_PVAL] if r["pval"] is not None else "N/A"
        ci95_col = r["ci95"][:W_CI]
        ci99_col = r["ci99"][:W_CI]

        print(f"| {name_col:<{W_NAME}} "
              f"| {prop_col:<{W_PROP}} "
              f"| {pval_col:>{W_PVAL}} "
              f"| {ci95_col:>{W_CI}} "
              f"| {ci99_col:>{W_CI}} |")

    print(sep)
    print()
    print("  Note: Proportion is included per RNG Labs assessment standard")
    print("  (NIST Appendix B shows P-Value only; NIST Section 5 finalAnalysisReport")
    print("  includes both P-VALUE and PROPORTION as part of the operational output).")
    print("  For Tests 8, 12, 13: Proportion = X/K (min Y) sub-tests passing;")
    print("  95%/99% CI columns assess the representative sub-test's p-value directly.")
    print()


def generate_markdown_report(baseline_results, escalation_map, n, m, input_file,
                             assess_dir, output_path):
    """Generate a Markdown report file."""
    with open(output_path, "w") as f:
        f.write("# NIST SP 800-22 Statistical Test Suite - Analysis Report\n\n")
        f.write(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## Test Parameters\n\n")
        f.write("| Parameter | Value |\n")
        f.write("|---|---|\n")
        f.write(f"| Input file | `{os.path.basename(input_file)}` |\n")
        f.write(f"| File size | {os.path.getsize(input_file) / (1024**3):.2f} GB |\n")
        f.write(f"| Sequence length (n) | {n:,} |\n")
        f.write(f"| Bitstreams (m) | {m} |\n")
        f.write(f"| Total bits tested | {n * m:,} |\n")
        f.write(f"| Tool | NIST STS sts-2.1.2 (official) |\n\n")

        # ── Phase 1: NIST raw output ────────────────────────────────────────
        f.write("## Phase 1: Baseline Results (NIST Raw Output)\n\n")
        f.write("> The table below is the unmodified output of the official NIST STS tool "
                "(`finalAnalysisReport.txt`).  Columns: histogram bins C1–C10, "
                "uniformity P-VALUE, PROPORTION of passing sequences, test name.\n\n")

        nist_report_path = os.path.join(
            assess_dir, "experiments", "AlgorithmTesting", "finalAnalysisReport.txt"
        )
        if os.path.isfile(nist_report_path):
            with open(nist_report_path, "r") as nf:
                raw_content = nf.read()
            f.write("```\n")
            f.write(raw_content)
            if not raw_content.endswith("\n"):
                f.write("\n")
            f.write("```\n\n")
        else:
            f.write(f"*`finalAnalysisReport.txt` not found at: `{nist_report_path}`*\n\n")

        # Escalation details
        if escalation_map:
            f.write("\n## Escalation Details\n\n")

            by_test = {}
            for (ti, si), esc in escalation_map.items():
                if ti not in by_test:
                    by_test[ti] = []
                by_test[ti].append((si, esc))

            for test_idx in sorted(by_test.keys()):
                items = sorted(by_test[test_idx], key=lambda x: x[0])

                for sub_idx, esc in items:
                    label = esc["baseline"].get("label", "")
                    heading = f"[{test_idx}] {TEST_NAMES[test_idx]}"
                    if label:
                        heading += f" - {label}"
                    f.write(f"### {heading}\n\n")

                    if esc.get("is_multi_proportion"):
                        # Multi-proportion test: render sub-proportion summaries per level
                        f.write("| Level | Sub-Proportion (pass/total) | Overall |\n")
                        f.write("|---|---|---|\n")
                        for level_name, row_data, summary in [
                            (f"m={esc['baseline_m']}", esc["baseline"], esc.get("baseline_summary", "")),
                            (f"m={ESCALATION_M1}",     esc["m200"],     esc.get("m200_summary", "")),
                            (f"m={ESCALATION_M2}",     esc["m300"],     esc.get("m300_summary", "")),
                        ]:
                            if row_data is None:
                                continue
                            overall_lbl = "PASS" if row_data.get("overall_pass") else "FAIL"
                            f.write(f"| {level_name} | {summary} | {overall_lbl} |\n")
                    else:
                        f.write("| Level | P-Value | Proportion | 95% CI | 99% CI |\n")
                        f.write("|---|---|---|---|---|\n")

                        for level_name, row_data in [
                            (f"m={esc['baseline_m']}", esc["baseline"]),
                            (f"m={ESCALATION_M1}", esc["m200"]),
                            (f"m={ESCALATION_M2}", esc["m300"]),
                        ]:
                            if row_data is None:
                                continue
                            rp = fmt_p(row_data["p_value"])
                            rprop = row_data.get("proportion", "")
                            r95 = assess_95(row_data["p_value"])
                            r99 = assess_99(row_data["p_value"])
                            if not check_proportion(rprop):
                                r95 = "FAIL"
                            f.write(f"| {level_name} | {rp} | {rprop} | {r95} | {r99} |\n")

                    f.write(f"\n**Verdict: {esc['verdict']}**\n\n")

        # Multi-value sub-results
        multi_tests = [
            baseline_results[ti] for ti in sorted(baseline_results.keys())
            if ti in MULTI_VALUE_LABELS and len(baseline_results[ti]["rows"]) > 1
        ]
        if multi_tests:
            f.write("## Detailed Sub-Results (Baseline)\n\n")
            for test_data in multi_tests:
                test_idx = test_data["test_idx"]
                is_mp = test_idx in MULTI_PROPORTION_TESTS
                f.write(f"### [{test_idx}] {test_data['test_name']}\n\n")
                if is_mp:
                    summary = test_data.get("sub_proportion_summary", "")
                    overall = "PASS" if test_data["overall_pass"] else "FAIL"
                    f.write(f"**Sub-proportion assessment:** {summary} → **{overall}**\n\n")
                    if test_data["overall_pass"]:
                        f.write(f"> Individual sub-result failures marked `*` are within the "
                                f"acceptable threshold (α={ALPHA}) and do not fail the test.\n\n")
                f.write("| Sub-Result | P-Value | Proportion | 95% CI | 99% CI |\n")
                f.write("|---|---|---|---|---|\n")
                overall_test_passes = test_data["overall_pass"]
                for sr in test_data["rows"]:
                    flag95 = sr["pass_95"]
                    flag99 = sr["pass_99"]
                    # Only mark * when the overall test PASSES (sub-failures within threshold).
                    # When overall FAILS, sub-result failures are real — show plain FAIL.
                    if is_mp and not sr["overall_pass"] and overall_test_passes:
                        flag95 = flag95 + "*" if flag95 == "FAIL" else flag95
                        flag99 = flag99 + "*" if flag99 == "FAIL" else flag99
                    f.write(f"| {sr.get('label', '')} | {fmt_p(sr['p_value'])} "
                            f"| {sr.get('proportion', '')} | {flag95} | {flag99} |\n")
                f.write("\n")

        # Summary
        pass_count = 0
        fail_count = 0
        suspect_count = 0
        for test_idx in range(1, 16):
            if test_idx not in baseline_results:
                continue
            esc_rows = {k: v for k, v in escalation_map.items() if k[0] == test_idx} if escalation_map else {}
            if not esc_rows:
                pass_count += 1
            else:
                verdicts = [v["verdict"] for v in esc_rows.values()]
                if "FAIL" in verdicts:
                    fail_count += 1
                elif "SUSPECT" in verdicts:
                    suspect_count += 1
                else:
                    pass_count += 1

        f.write("## Summary\n\n")
        f.write("| Metric | Value |\n")
        f.write("|---|---|\n")
        f.write(f"| Total tests | {len(baseline_results)} |\n")
        f.write(f"| PASS | {pass_count} |\n")
        if suspect_count > 0:
            f.write(f"| SUSPECT | {suspect_count} |\n")
        f.write(f"| FAIL | {fail_count} |\n\n")

        if fail_count == 0 and suspect_count == 0:
            f.write("**Final Assessment: ALL TESTS PASSED**\n")
        elif fail_count > 0:
            f.write("**Final Assessment: FAILURES DETECTED**\n")
        else:
            f.write("**Final Assessment: SUSPECT RESULTS - Requires independent file verification**\n")

        # ── NIST Appendix-B style final summary table ────────────────────
        summary = _collect_summary_rows(baseline_results, escalation_map)

        f.write("\n---\n\n## Final Assessment Summary  (NIST SP 800-22 Appendix B style)\n\n")
        f.write("> **Why Proportion is included:** NIST Appendix B lists P-Value only "
                "(it is a validation reference). NIST's operational output "
                "(`finalAnalysisReport.txt`, Section 5 Figure 5-1) includes both "
                "P-VALUE and PROPORTION. RNG Labs' assessment standard uses the "
                "Bernoulli proportion CI as an explicit criterion, so it is included here.\n")
        f.write("> For Tests 8, 12, 13: Proportion = `X/K (min Y)` — passing sub-tests / total, "
                "min required. P-Value shown is for the canonical representative sub-test "
                "(template-1 / x=+1 / x=−1). The 95%/99% CI columns assess that "
                "representative sub-test's p-value directly.\n\n")

        f.write("| Statistical Test | Proportion | P-Value | 95% CI | 99% CI |\n")
        f.write("|---|---|---|---|---|\n")

        for r in summary:
            prop_col = r["prop"] or ""
            pval_col = fmt_p(r["pval"]) if r["pval"] is not None else "N/A"
            f.write(f"| {r['name']} | {prop_col} | {pval_col} | {r['ci95']} | {r['ci99']} |\n")

    print(f"  Markdown report : {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NIST SP 800-22 Statistical Test Suite - Automated Analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -f 1 -n 1000000 -m 100 -i rng_4gb.bin
  %(prog)s -f 1 -n 1000000 -m 100 -i rng_4gb.bin --assess-path /usr/local/bin/assess
  %(prog)s -f 1 -n 2000000 -m 100 -i rng_4gb.bin -o report.md
  %(prog)s -f 0 -n 1000000 -m 1  -i data.pi --test

  -f 0  ASCII  — input file contains a sequence of '0' and '1' characters
  -f 1  Binary — input file is raw binary (each byte = 8 bits)

  --test  Bypass m >= 100 restriction and print per-stream p-values for
          Appendix B validation (used by test_nist_appendix_b.py).

Report output:
  All reports are saved to <input_filename>-report/ in the current directory.
  NIST raw output (experiments/AlgorithmTesting/) is copied there automatically.
        """
    )
    parser.add_argument("-n", type=int, default=1_000_000,
                        help="Sequence length in bits (default: 1000000, minimum: 1000000)")
    parser.add_argument("-m", type=int, default=100,
                        help="Number of bitstreams (default: 100, minimum: 100 for binary; "
                             "1 allowed for ASCII validation)")
    parser.add_argument("-f", type=int, default=None, choices=[0, 1],
                        help="Input file format (REQUIRED): "
                             "0=ASCII (sequence of '0'/'1' characters), "
                             "1=Binary (each byte = 8 bits)")
    parser.add_argument("-i", type=str, required=True,
                        help="Input file path")
    parser.add_argument("--assess-path", type=str, default=None,
                        help="Path to the NIST STS assess binary")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Output Markdown report path (default: nist_report_<timestamp>.md)")
    parser.add_argument("--test", action="store_true", default=False,
                        help="Test mode: bypass the m >= 100 restriction and print "
                             "per-stream p-values for Appendix B validation")

    args = parser.parse_args()

    # Require -f to be explicitly specified
    if args.f is None:
        parser.error(
            "argument -f is required.\n\n"
            "  Please specify the input file format:\n"
            "    -f 0   ASCII  — file contains a sequence of '0' and '1' characters\n"
            "    -f 1   Binary — file is raw binary (each byte = 8 bits)\n\n"
            "  Example: nist_analysis.py -f 1 -n 1000000 -m 100 -i rng_data.bin"
        )

    # Banner
    print()
    print("=" * 60)
    print("  NIST SP 800-22 Statistical Test Suite")
    print("  Automated Analysis Script (official sts-2.1.2)")
    print("  With Re-Assessment Escalation (m=200, m=300)")
    print("=" * 60)

    # Validate
    validate_inputs(args)

    # Locate assess
    assess_path = find_assess_binary(args.assess_path)
    print(f"\n  assess binary : {assess_path}")

    # File info
    file_size = os.path.getsize(args.i)
    fmt_label = "0 — ASCII (sequence of '0'/'1' characters)" if args.f == 0 else "1 — Binary (8 bits per byte)"
    if args.f == 1:
        bits_available = file_size * 8
        size_display = f"{file_size / (1024**3):.2f} GB ({file_size:,} bytes)"
    else:
        bits_available = file_size
        size_display = f"{file_size / (1024**2):.2f} MB ({file_size:,} bytes)"
    print(f"  Input file    : {args.i}")
    print(f"  File size     : {size_display}")
    print(f"  Format        : {fmt_label}")
    print(f"  n             : {args.n:,}")
    print(f"  m             : {args.m}")
    print(f"  Total bits    : {args.n * args.m:,}  (available: {bits_available:,})")

    # ── Create local report directory ──
    input_basename = os.path.splitext(os.path.basename(args.i))[0]
    report_dir = os.path.join(os.getcwd(), f"{input_basename}-report")
    os.makedirs(report_dir, exist_ok=True)
    print(f"  Report folder : {report_dir}")

    # ── Phase 1: Baseline run (all tests) ──
    print()
    print_box("Phase 1: Baseline Run (m={})".format(args.m))

    assess_dir = run_assess(assess_path, args.i, args.n, args.m, None,
                            f"Baseline m={args.m}", report_dir, fmt=args.f)

    print("\n  Parsing baseline results ...")
    baseline_rows = parse_report(assess_dir)
    baseline_results = build_results(baseline_rows)

    if not baseline_results:
        print("\n  ERROR: No results parsed from baseline run.")
        sys.exit(1)

    print(f"  Parsed {len(baseline_rows)} rows across {len(baseline_results)} tests.")

    # Show NIST's own output for Phase 1 (replaces our custom 15-test table)
    print_nist_raw_report(assess_dir)

    # In test mode: print per-stream p-values from individual results.txt files, then exit.
    # For m=1 the P-VALUE column in finalAnalysisReport.txt is "----" (chi-squared
    # uniformity is undefined for 1 observation).  The actual per-test p-values —
    # the ones documented in NIST SP 800-22 Appendix B — are written by each test
    # to its own results.txt file under experiments/AlgorithmTesting/<TestDir>/.
    if args.test:
        pvalues = read_per_stream_pvalues(assess_dir)
        print()
        print("--- APPENDIX B P-VALUES ---")
        for test_dir, vals in pvalues.items():
            for i, pv in enumerate(vals):
                print(f"PVALUE: {test_dir}[{i}]={pv:.6f}")
        print("--- END ---")
        sys.exit(0)

    # Check for failures
    failing = find_failing_rows(baseline_results)
    escalation_map = {}

    if not failing:
        print("\n  All tests PASSED at baseline. No escalation needed.")
    else:
        print(f"\n  Failures detected across {len(failing)} test(s):")
        for ti, subs in sorted(failing.items()):
            tname = TEST_NAMES[ti]
            if ti in MULTI_PROPORTION_TESTS:
                summary = baseline_results[ti].get("sub_proportion_summary", "")
                print(f"    [{ti}] {tname}  [sub-proportion FAIL: {summary}]")
            elif ti in MULTI_VALUE_LABELS:
                labels = [get_sub_label(ti, s) for s in subs]
                print(f"    [{ti}] {tname}: {', '.join(labels)}")
            else:
                print(f"    [{ti}] {tname}")

        if args.f == 0:
            # ASCII / reference-validation mode — escalation is not meaningful
            # (m=1 means no statistical basis for multi-level re-runs).
            print()
            print("  [ASCII validation mode] Escalation skipped (not applicable for m=1 reference runs).")
            escalation_map = {}
            m200_results = None
            m300_results = None
        else:
            # ── Phase 2: Escalation ──
            print()
            print_box("Phase 2: Escalation (m={}, m={})".format(ESCALATION_M1, ESCALATION_M2))

            m200_results, m300_results = run_escalation(
                assess_path, args.i, args.n, failing, baseline_results, report_dir
            )

        if args.f != 0:
            # ── Phase 3: Apply decision matrix ──
            print()
            print_box("Phase 3: Decision Matrix")
            print()

            for test_idx, sub_indices in sorted(failing.items()):
                if test_idx in MULTI_PROPORTION_TESTS:
                    # ── Multi-proportion test (8, 12, 13) ────────────────────────
                    # sub_indices == [0] (sentinel meaning whole test failed)
                    # Decision is based on the test-level overall_pass, not a
                    # specific sub-row, because we re-ran the full test.
                    sub_idx = 0

                    # Build synthetic row-like dicts with just overall_pass set
                    # so apply_decision_matrix can work uniformly.
                    def _td_to_pseudo_row(td):
                        if td is None or test_idx not in td:
                            return None
                        return {"overall_pass": td[test_idx]["overall_pass"],
                                "p_value": None, "proportion": "",
                                "pass_95": td[test_idx]["overall_pass_95"],
                                "pass_99": td[test_idx]["overall_pass_99"],
                                "label": ""}

                    b_pseudo   = {"overall_pass": False, "p_value": None,
                                  "proportion": "", "pass_95": "FAIL",
                                  "pass_99": "FAIL", "label": ""}
                    m200_pseudo = _td_to_pseudo_row(m200_results)
                    m300_pseudo = _td_to_pseudo_row(m300_results)

                    verdict = apply_decision_matrix(b_pseudo, m200_pseudo, m300_pseudo)

                    test_name = TEST_NAMES[test_idx]
                    desc = f"[{test_idx}] {test_name}"

                    b_summary   = baseline_results[test_idx].get("sub_proportion_summary", "FAIL")
                    m2_summary  = m200_results[test_idx].get("sub_proportion_summary", "?") \
                                  if m200_results and test_idx in m200_results else "?"
                    m3_summary  = m300_results[test_idx].get("sub_proportion_summary", "?") \
                                  if m300_results and test_idx in m300_results else "?"
                    m2_status   = "PASS" if m200_pseudo and m200_pseudo["overall_pass"] else "FAIL"
                    m3_status   = "PASS" if m300_pseudo and m300_pseudo["overall_pass"] else "FAIL"

                    print(f"  {desc}  [sub-proportion check]")
                    print(f"    m={args.m}: FAIL ({b_summary})  |  "
                          f"m={ESCALATION_M1}: {m2_status} ({m2_summary})  |  "
                          f"m={ESCALATION_M2}: {m3_status} ({m3_summary})  ->  {verdict}")

                    escalation_map[(test_idx, sub_idx)] = {
                        "baseline":   b_pseudo,
                        "baseline_m": args.m,
                        "m200":       m200_pseudo,
                        "m300":       m300_pseudo,
                        "verdict":    verdict,
                        "is_multi_proportion": True,
                        "baseline_summary":    b_summary,
                        "m200_summary":        m2_summary,
                        "m300_summary":        m3_summary,
                    }
                else:
                    # ── Standard per-sub-row escalation ──────────────────────────
                    for sub_idx in sub_indices:
                        b_row   = lookup_row(baseline_results, test_idx, sub_idx)
                        m200_row = lookup_row(m200_results, test_idx, sub_idx)
                        m300_row = lookup_row(m300_results, test_idx, sub_idx)

                        verdict = apply_decision_matrix(b_row, m200_row, m300_row)

                        label = b_row.get("label", "") if b_row else ""
                        test_name = TEST_NAMES[test_idx]
                        desc = f"[{test_idx}] {test_name}"
                        if label:
                            desc += f" ({label})"

                        b_status = "FAIL"
                        m2_status = "PASS" if (m200_row and m200_row["overall_pass"]) else "FAIL"
                        m3_status = "PASS" if (m300_row and m300_row["overall_pass"]) else "FAIL"

                        print(f"  {desc}")
                        print(f"    m={args.m}: {b_status}  |  m={ESCALATION_M1}: {m2_status}  |  m={ESCALATION_M2}: {m3_status}  ->  {verdict}")

                        escalation_map[(test_idx, sub_idx)] = {
                            "baseline": b_row,
                            "baseline_m": args.m,
                            "m200": m200_row,
                            "m300": m300_row,
                            "verdict": verdict,
                            "is_multi_proportion": False,
                        }

    # ── Display final results ──
    print_results_table(baseline_results, escalation_map)

    # ── Copy NIST tool reports to local report directory ──
    nist_experiments_dir = os.path.join(assess_dir, "experiments", "AlgorithmTesting")
    local_nist_dir = os.path.join(report_dir, "nist_raw_output")

    if os.path.isdir(nist_experiments_dir):
        # Remove previous copy if exists
        if os.path.isdir(local_nist_dir):
            shutil.rmtree(local_nist_dir)
        shutil.copytree(nist_experiments_dir, local_nist_dir)
        print(f"\n  NIST raw reports copied to: {local_nist_dir}")
    else:
        print(f"\n  WARNING: NIST experiments directory not found: {nist_experiments_dir}")

    # ── Generate Markdown report ──
    if args.output:
        report_path = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(report_dir, f"nist_report_{timestamp}.md")

    generate_markdown_report(baseline_results, escalation_map, args.n, args.m, args.i,
                             assess_dir, report_path)

    # Print locations
    print()
    print_box("Report Locations")
    print()
    print(f"  Report folder   : {report_dir}")
    print(f"  Markdown report : {report_path}")
    print(f"  NIST raw output : {local_nist_dir}")
    nist_final = os.path.join(local_nist_dir, "finalAnalysisReport.txt")
    if os.path.isfile(nist_final):
        print(f"  Final analysis  : {nist_final}")
    print(f"  (Original at)   : {nist_experiments_dir}")
    print()


if __name__ == "__main__":
    main()