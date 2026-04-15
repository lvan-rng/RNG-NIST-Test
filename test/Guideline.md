# test_nist_appendix_b.py

Validates `nist_analysis.py` (or any compatible wrapper around the official NIST STS `assess` binary) against the reference p-values published in **Appendix B of NIST SP 800-22 Rev. 1a**.

---

## How It Works

For each of the five official NIST reference files the script does three things:

1. **Run `nist_analysis.py`** — invoked as a subprocess with the same arguments a user would provide, plus `--test` to bypass production-mode guards (see [The --test flag](#the---test-flag) below).
2. **Read `results.txt`** — after `assess` completes, p-values are read directly from the canonical output path:
   ```
   <assess_dir>/experiments/AlgorithmTesting/<TestName>/results.txt
   ```
3. **Compare & report** — each p-value is compared to the Appendix B table and reported as `PASS`, `FAIL`, or `SKIP`.

The script never deletes or modifies any files or directories. Before each run it calls `ensure_output_dirs()`, which creates any missing subdirectories under `experiments/AlgorithmTesting/` using `os.makedirs(..., exist_ok=True)` — purely additive.

---

## Usage

```bash
python3 test_nist_appendix_b.py \
    --assess-path /path/to/sts-2.1.2/assess \
    --data-dir    /path/to/sts-2.1.2/data/  \
    --script-path /path/to/nist_analysis.py
```

### Arguments

| Argument | Required | Description |
|---|---|---|
| `--assess-path` | **Yes** | Full path to the compiled NIST STS `assess` binary |
| `--data-dir` | No | Directory containing the 5 reference files. Defaults to current directory. |
| `--script-path` | No | Path to `nist_analysis.py`. Defaults to the file next to the test script. |

### Example (RNG Labs server)

```bash
python3 test.py \
    --assess-path /home/ubuntu/tools/sts-2.1.2/sts-2.1.2/assess \
    --data-dir    /home/ubuntu/tools/sts-2.1.2/sts-2.1.2/data   \
    --script-path nist_analyzer_1.6-beta.py
```

---

## NIST Parameters Used

All runs use the parameters documented in NIST SP 800-22 Rev. 1a Appendix B.

| Parameter | Value | Notes |
|---|---|---|
| Sequence length `n` | `1,000,000` | 1,000,000 bits per stream |
| Number of streams `m` | `1` | One stream per reference file |
| Format — `data.sha1` | `-f 1` (binary) | 125,000 bytes × 8 = 1,000,000 bits |
| Format — other 4 files | `-f 0` (ASCII) | 1,000,000 ASCII `0`/`1` characters |
| BlockFrequency block length | `m = 128` | NIST default |
| NonOverlappingTemplate | `m = 9`, `B = 000000001` | Appendix B reference template |
| OverlappingTemplate | `m = 9` | NIST default |
| ApproximateEntropy | `m = 10` | NIST default |
| LinearComplexity block length | `M = 500` | NIST default |
| Serial | `m = 16` | Only ∇Ψ²_m (first p-value) is tabulated in Appendix B |
| RandomExcursions state | `x = +1` | Index 4 of 8 states (`x = −4,−3,−2,−1,+1,+2,+3,+4`) |
| RandomExcursionsVariant state | `x = −1` | Index 8 of 18 states (`x = −9…−1, +1…+9`) |
| Tolerance | `±0.001` | `assess` prints 6 decimal places; platform rounding stays within this bound |

---

## Reference Data Files

All five files ship with the official NIST STS `sts-2.1.2` distribution under `data/`.

| File | Format | Size | Source |
|---|---|---|---|
| `data.pi` | ASCII | 1,165,666 B | First 1,000,000 bits of π |
| `data.e` | ASCII | 1,165,666 B | First 1,000,000 bits of e |
| `data.sha1` | Binary | 125,000 B | SHA-1 output stream |
| `data.sqrt2` | ASCII | 1,165,666 B | First 1,000,000 bits of √2 |
| `data.sqrt3` | ASCII | 1,165,666 B | First 1,000,000 bits of √3 |

Format is auto-detected: if `file_size × 8 == 1,000,000` the script passes `-f 1` (binary), otherwise `-f 0` (ASCII).

---

## Output

### Per-file table

```
────────────────────────────────────────────────────────────
  data.pi
────────────────────────────────────────────────────────────
  Test Name                    RNG Labs P-Value   NIST P-Value  Status
  ──────────────────────────────────────────────────────────────────
  Frequency                          0.578211       0.578211  ✓ PASS
  BlockFrequency                     0.380615       0.380615  ✓ PASS
  CumulativeSums[0]                  0.628308       0.628308  ✓ PASS
  CumulativeSums[1]                  0.663369       0.663369  ✓ PASS
  ...
```

### Grand summary

```
============================================================
  NIST SP 800-22 Appendix B — Grand Summary
============================================================
  File             Passed    Failed   Skipped
  ────────────  ────────  ────────  ────────
  data.pi             16         0         0
  data.e              16         0         0
  data.sha1           13         0         0
  data.sqrt2          16         0         0
  data.sqrt3          16         0         0
  ────────────  ────────  ────────  ────────
  TOTAL               77         0         0
============================================================

  ✓  All 77 p-values match NIST SP 800-22 Appendix B.
```

### Status codes

| Symbol | Meaning |
|---|---|
| `✓ PASS` | Computed p-value matches Appendix B within ±0.001 |
| `✗ FAIL` | Computed p-value differs from Appendix B by more than ±0.001 |
| `– SKIP` | `results.txt` not found (test did not produce output for this file) |

Exit code `0` = all tests pass. Exit code `1` = one or more mismatches.

---

## Special Cases

### `data.sha1` — no RandomExcursions values
Appendix B lists no RandomExcursions or RandomExcursionsVariant values for `data.sha1`. A single stream of SHA-1 output does not produce enough zero-crossings for these tests to execute. They appear as `SKIP` and do not count against the pass total.

### NonOverlappingTemplate — 148 templates
`assess` computes p-values for all 148 nine-bit templates and writes them in order to `NonOverlappingTemplate/results.txt`. Appendix B documents only template `B = 000000001`. The script locates this template's position in `templates/template9` and reads only that entry.

### CumulativeSums — two values
The test runs in both forward (`mode = 0`) and reverse (`mode = 1`) directions. Both p-values are validated: `CumulativeSums[0]` (forward) and `CumulativeSums[1]` (reverse).

### Serial — two values, one documented
The Serial test writes two p-values to `results.txt` (∇Ψ²_m and ∇²Ψ²_{m−1}). Appendix B documents only the first. The script compares index 0 only.

### RandomExcursions / RandomExcursionsVariant — indexed states
Appendix B documents one specific state per file. The script maps these to their index in `results.txt`:
- RandomExcursions `x = +1` → index 4 (states ordered `x = −4, −3, −2, −1, +1, +2, +3, +4`)
- RandomExcursionsVariant `x = −1` → index 8 (states ordered `x = −9…−1, +1…+9`)

### The `--test` flag
The script passes `--test` to `nist_analysis.py` for every run. This flag **only** bypasses two production-mode guards that are irrelevant for the small reference files: the `m ≥ 100` bitstream minimum and the 125 MB file-size minimum. The `assess` binary still runs in full and writes all `results.txt` files exactly as it would in production.

---

## Requirements

- Python 3.6+
- Compiled NIST STS `sts-2.1.2` `assess` binary
- The 5 reference data files from the `sts-2.1.2 data/` directory
- `nist_analysis.py` (or compatible wrapper) accessible via `--script-path`

---

## Reference

A. Rukhin et al., *A Statistical Test Suite for Random and Pseudorandom Number Generators for Cryptographic Applications*, NIST SP 800-22 Rev. 1a, April 2010. Appendix B, pp. B-1–B-3.
