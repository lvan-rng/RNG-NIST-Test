# RNGLABS — NIST SP 800-22 Automated Test & Analysis

Automated script for running the official NIST SP 800-22 Statistical Test Suite (sts-2.1.2) against RNG binary output, with intelligent escalation re-testing for borderline failures and Markdown report generation.

---

## Prerequisites

- Python 3.6+
- Official NIST STS `sts-2.1.2` compiled `assess` binary
- A raw binary input file (minimum 125 MB)

---

## Link NIST STS


The compiled `assess` binary is searched automatically in these locations:

```
/usr/local/bin/assess
~/tools/nist-sts/sts-2.1.2/assess
~/tools/sts-2.1.2/assess
./assess
../assess
```

If it is installed elsewhere, pass `--assess-path` at runtime.

---

## Quick Start

```bash
python3 nist_analysis.py -n 1000000 -m 100 -f 1 -i rng_output.bin
```

That's it. The script handles all interactive prompts, runs all 15 tests, escalates any failures, and writes a Markdown report automatically.

---

## Input File Requirements

| Requirement | Detail |
|---|---|
| Format | Raw binary — each byte is read as 8 bits |
| Minimum size | 125 MB (131,072,000 bytes) |
| Header | None — pure binary data, no headers or metadata |
| Sequence length `-n` | Default 1,000,000 bits (minimum enforced) |
| Bitstreams `-m` | Default 100 (minimum enforced) |
| File Type `-f` | 0 - ASCII / 1 - Binary |

The script validates both the file size and the `-n` / `-m` parameters on startup and exits with an error if any requirement is not met.

**`-f` is required** — the script will not run without it:

- `-f 0` : ASCII  — file contains a sequence of '0' and '1' characters
- `-f 1` : Binary — file is raw binary (each byte = 8 bits)

---

## How the Script Works

### Overview

```
Input file (>= 125 MB)
        |
        v
[PHASE 1]  Run assess — all 15 tests at m=100
        |
        v
Display NIST raw output (finalAnalysisReport.txt)
        |
+---> All tests pass?  ---> Final Assessment Summary ---> Done
        |
        v
[PHASE 2]  Re-run failing tests at m=200
        |
[PHASE 3]  Re-run failing tests at m=300
        |
        v
Apply decision matrix per failing test
        |
        v
Final Assessment Summary + Markdown report (.md)
```

---

### Phase 1 — Baseline Run

Runs all 15 NIST SP 800-22 tests at the specified bitstream count (default `m=100`). The script drives the interactive `assess` binary entirely via stdin — no manual prompting required.

After the run completes, the script displays the unmodified NIST raw output (`finalAnalysisReport.txt`) directly in the terminal. This is the official NIST record for the baseline run.

```
+------------------------------------------------------------+
|  Phase 1 — NIST Raw Output  (finalAnalysisReport.txt)     |
+------------------------------------------------------------+

 -------------------------------------------------------------------------------
 RESULTS FOR THE UNIFORMITY OF P-VALUES AND THE PROPORTION OF PASSING SEQUENCES
 -------------------------------------------------------------------------------
  C1  C2  C3  C4  C5  C6  C7  C8  C9 C10  P-VALUE  PROPORTION  STATISTICAL TEST
 -------------------------------------------------------------------------------
   9  11  10   9  10  10  11  10  11   9  0.534146     99/100   Frequency
  ...
```

If any test fails (uniformity p-value outside the 99% bilateral CI, or proportion below threshold), the script automatically proceeds to Phase 2.

---

### Phase 2 — Escalation Re-tests

Any test that fails at baseline is automatically re-tested with increased statistical power:

**Step 1:** Re-run failing tests at `m=200`

```bash
# Equivalent assess invocation (driven via stdin by the script)
./assess   # stdin: input file, targeted test mask, m=200, binary format
```

If it passes → no further escalation for that test.

