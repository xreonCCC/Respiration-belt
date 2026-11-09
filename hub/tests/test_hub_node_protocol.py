"""Tests for hub_node_protocol.py. No Node or serial port needed."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hub_node_protocol import (  # noqa: E402
    Battery,
    DeleteResult,
    NodeLink,
    NodeStatus,
    NodeTimeout,
    ProtocolError,
    RtcStatus,
    filename_allowed,
    parse_battery,
    parse_delete_reply,
    parse_rtc_status,
    parse_rtcset_reply,
    parse_status,
)

SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


class FakePort:
    """Fake serial.Serial. `replies` maps a command line to the Node's raw
    reply, which readline() returns in small pieces.
    """

    def __init__(self, replies: dict[bytes, bytes]) -> None:
        self.replies = replies
        self.written: list[bytes] = []
        self.pending = b""

    def write(self, data: bytes) -> int:
        self.written.append(data)
        self.pending += self.replies.get(data, b"")
        return len(data)

    def readline(self) -> bytes:
        # Return at most 5 bytes per call, so command() must reassemble.
        chunk, self.pending = self.pending[:5], self.pending[5:]
        return chunk

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass


class FilenameTests(unittest.TestCase):
    def test_allowed(self) -> None:
        for name in ("R0000001.CSV", "T0000005.CSV", "EVENTS.CSV"):
            self.assertTrue(filename_allowed(name), name)

    def test_rejected(self) -> None:
        for name in ("", "R", "X0000001.CSV", "R/../a", "R..CSV", "events.csv",
                     "R0000001 .CSV", "R" + "0" * 40 + ".CSV"):
            self.assertFalse(filename_allowed(name), name)


class ParserTests(unittest.TestCase):
    def test_rtcset(self) -> None:
        parse_rtcset_reply("RTCSET OK: epoch=1790000000", 1790000000)
        with self.assertRaises(ProtocolError):
            parse_rtcset_reply("RTCSET OK: epoch=1790000001", 1790000000)
        with self.assertRaises(ProtocolError):
            parse_rtcset_reply("PONG", 1790000000)

    def test_rtc_status(self) -> None:
        self.assertEqual(parse_rtc_status("RTC STATUS: state=SYNCED,epoch=1790000001"),
                         RtcStatus("SYNCED", 1790000001))
        self.assertEqual(parse_rtc_status("RTC STATUS: state=UNSET,epoch=-1"),
                         RtcStatus("UNSET", -1))
        with self.assertRaises(ProtocolError):
            parse_rtc_status("RTC STATUS: garbage")

    def test_battery(self) -> None:
        self.assertEqual(parse_battery("BATT:PERCENT=85,VOLTAGE_MV=3920,CHARGING=1"),
                         Battery(85, 3920, True))
        # RespBelt V1.0 cannot measure voltage: the Node sends -1.
        self.assertEqual(parse_battery("BATT:PERCENT=-1,VOLTAGE_MV=-1,CHARGING=0"),
                         Battery(None, None, False))
        with self.assertRaises(ProtocolError):
            parse_battery("BATT:PERCENT=85")

    def test_delete(self) -> None:
        self.assertEqual(parse_delete_reply("DELETE ACK: R0000001.CSV", "R0000001.CSV"),
                         DeleteResult("R0000001.CSV", True))
        self.assertEqual(
            parse_delete_reply("DELETE FAIL: R0000001.CSV,reason=not_found", "R0000001.CSV"),
            DeleteResult("R0000001.CSV", False, "not_found"))
        with self.assertRaises(ProtocolError):  # reply for a different file
            parse_delete_reply("DELETE ACK: R0000002.CSV", "R0000001.CSV")

    def test_status(self) -> None:
        self.assertEqual(parse_status("STATUS: state=USB_EXPORT,owner=USB"),
                         NodeStatus("USB_EXPORT", "USB"))


class LinkTests(unittest.TestCase):
    def link(self, replies: dict[bytes, bytes], timeout_s: float = 0.3) -> NodeLink:
        return NodeLink("fake", timeout_s=timeout_s, port=FakePort(replies)).open()

    def test_ping_reassembles_partial_reads(self) -> None:
        link = self.link({b"PING\n": b"PONG\r\n"})
        self.assertTrue(link.ping())
        self.assertEqual(link._port.written, [b"PING\n"])

    def test_timeout(self) -> None:
        link = self.link({}, timeout_s=0.2)
        with self.assertRaises(NodeTimeout):
            link.command("PING")

    def test_err_reply(self) -> None:
        link = self.link({b"HELLO\n": b"ERR: unknown_command\n"})
        with self.assertRaises(ProtocolError):
            link.command("HELLO")

    def test_delete_ok_wire_format(self) -> None:
        cmd = f"DELETE_OK R0000001.CSV {SHA}\n".encode()
        link = self.link({cmd: b"DELETE ACK: R0000001.CSV\n"})
        self.assertTrue(link.delete_ok("R0000001.CSV", SHA).ok)

    def test_delete_ok_refuses_bad_input_locally(self) -> None:
        link = self.link({})
        with self.assertRaises(ValueError):
            link.delete_ok("../etc/passwd", SHA)
        with self.assertRaises(ValueError):
            link.delete_ok("R0000001.CSV", "abc")
        self.assertEqual(link._port.written, [])  # nothing reached the wire


if __name__ == "__main__":
    unittest.main()
