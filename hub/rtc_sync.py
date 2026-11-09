#!/usr/bin/env python3
"""Set the Node's DS3231 from the Pi's NTP-synced clock over the CDC port.

For manual runs and hub-rtc-sync.timer; hub_usb_export.py also syncs on
every plug-in. Exit codes: 0 PASS, 1 FAIL, 2 Pi clock not NTP-synced, 3 no
port or serial error (e.g. the port is busy during an export). Results go to
state/rtc_sync.jsonl and state/rtc_sync_last.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hub_node_protocol import (  # noqa: E402
    NodeLink,
    NodeTimeout,
    ProtocolError,
    find_node_port,
)

MIN_VALID_EPOCH = 1_704_067_200  # 2024-01-01 UTC, same floor as the Node
DEFAULT_STATE_DIR = Path(os.environ.get("HUB_STATE_DIR", Path.home() / "hub" / "state"))


def hub_clock_synced() -> bool:
    """True when timedatectl reports NTPSynchronized=yes."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip().lower() == "yes"


def measure_offset(link, now: Callable[[], float] = time.time) -> tuple[Optional[float], str]:
    """Node clock minus Pi clock in seconds (midpoint of request and reply,
    about 1 s resolution), and the Node's RTC state. The offset is None when
    the time is UNSET, ERROR or before 2024.
    """
    before = now()
    status = link.rtc_get()
    after = now()
    if status.state != "SYNCED" or status.epoch < MIN_VALID_EPOCH:
        return None, status.state
    return status.epoch - (before + after) / 2, status.state


def set_on_second_boundary(link, now: Callable[[], float] = time.time,
                           sleep: Callable[[float], None] = time.sleep) -> int:
    """Send RTCSET at the start of a second, so the whole-second value the
    DS3231 stores is not up to 1 s behind.
    """
    current = now()
    target = int(current) + 1
    sleep(target - current)
    link.rtc_set(target)  # raises ProtocolError if the echo doesn't match
    return target


def sync(link, record: dict, retries: int, tolerance: float, check_only: bool,
         now: Callable[[], float] = time.time,
         sleep: Callable[[float], None] = time.sleep) -> tuple[str, int, str]:
    """Measure, set and verify. Fills record with offset_before_s,
    offset_after_s and attempts; returns (result, exit_code, detail).
    """
    before, state = measure_offset(link, now)
    record["offset_before_s"] = None if before is None else round(before, 3)
    print(f"Node RTC before: {state}" + ("" if before is None else f", offset {before:+.2f} s"))

    if check_only:
        if before is None:
            return "NO_REPLY" if state == "ERROR" else "OUT_OF_TOLERANCE", 1, f"Node RTC state={state}"
        record["offset_after_s"] = record["offset_before_s"]
        ok = abs(before) <= tolerance
        return ("PASS", 0, "") if ok else ("OUT_OF_TOLERANCE", 1, f"offset {before:+.2f} s")

    for attempt in range(1, retries + 1):
        record["attempts"] = attempt
        try:
            target = set_on_second_boundary(link, now, sleep)
        except ProtocolError as error:  # rejected, or wrong echo
            print(f"Attempt {attempt}: RTCSET rejected ({error})")
            continue
        after, state = measure_offset(link, now)
        if after is None:
            print(f"Attempt {attempt}: RTCSET {target} accepted, but RTCGET says {state}")
            continue
        record["offset_after_s"] = round(after, 3)
        print(f"Attempt {attempt}: RTCSET {target}, read back offset {after:+.2f} s")
        if abs(after) <= tolerance:
            return "PASS", 0, ""
    return "FAIL", 1, f"offset not within +/-{tolerance} s after {retries} attempts"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial device; default: the Node under /dev/serial/by-id/")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--tolerance", type=float, default=3.0,
                        help="maximum absolute offset in seconds for PASS")
    parser.add_argument("--read-timeout", type=float, default=2.0,
                        help="seconds to wait for each reply")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="skip the Hub NTP check")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--no-boot-wait", action="store_true",
                        help="accepted for compatibility; the new firmware needs no boot wait")
    parser.add_argument("--trigger", default="manual")
    args = parser.parse_args()

    args.retries = max(1, args.retries)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "check" if args.check_only else "sync",
        "trigger": args.trigger,
        "retries_allowed": args.retries,
        "tolerance_s": args.tolerance,
        "hub_ntp_synced": hub_clock_synced(),
        "port": None,
        "attempts": 0,
        "offset_before_s": None,
        "offset_after_s": None,
        "result": None,
        "detail": "",
    }

    def finish(result: str, code: int, detail: str = "") -> int:
        record["result"] = result
        record["detail"] = detail
        with (args.state_dir / "rtc_sync.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        (args.state_dir / "rtc_sync_last.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"RESULT {result}" + (f": {detail}" if detail else ""))
        return code

    # A Pi clock that is not NTP-synced is no reference, for setting or for
    # checking.
    if not record["hub_ntp_synced"] and not args.force:
        return finish("HUB_NOT_SYNCED", 2,
                      "Pi clock is not NTP-synchronized; check timedatectl status")

    port_path = args.port or find_node_port()
    record["port"] = port_path
    if not port_path:
        return finish("NO_PORT", 3, "no Node serial port under /dev/serial/by-id/")
    try:
        link = NodeLink(port_path, timeout_s=args.read_timeout).open()
    except (OSError, RuntimeError) as error:
        return finish("PORT_ERROR", 3, f"{error} (an export may be using the port)")

    try:
        print(f"Node on {port_path}")
        result, code, detail = sync(link, record, args.retries, args.tolerance, args.check_only)
        return finish(result, code, detail)
    except NodeTimeout as error:
        return finish("NO_REPLY", 1, str(error))
    except (ProtocolError, OSError) as error:
        return finish("FAIL", 1, str(error))
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
