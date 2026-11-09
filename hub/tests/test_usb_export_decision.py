"""Tests for the delete decisions in hub_usb_export.py, on temporary folders.

"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hub_usb_export as ux  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.archive = root / "archive"
        self.destination = root / "upload"
        self.archive.mkdir()
        self.destination.mkdir()
        self.manifest: dict = {"files": {}}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # Helpers that build the same layout hub_ingest.py / hub_uplink.py produce.

    def archived(self, name: str, data: bytes, date: str = "20261021") -> None:
        path = self.archive / date / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def uploaded(self, name: str, data: bytes, date: str = "20261021",
                 write_file: bool = True, manifest_sha: str | None = None) -> None:
        if write_file:
            path = self.destination / date / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.manifest["files"][f"{date}/{name}"] = {
            "size": len(data), "sha256": manifest_sha or sha(data)}
        (self.destination / ux.MANIFEST_NAME).write_text(json.dumps(self.manifest))

    def decide(self, *sources: tuple[str, bytes]) -> dict[str, str]:
        files = [ux.SourceFile(n, len(d), sha(d)) for n, d in sources]
        decision = ux.safe_to_delete(files, self.archive, self.destination)
        result = {f.name: "confirm" for f in decision.confirm}
        result.update(dict(decision.keep))
        self.assertEqual(len(result), len(files), "every file must be decided exactly once")
        return result

    def test_fully_backed_up_is_confirmed(self) -> None:
        self.archived("R0000007.CSV", b"night 7")
        self.uploaded("R0000007.CSV", b"night 7")
        self.assertEqual(self.decide(("R0000007.CSV", b"night 7")),
                         {"R0000007.CSV": "confirm"})

    def test_test_file_backed_up_is_confirmed(self) -> None:
        self.archived("T0000005.CSV", b"selftest")
        self.uploaded("T0000005.CSV", b"selftest")
        self.assertEqual(self.decide(("T0000005.CSV", b"selftest")),
                         {"T0000005.CSV": "confirm"})

    def test_events_file_is_never_confirmed(self) -> None:
        self.archived("EVENTS.CSV", b"ev")
        self.uploaded("EVENTS.CSV", b"ev")
        self.assertEqual(self.decide(("EVENTS.CSV", b"ev")),
                         {"EVENTS.CSV": ux.KEEP_EVENTS_FILE})

    def test_not_archived(self) -> None:  # e.g. it went to quarantine
        self.assertEqual(self.decide(("R0000001.CSV", b"x")),
                         {"R0000001.CSV": ux.KEEP_NOT_ARCHIVED})

    def test_archive_has_different_bytes(self) -> None:
        self.archived("R0000002.CSV", b"old copy")
        self.uploaded("R0000002.CSV", b"old copy")
        self.assertEqual(self.decide(("R0000002.CSV", b"new bytes on the card")),
                         {"R0000002.CSV": ux.KEEP_ARCHIVE_MISMATCH})

    def test_upload_failed(self) -> None:  # acceptance test 6
        self.archived("R0000003.CSV", b"n3")
        self.assertEqual(self.decide(("R0000003.CSV", b"n3")),
                         {"R0000003.CSV": ux.KEEP_NOT_UPLOADED})

    def test_manifest_hash_disagrees(self) -> None:
        self.archived("R0000004.CSV", b"n4")
        self.uploaded("R0000004.CSV", b"n4", manifest_sha="0" * 64)
        self.assertEqual(self.decide(("R0000004.CSV", b"n4")),
                         {"R0000004.CSV": ux.KEEP_UPLOAD_MISMATCH})

    def test_manifest_says_uploaded_but_file_is_gone(self) -> None:
        self.archived("R0000005.CSV", b"n5")
        self.uploaded("R0000005.CSV", b"n5", write_file=False)
        self.assertEqual(self.decide(("R0000005.CSV", b"n5")),
                         {"R0000005.CSV": ux.KEEP_UPLOAD_MISSING})

    def test_mixed_batch(self) -> None:
        self.archived("R0000007.CSV", b"a")
        self.uploaded("R0000007.CSV", b"a")
        self.archived("R0000008.CSV", b"b")
        result = self.decide(("R0000007.CSV", b"a"), ("R0000008.CSV", b"b"),
                             ("EVENTS.CSV", b"e"))
        self.assertEqual(result, {"R0000007.CSV": "confirm",
                                  "R0000008.CSV": ux.KEEP_NOT_UPLOADED,
                                  "EVENTS.CSV": ux.KEEP_EVENTS_FILE})


if __name__ == "__main__":
    unittest.main()


class ScanTests(unittest.TestCase):
    def test_scan_hashes_only_node_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            card = Path(tmp)
            (card / "R0000007.CSV").write_bytes(b"night")
            (card / "T0000005.CSV").write_bytes(b"selftest")
            (card / "EVENTS.CSV").write_bytes(b"ev")
            (card / "NOTES.TXT").write_bytes(b"ignore me")
            (card / "System Volume Information").mkdir()
            names = {f.name: f.sha256 for f in ux.scan_card(card)}
            self.assertEqual(set(names), {"R0000007.CSV", "T0000005.CSV", "EVENTS.CSV"})
            self.assertEqual(names["R0000007.CSV"], sha(b"night"))
            without_t = {f.name for f in ux.scan_card(card, include_test_files=False)}
            self.assertNotIn("T0000005.CSV", without_t)


class FakeLink:
    """Answers delete_ok() from a script: a DeleteResult, or an exception."""

    def __init__(self, script: dict) -> None:
        self.script = script
        self.sent: list[str] = []

    def delete_ok(self, name: str, sha256: str):
        self.sent.append(name)
        outcome = self.script[name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ConfirmDeletesTests(unittest.TestCase):
    def files(self, *names: str) -> list:
        return [ux.SourceFile(n, 1, "a" * 64) for n in names]

    def test_failures_dont_stop_the_batch(self) -> None:
        link = FakeLink({
            "R1.CSV": ux.DeleteResult("R1.CSV", True),
            "R2.CSV": ux.DeleteResult("R2.CSV", False, "sha_mismatch"),
            "R3.CSV": ux.DeleteResult("R3.CSV", False, "not_found"),
        })
        results = ux.confirm_deletes(link, self.files("R1.CSV", "R2.CSV", "R3.CSV"))
        self.assertEqual([r.ok for r in results], [True, False, False])
        self.assertEqual(link.sent, ["R1.CSV", "R2.CSV", "R3.CSV"])

    def test_lost_link_stops_the_batch(self) -> None:  # cable pulled mid-way
        link = FakeLink({
            "R1.CSV": ux.DeleteResult("R1.CSV", True),
            "R2.CSV": ux.NodeTimeout("no reply"),
            "R3.CSV": ux.DeleteResult("R3.CSV", True),
        })
        results = ux.confirm_deletes(link, self.files("R1.CSV", "R2.CSV", "R3.CSV"))
        self.assertEqual(link.sent, ["R1.CSV", "R2.CSV"])  # R3 never sent
        self.assertFalse(results[-1].ok)
        self.assertTrue(results[-1].reason.startswith("link_lost"))
