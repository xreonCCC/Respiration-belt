#!/usr/bin/env python3
"""Serial client for the Node's USB CDC port.

Mirrors node/usb_link_bringup/main/node_protocol.h; reply formats must match
it exactly. Manual test with a Node plugged in:

    python3 hub_node_protocol.py [/dev/ttyACM0]
"""
from __future__ import annotations

import glob
import re
import time
from dataclasses import dataclass
from typing import Optional

try:
    import serial
except ImportError:  # tests use a fake port
    serial = None  # type: ignore[assignment]


BAUD = 115200  # ignored by CDC-ACM, but pyserial wants a number
DEFAULT_TIMEOUT_S = 2.0
# The Node re-hashes the whole file before deleting. Keep this at about twice
# the "hashed ... in <ms>" time it logs for a full night's file.
DELETE_TIMEOUT_S = 300.0

MAX_FILENAME = 32
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_FILENAME_RE = re.compile(r"^[RT][0-9A-Za-z_.-]+$")

# Replies must fullmatch(); anything else is a ProtocolError.
_RTCSET_OK_RE = re.compile(r"RTCSET OK: epoch=(-?\d+)")
_RTC_STATUS_RE = re.compile(r"RTC STATUS: state=([A-Z_]+),epoch=(-?\d+)")
_BATT_RE = re.compile(r"BATT:PERCENT=(-?\d+),VOLTAGE_MV=(-?\d+),CHARGING=([01])")
_DELETE_ACK_RE = re.compile(r"DELETE ACK: (\S+)")
_DELETE_FAIL_RE = re.compile(r"DELETE FAIL: ([^,\s]+),reason=([a-z_]+)")
_STATUS_RE = re.compile(r"STATUS: state=([A-Z_]+),owner=([A-Z]+)")


class ProtocolError(RuntimeError):
    """The Node replied, but not with what this command expects."""


class NodeTimeout(ProtocolError):
    """No complete reply line within the timeout."""


@dataclass(frozen=True)
class RtcStatus:
    state: str  # "SYNCED" | "UNSET" | "ERROR"
    epoch: int


@dataclass(frozen=True)
class Battery:
    percent: Optional[int]     # None when the Node reports -1 (not measurable)
    voltage_mv: Optional[int]  # None when the Node reports -1
    charging: bool


@dataclass(frozen=True)
class DeleteResult:
    filename: str
    ok: bool
    reason: Optional[str] = None  # set when ok is False, e.g. "not_found"


@dataclass(frozen=True)
class NodeStatus:
    state: str  # export_fsm state name, e.g. "USB_EXPORT"
    owner: str  # "APP" | "USB" | "NONE"


def filename_allowed(name: str) -> bool:
    """Same rules as node_proto_filename_allowed() on the Node."""
    if name == "EVENTS.CSV":
        return True
    return (2 <= len(name) <= MAX_FILENAME
            and ".." not in name
            and _FILENAME_RE.match(name) is not None)


def _match(pattern: re.Pattern, line: str, what: str) -> re.Match:
    match = pattern.fullmatch(line)
    if match is None:
        raise ProtocolError(f"unexpected {what} reply: {line!r}")
    return match


def parse_rtcset_reply(line: str, expected_epoch: int) -> None:
    got = int(_match(_RTCSET_OK_RE, line, "RTCSET").group(1))
    if got != expected_epoch:
        raise ProtocolError(f"RTCSET echoed {got}, sent {expected_epoch}")


def parse_rtc_status(line: str) -> RtcStatus:
    m = _match(_RTC_STATUS_RE, line, "RTCGET")
    return RtcStatus(m.group(1), int(m.group(2)))


def parse_battery(line: str) -> Battery:
    m = _match(_BATT_RE, line, "BATT?")

    def measured(text: str) -> Optional[int]:
        value = int(text)
        return None if value < 0 else value

    return Battery(measured(m.group(1)), measured(m.group(2)), m.group(3) == "1")


def parse_delete_reply(line: str, filename: str) -> DeleteResult:
    ack = _DELETE_ACK_RE.fullmatch(line)
    if ack is not None:
        if ack.group(1) != filename:
            raise ProtocolError(f"ACK for {ack.group(1)}, asked about {filename}")
        return DeleteResult(filename, True)
    fail = _match(_DELETE_FAIL_RE, line, "DELETE_OK")
    if fail.group(1) != filename:
        raise ProtocolError(f"FAIL for {fail.group(1)}, asked about {filename}")
    return DeleteResult(filename, False, fail.group(2))


