#!/usr/bin/env python3
"""hub-ingest: copy new session CSVs from an inbox into the archive.

The inbox is abstract: today it is a plain folder you scp files into; when
the SD reader arrives it becomes a read-only USB mount of the Node's TF
card. Only the --inbox argument changes, the processing stays the same.

Behavior:
- scans the inbox for REC_*.CSV / LOG*.CSV and EVENTS.CSV
- runs session_scan on each session file
- GOOD files go to  <archive>/<YYYYMMDD>/<stem>/
- flawed files (IMU_ONLY etc.) are still archived, but marked in quality.json
- garbage (unparsable) goes to <quarantine>/ for inspection
- already-ingested files (matched by size+mtime) are skipped
- writes a quality.json next to each archived session
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_scan import (  # noqa: E402
    BOTH_BELTS_MISSING,
    PARSE_FAILED,
    SHORT_RECORDING,
    SYNTHETIC_DATA,
    scan_session,
)
from user_store import (  # noqa: E402
    hub_root_for,
    load_current_user,
    session_id_for,
)

_NUMBERED_NODE_FILE = re.compile(r"^R\d+\.CSV$", re.IGNORECASE)
_TEST_NODE_FILE = re.compile(r"^T\d+\.CSV$", re.IGNORECASE)


def is_event_file(name: str) -> bool:
    return name.upper() == "EVENTS.CSV"


def is_test_file(name: str) -> bool:
    return bool(_TEST_NODE_FILE.match(name.upper()))


def is_session_file(name: str) -> bool:
    upper = name.upper()
    return bool(
        _NUMBERED_NODE_FILE.match(upper)
        or upper.startswith("REC_")
        or upper.startswith("LOG")
    )


def looks_like_legacy_session(path: Path) -> bool:
    """Accept old datasets with human-readable names, but reject label CSVs."""
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            header = next(csv.reader(handle), [])
    except (OSError, csv.Error):
        return False
    return (
        "mono_us" in header
        and ("chest_resp_uart_valid" in header
             or "resp_uart_valid" in header
             or "chest_belt_low_valid" in header)
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def same_content(left: Path, right: Path) -> bool:
    try:
        return left.stat().st_size == right.stat().st_size and sha256_of(left) == sha256_of(right)
    except OSError:
        return False


def archive_dir_for(path: Path, archive_root: Path) -> Path:
    """Use the filename date, then the first valid unix_ms, then today."""
    stem = path.stem
    date_part = stem.split("_")[1] if stem.startswith("REC_") and "_" in stem else None
    if date_part and date_part.isdigit() and len(date_part) == 8:
        return archive_root / date_part
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                epoch_ms = int(row.get("unix_ms", "-1"))
                if epoch_ms >= 1_704_067_200_000:
                    return archive_root / datetime.fromtimestamp(
                        epoch_ms / 1000, timezone.utc
                    ).strftime("%Y%m%d")
    except (OSError, ValueError, csv.Error, OverflowError):
        pass
    return archive_root / datetime.now(timezone.utc).strftime("%Y%m%d")


def event_destination_for(path: Path, archive_root: Path) -> Path:
    existing = sorted(archive_root.glob("*/EVENTS.CSV"))
    if existing:
        return existing[-1]
    return archive_dir_for(path, archive_root) / "EVENTS.CSV"


def ingest(inbox: Path, archive: Path, quarantine: Path) -> int:
    if not inbox.is_dir():
        print(f"inbox not found: {inbox}", file=sys.stderr)
        return 2
    archive.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    # Ownership is frozen at ingest time from the active Hub user.
    hub_root = hub_root_for(archive)
    current = load_current_user(hub_root)
    user_id = current["user_id"]
    node_id = current["node_id"]

    candidates: list[Path] = [
        path for path in inbox.iterdir()
        if path.is_file() and (
            is_event_file(path.name)
            or is_test_file(path.name)
            or is_session_file(path.name)
            or (path.suffix.upper() == ".CSV" and looks_like_legacy_session(path)))
    ]

    moved = 0
    for path in sorted(set(candidates)):
        if path.name.upper() == "EVENTS.CSV":
            destination = event_destination_for(path, archive)
            verdict = "EVENTS"
        elif is_test_file(path.name):
            destination = archive_dir_for(path, archive) / path.name
            verdict = "SD_TEST"
        else:
            report = scan_session(path)
            verdict = report.verdict
            print(report.summary())
            if verdict == PARSE_FAILED:
                destination = quarantine / path.name
            else:
                destination = archive_dir_for(path, archive) / path.name
        if destination.exists() and same_content(path, destination):
            print(f"  skip (unchanged): {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        moved += 1

        quality = {
            "file": path.name,
            "verdict": verdict,
            "user_id": user_id,
            "session_id": None,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "source": str(path),
        }
        if verdict not in (PARSE_FAILED, "EVENTS", "SD_TEST"):
            report = scan_session(destination)
            quality.update({
                "format": report.format_version,
                "data_source": report.data_source,
                "rows": report.rows,
                "duration_s": round(report.duration_s, 1),
                "chest_imu_valid": round(report.chest_imu_valid_ratio, 3),
                "abdomen_imu_valid": round(report.abdomen_imu_valid_ratio, 3),
                "chest_belt_frames": report.chest_belt_unique_frames,
                "abdomen_belt_frames": report.abdomen_belt_unique_frames,
                "max_row_gap_s": round(report.max_row_gap_s, 2),
                "issues": report.issues,
            })
        quality_path = destination.with_name(destination.stem + ".quality.json")
        quality_path.write_text(
            json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
        # Companion ownership file; never edits the Node CSV itself.
        if verdict not in (PARSE_FAILED,):
            date_folder = destination.parent.name
            session_id = session_id_for(user_id, date_folder)
            quality["session_id"] = session_id
            quality_path.write_text(
                json.dumps(quality, ensure_ascii=False, indent=2),
                encoding="utf-8")
            meta = {
                "user_id": user_id,
                "session_id": session_id,
                "node_id": node_id,
                "received_at": quality["ingested_at"],
                "firmware_version": "unknown",
                "source_file": path.name,
                "archived_path": str(destination),
                "verdict": verdict,
            }
            meta_path = destination.with_name(destination.stem + ".meta.json")
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8")
        print(f"  -> {destination} [{verdict}] user={user_id}")

    print(f"ingest done: {moved} new file(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest ESP32 session CSVs")
    parser.add_argument("--inbox", type=Path, required=True,
                        help="folder that holds new TF-card exports")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    return ingest(parser.parse_args().inbox, parser.parse_args().archive,
                  parser.parse_args().quarantine)


if __name__ == "__main__":
    raise SystemExit(main())
