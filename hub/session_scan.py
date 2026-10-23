"""Scan one ESP32 session CSV and judge its quality.

Only the Python standard library is used so the same file runs on the
Raspberry Pi without installing dependencies.

The scanner accepts the current ``resp_v2`` 44-column format as well as the
older 39-column and 35-column UART formats.  It never interprets the new
ADS1220 fields as UART waveform fields.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

STALE_RESP_UART_US = 2_000_000
MIN_ROWS = 200  # at 20 Hz this is about 10 seconds
LARGE_ROW_GAP_US = 5_000_000  # missing CSV rows for more than 5 s

GOOD = "GOOD"
IMU_ONLY = "IMU_ONLY"
CHEST_BELT_MISSING = "CHEST_BELT_MISSING"
ABDOMEN_BELT_MISSING = "ABDOMEN_BELT_MISSING"
BOTH_BELTS_MISSING = "BOTH_BELTS_MISSING"
SHORT_RECORDING = "SHORT_RECORDING"
HAS_LARGE_GAP = "HAS_LARGE_GAP"
PARSE_FAILED = "PARSE_FAILED"
SYNTHETIC_DATA = "SYNTHETIC_DATA"


@dataclass
class SessionReport:
    path: Path
    format_version: str = "unknown"
    data_source: str = "unknown"
    rows: int = 0
    duration_s: float = 0.0
    chest_imu_valid_ratio: float = 0.0
    abdomen_imu_valid_ratio: float = 0.0
    chest_belt_valid_ratio: float = 0.0
    abdomen_belt_valid_ratio: float = 0.0
    chest_belt_unique_frames: int = 0
    abdomen_belt_unique_frames: int = 0
    chest_belt_rate_bpm_medians: int = 0
    max_row_gap_s: float = 0.0
    issues: list[str] = field(default_factory=list)
    verdict: str = PARSE_FAILED

    def summary(self) -> str:
        return (
            f"{self.path.name}: {self.verdict} rows={self.rows} "
            f"dur={self.duration_s:.1f}s imu={self.chest_imu_valid_ratio:.2f}/"
            f"{self.abdomen_imu_valid_ratio:.2f} "
            f"belt={self.chest_belt_valid_ratio:.2f}/"
            f"{self.abdomen_belt_valid_ratio:.2f}"
        )


def _ratio(rows: list[dict], column: str) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if row.get(column, "") == "1") / len(rows)


def _any_valid_ratio(rows: list[dict], columns: tuple[str, ...]) -> float:
    if not rows:
        return 0.0
    return sum(1 for row in rows if any(row.get(column) == "1" for column in columns)) / len(rows)


def _unique_valid_frames(
    rows: list[dict], pairs: tuple[tuple[str, str], ...]
) -> int:
    values: set[str] = set()
    for row in rows:
        for valid_column, sample_column in pairs:
            value = row.get(sample_column, "")
            if row.get(valid_column) == "1" and value:
                values.add(value)
    return len(values)


def scan_session(path: Path) -> SessionReport:
    report = SessionReport(path=path)
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            if "mono_us" not in header:
                report.issues.append("no mono_us column")
                return report
            if "schema_version" in header and {
                "chest_belt_low_valid",
                "chest_belt_high_valid",
                "abdomen_belt_low_valid",
                "abdomen_belt_high_valid",
            }.issubset(header):
                report.format_version = "resp_v2"
            elif "chest_resp_uart_valid" in header:
                report.format_version = "39col"
            elif "resp_uart_valid" in header:
                report.format_version = "35col-legacy"
                report.issues.append("legacy 35-column format")
            else:
                report.format_version = "unknown"

            rows = []
            first_us = last_us = None
            max_gap = 0.0
            previous_us = None
            for row in reader:
                try:
                    current_us = int(row["mono_us"])
                except (KeyError, TypeError, ValueError):
                    continue
                if previous_us is not None:
                    gap = current_us - previous_us
                    if gap > max_gap:
                        max_gap = gap
                previous_us = current_us
                if first_us is None:
                    first_us = current_us
                last_us = current_us
                rows.append(row)
    except OSError as error:
        report.issues.append(f"read error: {error}")
        return report

    report.rows = len(rows)
    if rows and first_us is not None and last_us is not None:
        report.duration_s = (last_us - first_us) / 1_000_000
    report.max_row_gap_s = max_gap / 1_000_000
    report.chest_imu_valid_ratio = _ratio(rows, "chest_valid")
    report.abdomen_imu_valid_ratio = _ratio(rows, "abdomen_valid")
    if report.format_version == "resp_v2":
        report.data_source = next(
            (row.get("data_source", "") for row in rows if row.get("data_source")),
            "unknown",
        )
        report.chest_belt_valid_ratio = _any_valid_ratio(
            rows, ("chest_belt_low_valid", "chest_belt_high_valid")
        )
        report.abdomen_belt_valid_ratio = _any_valid_ratio(
            rows, ("abdomen_belt_low_valid", "abdomen_belt_high_valid")
        )
        report.chest_belt_unique_frames = _unique_valid_frames(
            rows,
            (("chest_belt_low_valid", "chest_belt_low_sample_us"),
             ("chest_belt_high_valid", "chest_belt_high_sample_us")),
        )
        report.abdomen_belt_unique_frames = _unique_valid_frames(
            rows,
            (("abdomen_belt_low_valid", "abdomen_belt_low_sample_us"),
             ("abdomen_belt_high_valid", "abdomen_belt_high_sample_us")),
        )
    else:
        report.chest_belt_valid_ratio = _ratio(rows, "chest_resp_uart_valid")
        report.abdomen_belt_valid_ratio = _ratio(rows, "abdomen_resp_uart_valid")
        report.chest_belt_unique_frames = len(
            {row["chest_resp_uart_sample_us"]
             for row in rows
             if row.get("chest_resp_uart_valid") == "1" and row.get("chest_resp_uart_sample_us")}
        )
        report.abdomen_belt_unique_frames = len(
            {row["abdomen_resp_uart_sample_us"]
             for row in rows
             if row.get("abdomen_resp_uart_valid") == "1" and row.get("abdomen_resp_uart_sample_us")}
        )

    if report.rows < MIN_ROWS:
        report.issues.append(f"only {report.rows} rows (<{MIN_ROWS})")
        report.verdict = SHORT_RECORDING
        return report
    if report.max_row_gap_s >= LARGE_ROW_GAP_US / 1_000_000:
        report.issues.append(f"row gap {report.max_row_gap_s:.1f}s")
        report.verdict = HAS_LARGE_GAP
        return report

    if report.data_source == "synthetic_test":
        report.issues.append("synthetic_test data; exclude from scientific analysis")
        report.verdict = SYNTHETIC_DATA
        return report

    chest_belt_ok = (
        report.chest_belt_valid_ratio >= 0.10
        and report.chest_belt_unique_frames >= 10
    )
    abdomen_belt_ok = (
        report.abdomen_belt_valid_ratio >= 0.10
        and report.abdomen_belt_unique_frames >= 10
    )
    if not chest_belt_ok and not abdomen_belt_ok:
        report.verdict = BOTH_BELTS_MISSING
    elif not chest_belt_ok:
        report.verdict = CHEST_BELT_MISSING
    elif not abdomen_belt_ok:
        report.verdict = ABDOMEN_BELT_MISSING
    else:
        report.verdict = GOOD

    # A leading invalid stretch is normal (sensors still booting); only warn.
    if report.chest_imu_valid_ratio < 0.9:
        report.issues.append(
            f"chest IMU valid only {report.chest_imu_valid_ratio:.0%}")
    if report.abdomen_imu_valid_ratio < 0.9:
        report.issues.append(
            f"abdomen IMU valid only {report.abdomen_imu_valid_ratio:.0%}")
    return report
