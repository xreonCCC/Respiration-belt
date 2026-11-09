"""Runs the hub branch's real hub_ingest.py and hub_uplink.py on a fake card,
as hub_usb_export.py does, and checks what safe_to_delete() would confirm.
Needs those scripts, session_scan.py and user_store.py next to
hub_usb_export.py.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HUB))

import hub_usb_export as ux  # noqa: E402

HEADER = (
    "mono_us,unix_ms,seq,schema_version,data_source,"
    "chest_belt_low_sample_us,chest_belt_low_valid,chest_belt_low_raw,chest_belt_low_uv,"
    "chest_belt_high_sample_us,chest_belt_high_valid,chest_belt_high_raw,chest_belt_high_uv,"
    "abdomen_belt_low_sample_us,abdomen_belt_low_valid,abdomen_belt_low_raw,abdomen_belt_low_uv,"
    "abdomen_belt_high_sample_us,abdomen_belt_high_valid,abdomen_belt_high_raw,abdomen_belt_high_uv,"
    "chest_sample_us,chest_valid,chest_ax_g,chest_ay_g,chest_az_g,chest_gx_dps,chest_gy_dps,"
    "chest_gz_dps,chest_roll_deg,chest_pitch_deg,chest_yaw_deg,"
    "abdomen_sample_us,abdomen_valid,abdomen_ax_g,abdomen_ay_g,abdomen_az_g,abdomen_gx_dps,"
    "abdomen_gy_dps,abdomen_gz_dps,abdomen_roll_deg,abdomen_pitch_deg,abdomen_yaw_deg,status\n"
)


def synthetic_csv(rows: int, start_us: int = 1_000_000) -> bytes:
    """Same shape as the bring-up firmware's output (data_source=synthetic_test)."""
    lines = [HEADER]
    for i in range(rows):
        t = start_us + i * 100_000
        lines.append(f"{t},1790000000000,{i},resp_v2,synthetic_test,"
                     f"{t},1,-6300,-1537,{t},1,-220500,-53802,"
                     f"{t},1,-5200,-1268,{t},1,-182000,-44408,"
                     "0,0,nan,nan,nan,nan,nan,nan,nan,nan,nan,"
                     "0,0,nan,nan,nan,nan,nan,nan,nan,nan,nan,0x00\n")
    return "".join(lines).encode()


def run(script: str, *args: str) -> int:
    return subprocess.run([sys.executable, str(HUB / script), *args],
                          capture_output=True, text=True, check=False).returncode


@unittest.skipUnless((HUB / "hub_ingest.py").exists() and (HUB / "hub_uplink.py").exists(),
                     "needs the hub branch's hub_ingest.py and hub_uplink.py")
class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # hub_ingest.py looks for current_user.json via user_store.hub_root_for(archive)
        self.hub_root = root / "hub"
        self.card = root / "card"
        self.archive = self.hub_root / "archive"
        self.quarantine = self.hub_root / "quarantine"
        self.upload = root / "upload"
        for d in (self.card, self.archive, self.quarantine, self.upload):
            d.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def ingest(self) -> None:
        self.assertEqual(run("hub_ingest.py", "--inbox", str(self.card), "--archive",
                             str(self.archive), "--quarantine", str(self.quarantine)), 0)

    def uplink(self) -> None:
        self.assertEqual(run("hub_uplink.py", "--archive", str(self.archive),
                             "--destination", str(self.upload)), 0)

    def decide(self) -> dict[str, str]:
        decision = ux.safe_to_delete(ux.scan_card(self.card), self.archive, self.upload)
        result = {f.name: "confirm" for f in decision.confirm}
        result.update(dict(decision.keep))
        return result

    def test_full_chain(self) -> None:
        (self.card / "R0000001.CSV").write_bytes(synthetic_csv(300))
        (self.card / "R0000002.CSV").write_bytes(synthetic_csv(5))     # too short, still archived
        (self.card / "R0000003.CSV").write_bytes(b"garbage, no header\n")  # PARSE_FAILED
        (self.card / "EVENTS.CSV").write_bytes(b'unix_ms,event,session_file,rows,detail\n'
                                               b'-1,"BOOT","",0,""\n')

        self.ingest()
        before_upload = self.decide()
        self.assertEqual(before_upload["R0000001.CSV"], ux.KEEP_NOT_UPLOADED)

        self.uplink()
        after = self.decide()
        self.assertEqual(after["R0000001.CSV"], "confirm")
        self.assertEqual(after["R0000002.CSV"], "confirm")
        self.assertEqual(after["R0000003.CSV"], ux.KEEP_NOT_ARCHIVED)  # quarantined, never uplinked
        self.assertEqual(after["EVENTS.CSV"], ux.KEEP_EVENTS_FILE)

    def test_upload_corrupted_after_the_fact(self) -> None:
        (self.card / "R0000001.CSV").write_bytes(synthetic_csv(300))
        self.ingest()
        self.uplink()
        uploaded = next(self.upload.glob("*/R0000001.CSV"))
        uploaded.write_bytes(b"bit rot")
        self.assertEqual(self.decide()["R0000001.CSV"], ux.KEEP_UPLOAD_MISMATCH)

    def test_manifest_matches_hub_uplink_format(self) -> None:
        data = synthetic_csv(300)
        (self.card / "R0000001.CSV").write_bytes(data)
        self.ingest()
        self.uplink()
        manifest = json.loads((self.upload / ux.MANIFEST_NAME).read_text())
        (key, entry), = [(k, v) for k, v in manifest["files"].items() if k.endswith("R0000001.CSV")]
        self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        self.assertRegex(key, r"^\d{8}/R0000001\.CSV$")


if __name__ == "__main__":
    unittest.main()