**Step 2:** If `m=200` still fails, re-run at `m=300`

```bash
./assess   # stdin: input file, targeted test mask, m=300, binary format
```

Only the failing tests are re-run — passing tests are not repeated.

---

### Phase 3 — Decision Matrix

After escalation, each originally-failing test is assigned a final verdict:

| Baseline | m=200 | m=300 | Verdict |
|---|---|---|---|
| FAIL | PASS | — | **PASS** — false alarm at baseline |
| FAIL | FAIL | PASS | **SUSPECT** — escalate to expert review |
| FAIL | FAIL | FAIL | **FAIL** — definitive failure |
| PASS | — | — | **PASS** — no escalation needed |

---

## Pass / Fail Criteria

Each test is assessed across two dimensions:

### 1. Uniformity P-value (bilateral CI)

The chi-squared uniformity p-value for each test is checked against two confidence levels:

| Level | PASS zone (bilateral) | Fail condition |
|---|---|---|
| **99% CI** | 0.005 ≤ p ≤ 0.995 | p < 0.005 or p > 0.995 |
| **95% CI** | 0.025 ≤ p ≤ 0.975 | p < 0.025 or p > 0.975 |

The **99% CI is the definitive level** — it drives escalation and the final PASS/FAIL verdict, consistent with NIST's significance level α = 0.01. The 95% CI column is displayed for additional transparency.

> **Note:** The 99% pass zone is wider than 95%. If a p-value fails at 99%, it automatically fails at 95% as well.

### 2. Proportion of Passing Sequences (Bernoulli CI)

The proportion of bitstreams that individually pass each test is checked against a minimum threshold derived from the NIST Bernoulli formula:

```
min_pass = ⌈(p̂ − 3 × √(p̂ × α / m)) × m⌉
where p̂ = 1 − α = 0.99,  α = 0.01
```

Example thresholds:

| Bitstreams (m) | Min passing sequences |
|---|---|
| 100 | 96 / 100 |
| 200 | 194 / 200 |
| 300 | 292 / 300 |

A test fails if **either** the uniformity p-value or the proportion falls outside its threshold.

---

## Special Handling — Multi-Sub-Result Tests

Three tests produce one independent p-value per template or excursion state rather than a single p-value:

| Test | Sub-result type | Count |
|---|---|---|
| 8 — Non-overlapping Template Matching | One p-value per 9-bit template | Up to 148 |
| 12 — Random Excursions | One p-value per excursion state | 8 (x = ±1 through ±4) |
| 13 — Random Excursions Variant | One p-value per excursion state | 18 (x = ±1 through ±9) |

Applying a strict "all must pass" rule to these tests is statistically inconsistent — at α = 0.01, approximately 1% of sub-results are expected to fail even for a perfect RNG. Instead, RNG Labs uses a **sub-proportion criterion**: the overall test passes if the number of passing sub-results meets the same Bernoulli threshold used for bitstreams.

| Test | Max sub-results that may fail |
|---|---|
| Test 8 (K = 148) | 5 |
| Test 12 (K = 8) | 0 |
| Test 13 (K = 18) | 1 |

In the Final Assessment Summary, these tests are represented by a **canonical sub-result** following NIST Appendix B conventions:

| Test | Representative sub-result |
|---|---|
| Test 8 | template-1 (B = 000000001) |
| Test 12 | x = +1 |
| Test 13 | x = -1 |

The **Proportion column** for these rows shows the sub-proportion summary (e.g. `145/148 (min 143)`). The **95%/99% CI columns** assess the representative sub-result's own p-value directly.

---

## Test Scope

All 15 NIST SP 800-22 statistical tests are evaluated:

