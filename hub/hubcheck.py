#!/usr/bin/env python3
"""hubcheck: scan one or more session CSVs and print quality reports.

Usage:
    python3 hubcheck.py /path/to/REC_....CSV [more.csv ...]
    python3 hubcheck.py --json /path/to/file.CSV
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_scan import scan_session


def main() -> int:
    parser = argparse.ArgumentParser(description="Check ESP32 session CSV quality")
    parser.add_argument("files", nargs="+", help="Session CSV files")
    parser.add_argument("--json", action="store_true", help="Output JSON lines")
    args = parser.parse_args()
    code = 0
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(f"{name}: NOT FOUND", file=sys.stderr)
            code = 2
            continue
        report = scan_session(path)
        if report.verdict in ("PARSE_FAILED", "SHORT_RECORDING", "BOTH_BELTS_MISSING"):
            code = 1
        if args.json:
            print(json.dumps({
                "file": report.path.name,
                "format": report.format_version,
                "rows": report.rows,
                "duration_s": round(report.duration_s, 1),
                "chest_imu": round(report.chest_imu_valid_ratio, 3),
                "abdomen_imu": round(report.abdomen_imu_valid_ratio, 3),
                "chest_belt": round(report.chest_belt_valid_ratio, 3),
                "abdomen_belt": round(report.abdomen_belt_valid_ratio, 3),
                "chest_frames": report.chest_belt_unique_frames,
                "abdomen_frames": report.abdomen_belt_unique_frames,
                "max_gap_s": round(report.max_row_gap_s, 2),
                "issues": report.issues,
                "verdict": report.verdict,
            }, ensure_ascii=False))
        else:
            print(report.summary())
            for issue in report.issues:
                print(f"  - {issue}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
