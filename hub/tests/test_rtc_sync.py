"""Tests for rtc_sync.py with a fake Node (its own DS3231 clock) and a fake Pi
clock, so no test sleeps.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rtc_sync  # noqa: E402
from hub_node_protocol import ProtocolError, RtcStatus  # noqa: E402

START = 1_790_000_000.4  # Pi clock at the start of each test


class FakeClock:
    """The Pi's clock; sleep() moves it forward."""

    def __init__(self, start: float) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class FakeNode:
    """The belt, as NodeLink would present it: rtc_get() and rtc_set()."""

    def __init__(self, clock: FakeClock, offset_s: float, state: str = "SYNCED",
                 reject_sets: int = 0, sets_stick: bool = True) -> None:
        self.clock = clock
        self.offset_s = offset_s      # DS3231 time minus Pi time
        self.state = state
        self.reject_sets = reject_sets  # how many RTCSETs to reject first
        self.sets_stick = sets_stick    # False = chip "forgets" (bad battery/I2C)
        self.sets: list[int] = []

    def rtc_get(self) -> RtcStatus:
        return RtcStatus(self.state, int(self.clock.now() + self.offset_s))

    def rtc_set(self, epoch: int) -> None:
        self.sets.append(epoch)
        if self.reject_sets > 0:
            self.reject_sets -= 1
            raise ProtocolError("'RTCSET' -> ERR: rtc_write_failed")
        if self.sets_stick:
            self.offset_s = epoch - self.clock.now()
            self.state = "SYNCED"


def run(node: FakeNode, clock: FakeClock, check_only: bool = False, retries: int = 3):
    record: dict = {}
    result = rtc_sync.sync(node, record, retries=retries, tolerance=3.0,
                           check_only=check_only, now=clock.now, sleep=clock.sleep)
    return result, record


class SyncTests(unittest.TestCase):
    def test_wrong_clock_gets_fixed(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=-3600)  # belt is an hour behind
        (result, code, _), record = run(node, clock)
        self.assertEqual((result, code), ("PASS", 0))
        self.assertAlmostEqual(record["offset_before_s"], -3600, delta=1)
        self.assertLessEqual(abs(record["offset_after_s"]), 1)
        self.assertEqual(len(node.sets), 1)

    def test_set_happens_on_a_second_boundary(self) -> None:
        clock = FakeClock(START)  # .4 s into a second
        node = FakeNode(clock, offset_s=-3600)
        run(node, clock)
        self.assertEqual(node.sets, [int(START) + 1])  # waited for the next whole second

    def test_never_set_rtc_unset(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=0, state="UNSET")  # fresh chip / dead coin cell
        (result, code, _), record = run(node, clock)
        self.assertEqual((result, code), ("PASS", 0))
        self.assertIsNone(record["offset_before_s"])  # UNSET time is not measured

    def test_retries_after_a_rejected_set(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=-50, reject_sets=1)
        (result, code, _), record = run(node, clock)
        self.assertEqual((result, code), ("PASS", 0))
        self.assertEqual(record["attempts"], 2)

    def test_gives_up_when_the_chip_never_keeps_the_time(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=-50, sets_stick=False)
        (result, code, _), record = run(node, clock, retries=3)
        self.assertEqual((result, code), ("FAIL", 1))
        self.assertEqual(record["attempts"], 3)


class CheckOnlyTests(unittest.TestCase):
    def test_in_tolerance(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=1)
        (result, code, _), _ = run(node, clock, check_only=True)
        self.assertEqual((result, code), ("PASS", 0))
        self.assertEqual(node.sets, [])  # check-only never writes

    def test_out_of_tolerance(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=10)
        (result, code, _), _ = run(node, clock, check_only=True)
        self.assertEqual((result, code), ("OUT_OF_TOLERANCE", 1))
        self.assertEqual(node.sets, [])

    def test_unset_rtc_is_out_of_tolerance(self) -> None:
        clock = FakeClock(START)
        node = FakeNode(clock, offset_s=0, state="UNSET")
        (result, code, _), _ = run(node, clock, check_only=True)
        self.assertEqual((result, code), ("OUT_OF_TOLERANCE", 1))


class CompatibilityTests(unittest.TestCase):
    def test_hub_usb_export_can_still_import_it(self) -> None:
        self.assertTrue(callable(rtc_sync.hub_clock_synced))


if __name__ == "__main__":
    unittest.main()