| # | Test Name | Sub-results |
|---|---|---|
| 1 | Frequency (Monobit) Test | 1 |
| 2 | Frequency Test within a Block | 1 |
| 3 | Cumulative Sums Test | 2 (Forward / Reverse) |
| 4 | Runs Test | 1 |
| 5 | Tests for the Longest-Run-of-Ones | 1 |
| 6 | Binary Matrix Rank Test | 1 |
| 7 | Discrete Fourier Transform (Spectral) | 1 |
| 8 | Non-overlapping Template Matching Test | Up to 148 |
| 9 | Overlapping Template Matching Test | 1 |
| 10 | Maurer's Universal Statistical Test | 1 |
| 11 | Approximate Entropy Test | 1 |
| 12 | Random Excursions Test | 8 |
| 13 | Random Excursions Variant Test | 18 |
| 14 | Serial Test | 2 (p-value 1 / p-value 2) |
| 15 | Linear Complexity Test | 1 |

---

## Output

### Terminal

The script prints live progress during execution, followed by the full structured report to stdout. The output is divided into clearly labelled phases:

```
+--------------------------------------+
|  Phase 1: Baseline Run (m=100)       |
+--------------------------------------+
  Running NIST STS assess ...
  Parsing baseline results ...
  Parsed 17 rows across 15 tests.

+------------------------------------------------------------+
|  Phase 1 — NIST Raw Output  (finalAnalysisReport.txt)     |
+------------------------------------------------------------+
  [NIST raw output]

+--------------------------------------+
|  Phase 2: Escalation (m=200, m=300)  |   ← only if failures
+--------------------------------------+

+--------------------------------------+
|  Phase 3: Decision Matrix            |   ← only if failures
+--------------------------------------+

+--------------------------------------+
|  Summary                             |
+--------------------------------------+

+---------------------------------------------------------------+
|  Final Assessment Summary (NIST SP 800-22 Appendix B style)  |
+---------------------------------------------------------------+
  [17-row conclusion table]
```

### Markdown Report

A `.md` report is saved automatically in a timestamped report folder next to the input file:

```
<input_filename>-report/
└── nist_report_<YYYYMMDD_HHMMSS>.md
└── nist_raw_output/
    └── finalAnalysisReport.txt
    └── ...
```

For example:

```
Input:  rng_4gb.bin
Report: rng_4gb-report/nist_report_20260414_142351.md
```

### Report Sections

The generated Markdown report contains:

| Section | Contents |
|---|---|
| **Test Parameters** | Input file, size, sequence length n, bitstreams m, total bits, tool version |
| **Phase 1: Baseline Results** | Unmodified NIST `finalAnalysisReport.txt` in a fenced code block |
| **Escalation Details** | Per-failing-test table showing m=100 / m=200 / m=300 results (only if failures occurred) |
| **Detailed Sub-Results** | Full breakdown of every sub-result for multi-value tests (3, 8, 12, 13, 14) |
| **Summary** | Pass / Suspect / Fail counts and overall verdict |
| **Final Assessment Summary** | 17-row Appendix B style table — test name, proportion, p-value, 95% CI, 99% CI |

---

## Usage

```
python3 nist_analysis.py -n <seq_len> -m <bitstreams> -f <filetype> -i <input_file> [options]
```

| Argument | Default | Description |
|---|---|---|
| `-n` | `1000000` | Sequence length in bits (minimum 1,000,000) |
| `-m` | `100` | Number of bitstreams (minimum 100) |
| `-f` | *(required)* | `0` = ASCII, `1` = Binary |
| `-i` | *(required)* | Path to raw binary input file |
| `--assess-path` | *(auto-detected)* | Path to the NIST STS `assess` binary |
| `-o / --output` | *(auto-generated)* | Output Markdown report path |

### Examples

**Standard run (all defaults):**
```bash
python3 nist_analysis.py -n 1000000 -m 100 -f 1 -i rng_4gb.bin
```

**Custom sequence length:**
```bash
python3 nist_analysis.py -n 2000000 -m 100 -f 1  -i rng_4gb.bin
```

