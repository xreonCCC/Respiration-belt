#!/usr/bin/env python3
"""Synchronize the Node RTC from the Raspberry Pi's NTP-synchronized clock.

The Node firmware is expected to print a line like::

    RTC DS3231 | state=SYNCED | epoch=1790000000

and to accept ``RTCSET <utc_epoch>`` on its serial console.  The echoed
``RTCSET OK`` line only confirms that the command was accepted; verification
uses a later DS3231 status line so the value is independently read back.

Exit codes: 0 PASS, 1 FAIL, 2 Hub clock is not synchronized, 3 serial error.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:  # Keep the failure actionable for the systemd service.
    serial = None  # type: ignore[assignment]


BAUD = 115200
ANSI = re.compile(rb"\x1b\[[0-9;]*m")
STATUS_RE = re.compile(r"RTC DS3231 \| state=(\w+) \| epoch=(-?\d+)")
MIN_VALID_EPOCH = 1_704_067_200  # 2024-01-01 UTC
DEFAULT_STATE_DIR = Path(
    os.environ.get("HUB_STATE_DIR", Path.home() / "hub" / "state")
)


def hub_clock_synced() -> bool:
    """Return whether systemd-timesyncd/chrony reports an NTP-synced clock."""
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip().lower() == "yes"


def find_port() -> str | None:
    """Find a CP210x or native USB serial console."""
    candidates = sorted(glob.glob("/dev/serial/by-id/*CP210*"))
    candidates.extend(sorted(glob.glob("/dev/ttyUSB*")))
    candidates.extend(sorted(glob.glob("/dev/ttyACM*")))
    return candidates[0] if candidates else None


class NodePort:
    def __init__(self, path: str) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed; install hub/requirements.txt"
            )
        self.ser = serial.Serial(
            port=None,
            baudrate=BAUD,
            timeout=0.2,
            exclusive=True,
        )
        self.ser.port = path
        self.ser.dtr = False
        self.ser.rts = False
        try:
            self.ser.open()
        except serial.SerialException as error:
            raise RuntimeError(
                f"cannot open {path}: {error} (another monitor may be running)"
            ) from error
        self.ser.reset_input_buffer()
        self.buf = b""

    def write_line(self, text: str) -> None:
        self.ser.write(text.encode("ascii") + b"\n")
        self.ser.flush()

    def read_line(self, duration_s: float):
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            chunk = self.ser.readline()
            if chunk:
                self.buf += chunk
            if not self.buf.endswith(b"\n"):
                continue
            raw, self.buf = self.buf, b""
            line = ANSI.sub(b"", raw).decode(errors="replace").strip()
            if line:
                yield time.time(), line

    def close(self) -> None:
        self.ser.close()


def read_node_epoch(port: NodePort, wait_s: float):
    last_line = None
    for host_time, line in port.read_line(wait_s):
        match = STATUS_RE.search(line)
        if not match:
            continue
        last_line = line
        state, node_epoch_text = match.groups()
        node_epoch = int(node_epoch_text)
        if state == "SYNCED" and node_epoch >= MIN_VALID_EPOCH:
            return node_epoch - host_time, node_epoch, line
        return None, None, line
    return None, None, last_line


def send_rtcset(port: NodePort):
    now = time.time()
    target = int(now) + 1
    time.sleep(target - now)
    port.write_line(f"RTCSET {target}")
    replies = []
    for _host_time, line in port.read_line(1.5):
        if "RTCSET" in line or "DS3231" in line:
            replies.append(line)
        if f"RTCSET OK: epoch={target}" in line:
            return target, replies, False
    return target, replies, True


def wait_for_boot(port: NodePort, quiet_s: float, max_s: float) -> None:
    start = time.monotonic()
    last_output = start
    while time.monotonic() - start < max_s:
        got_output = False
        for _host_time, _line in port.read_line(0.2):
            got_output = True
            last_output = time.monotonic()
        if not got_output and time.monotonic() - last_output >= quiet_s:
            return


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", help="serial device; default is auto-detect")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--tolerance", type=float, default=3.0,
        help="maximum absolute offset in seconds for PASS",
    )
    parser.add_argument(
        "--read-timeout", type=float, default=7.0,
        help="seconds to wait for the periodic RTC status line",
    )
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="skip Hub NTP check")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--no-boot-wait", action="store_true")
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
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"RESULT {result}" + (f": {detail}" if detail else ""))
        return code

    if not record["hub_ntp_synced"] and not args.force:
        return finish(
            "HUB_NOT_SYNCED",
            2,
            "Pi clock is not NTP-synchronized; check timedatectl status",
        )

    port_path = args.port or find_port()
    record["port"] = port_path
    if not port_path:
        return finish("NO_PORT", 3, "no CP210x serial device found")
    try:
        port = NodePort(port_path)
    except (OSError, RuntimeError) as error:
        return finish("PORT_ERROR", 3, str(error))

    try:
        print(f"Node on {port_path}")
        if not args.no_boot_wait:
            wait_for_boot(port, quiet_s=1.0, max_s=8.0)

        before, _node_before, line = read_node_epoch(port, args.read_timeout)
        record["offset_before_s"] = None if before is None else round(before, 3)
        if before is None:
            print(f"Node RTC before: unknown ({line or 'no RTC status line seen'})")
        else:
            print(f"Node RTC before: offset {before:+.2f} s [{line}]")

        if args.check_only:
            if before is None:
                return finish("NO_REPLY", 1, "could not read the Node RTC")
            record["offset_after_s"] = record["offset_before_s"]
            ok = abs(before) <= args.tolerance
            return finish("PASS" if ok else "OUT_OF_TOLERANCE", 0 if ok else 1)

        for attempt in range(1, args.retries + 1):
            record["attempts"] = attempt
            sent, replies, rejected = send_rtcset(port)
            if rejected:
                reply = next((item for item in reversed(replies) if "RTCSET" in item), "no reply")
                print(f"Attempt {attempt}: RTCSET {sent} rejected or unacknowledged ({reply})")
                continue
            print(f"Attempt {attempt}: RTCSET {sent} accepted")
            after, _node_after, line = read_node_epoch(port, args.read_timeout)
            if after is None:
                print(f"  no SYNCED status within {args.read_timeout:.0f}s ({line}); retrying")
                continue
            record["offset_after_s"] = round(after, 3)
            print(f"  Node RTC after: offset {after:+.2f} s [{line}]")
            if abs(after) <= args.tolerance:
                return finish("PASS", 0)

        return finish(
            "FAIL", 1,
            f"offset not within +/-{args.tolerance} s after {args.retries} attempts",
        )
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