def parse_status(line: str) -> NodeStatus:
    m = _match(_STATUS_RE, line, "STATUS")
    return NodeStatus(m.group(1), m.group(2))


def find_node_port(usb_serial: Optional[str] = None) -> Optional[str]:
    """The Node's /dev/serial/by-id/ path, or None. by-id names contain the USB
    serial number, so the right belt is found even when two are plugged in.
    Without usb_serial, the first Node found is used.
    """
    if usb_serial:
        pattern = f"/dev/serial/by-id/*{usb_serial}*"
    else:
        pattern = "/dev/serial/by-id/*Sleep_Monitor_TF_Card_Export*"
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


class NodeLink:
    """One open serial connection to one Node. Tests pass a fake `port`."""

    def __init__(self, port_path: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                 port: object = None) -> None:
        self.port_path = port_path
        self.timeout_s = timeout_s
        self._port = port
        self._buf = b""

    def open(self) -> "NodeLink":
        if self._port is None:
            if serial is None:
                raise RuntimeError("pyserial is not installed; see hub/requirements.txt")
            # exclusive: fail if another program has the port open. The short
            # read timeout lets command() enforce its own deadline.
            self._port = serial.Serial(self.port_path, BAUD, timeout=0.1, exclusive=True)
        self._port.reset_input_buffer()
        return self

    def close(self) -> None:
        if self._port is not None:
            self._port.close()
            self._port = None

    def __enter__(self) -> "NodeLink":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def command(self, line: str, timeout_s: Optional[float] = None) -> str:
        """Send one command, return the Node's one reply line (stripped)."""
        if self._port is None:
            raise RuntimeError("NodeLink is not open")
        # Drop stale input so an old reply cannot be taken for this one.
        self._port.reset_input_buffer()
        self._buf = b""
        self._port.write((line + "\n").encode("ascii"))

        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        while time.monotonic() < deadline:
            # readline() may return part of a line on timeout.
            self._buf += self._port.readline()
            if not self._buf.endswith(b"\n"):
                continue
            reply = self._buf.decode("ascii", errors="replace").strip()
            self._buf = b""
            if not reply:
                continue  # skip blank lines
            if reply.startswith("ERR:"):
                raise ProtocolError(f"{line!r} -> {reply}")
            return reply
        raise NodeTimeout(f"{line!r}: no reply within {timeout_s or self.timeout_s:.1f} s")

    def ping(self) -> bool:
        return self.command("PING") == "PONG"

    def rtc_set(self, epoch: int) -> None:
        parse_rtcset_reply(self.command(f"RTCSET {epoch}"), epoch)

    def rtc_get(self) -> RtcStatus:
        return parse_rtc_status(self.command("RTCGET"))

    def battery(self) -> Battery:
        return parse_battery(self.command("BATT?"))

    def status(self) -> NodeStatus:
        return parse_status(self.command("STATUS"))

    def delete_ok(self, filename: str, sha256: str,
                  timeout_s: float = DELETE_TIMEOUT_S) -> DeleteResult:
        if not filename_allowed(filename):
            raise ValueError(f"refusing to send bad filename {filename!r}")
        if not SHA256_HEX_RE.match(sha256):
            raise ValueError(f"bad sha256 for {filename}")
        return parse_delete_reply(
            self.command(f"DELETE_OK {filename} {sha256}", timeout_s), filename)

    def delete_abort(self, filename: str) -> str:
        if not filename_allowed(filename):
            raise ValueError(f"refusing to send bad filename {filename!r}")
        return self.command(f"DELETE_ABORT {filename}")


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else find_node_port()
    if not path:
        raise SystemExit("no Node serial port under /dev/serial/by-id/")
    with NodeLink(path) as link:
        started = time.monotonic()
        print("PING   ->", link.ping(), f"({(time.monotonic() - started) * 1000:.0f} ms)")
        print("STATUS ->", link.status())
        print("RTCGET ->", link.rtc_get())
        print("BATT?  ->", link.battery())