**Explicit assess binary path:**
```bash
python3 nist_analysis.py -n 1000000 -m 100 -f 1  -i rng_4gb.bin \
  --assess-path /home/ubuntu/tools/sts-2.1.2/assess
```

**Custom report output path:**
```bash
python3 nist_analysis.py -n 1000000 -m 100 -f 1  -i rng_4gb.bin \
  -o /reports/nist_run_2026.md
```

---

## Appendix B Validation (`--test` mode)

`nist_analysis.py` includes a `--test` flag for validating correctness against the official NIST SP 800-22 Appendix B reference data.

When `--test` is specified:
- The `m >= 100` restriction is bypassed, allowing `m=1`
- After the assessment completes, per-stream p-values are printed to stdout and the script exits (skipping escalation and the Markdown report)

**Running a single file manually:**
```bash
python3 nist_analysis.py -f 0 -n 1000000 -m 1 -i data.pi --test
```

**Running the full Appendix B validation suite** (all 5 reference files):
```bash
python3 test_nist_appendix_b.py \
    --assess-path ./sts-2.1.2/assess \
    --data-dir    ./sts-2.1.2/data/
```

`test_nist_appendix_b.py` runs `nist_analysis.py` against each of the 5 NIST reference files (`data.pi`, `data.e`, `data.sha1`, `data.sqrt2`, `data.sqrt3`) and compares the returned p-values to the expected values from Appendix B (tolerance ±0.0001). A `PASS` on all comparisons confirms that `nist_analysis.py` is computing correct results.

---

## Terminal Output Examples

### All tests pass at baseline

```
+--------------------------------------+
|  Phase 1: Baseline Run (m=100)       |
+--------------------------------------+

  Running NIST STS assess ...
  Completed in 142.3 seconds.
  Parsing baseline results ...
  Parsed 17 rows across 15 tests.

  All tests PASSED at baseline. No escalation needed.

  ...

+--------------------------------------+
|  Summary                             |
+--------------------------------------+

  Total tests          : 15
  PASS                 : 15
  FAIL                 : 0

  Final Assessment: ALL TESTS PASSED
```

### A test requires escalation

```
  Failures detected across 1 test(s):
    [5] Tests for the Longest-Run-of-Ones

+----------------------------------------------+
|  Phase 2: Escalation (m=200, m=300)          |
+----------------------------------------------+

  ...

+--------------------------------------+
|  Phase 3: Decision Matrix            |
+--------------------------------------+

  [5] Tests for the Longest-Run-of-Ones
    m=100: FAIL  |  m=200: PASS  |  m=300: PASS  ->  PASS

  Final Assessment: ALL TESTS PASSED
```

### File too small

```
  ERROR: Input file is too small.
    Required : 125 MB (131,072,000 bytes)
    Actual   : 50 MB (52,428,800 bytes)
    File     : small_file.bin
```

---

## Runtime Expectations

| Phase | Typical Duration | Notes |
|---|---|---|
| Phase 1 (all 15 tests, m=100) | 2–10 minutes | Duration scales with `-n` and `-m` |
| Phase 2 (failing tests, m=200) | 1–5 minutes per test | Only runs for failed tests |
| Phase 3 (failing tests, m=300) | 2–8 minutes per test | Only runs if m=200 also fails |

Total runtime depends on how many tests need escalation. If all 15 pass at baseline, only Phase 1 runs.

---

## File Structure

```
RNGLABS/
├── nist_analysis.py                     # Main script
├── test_nist_appendix_b.py              # Appendix B validation test
├── README_nist_analysis.md              # This file
├── NIST_MultiSubResult_Policy_Provisions.md   # RNG Labs assessment standard
│
└── <input_filename>-report/             # Created automatically after each run
    ├── nist_report_<timestamp>.md       # Markdown report
    └── nist_raw_output/                 # NIST STS raw output files
        ├── finalAnalysisReport.txt
        └── experiments/
            └── AlgorithmTesting/
                └── ...
```
