#!/usr/bin/env python3
"""Handle one Node plug-in end to end.

Started by hub-usb-export@<sdX1>.service when the Node's card appears. The
card is unmounted before any DELETE_OK (the spec has the opposite order)
because the Node must take the card back to delete. Nothing is kept between
runs: what is safe to delete is recomputed from the card, the archive and
the upload manifest, so a reboot mid-run is harmless.

    sudo python3 hub_usb_export.py --device /dev/sda1 --destination ~/hub/upload --no-delete
"""
from __future__ import annotations

import argparse
import json
import pwd
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hub_ingest import sha256_of  # noqa: E402
from hub_node_protocol import (  # noqa: E402
    DeleteResult,
    NodeLink,
    NodeTimeout,
    ProtocolError,
    filename_allowed,
    find_node_port,
)

MANIFEST_NAME = "upload.manifest.json"  # written by hub_uplink.py
MOUNT_OPTIONS = "ro,nosuid,nodev,noexec"  # from the spec, do not loosen

# Why a file stays on the Node (recorded in the transfer log).
KEEP_EVENTS_FILE = "events_never_deleted"
KEEP_BAD_FILENAME = "bad_filename"
KEEP_NOT_ARCHIVED = "not_archived"
KEEP_ARCHIVE_MISMATCH = "archive_mismatch"
KEEP_NOT_UPLOADED = "not_uploaded"
KEEP_UPLOAD_MISMATCH = "upload_mismatch"
KEEP_UPLOAD_MISSING = "upload_missing"


@dataclass(frozen=True)
class SourceFile:
    """One file on the Node's card, hashed while the card was mounted."""
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Decision:
    confirm: list[SourceFile]          # send DELETE_OK for these
    keep: list[tuple[str, str]]        # (name, KEEP_* reason)


@dataclass
class TransferLog:
    started_at: str
    device: str
    node_serial: Optional[str] = None
    port: Optional[str] = None
    steps: list[dict] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
    deletes: list[dict] = field(default_factory=list)
    result: str = "RUNNING"

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": ok, "detail": detail,
                           "at": datetime.now(timezone.utc).isoformat()})
        print(f"[{'ok' if ok else 'FAIL'}] {name} {detail}".rstrip(), flush=True)


def usb_serial_of(device: Path) -> Optional[str]:
    """ID_SERIAL_SHORT of the USB device behind a block device, via udevadm."""
    out = subprocess.run(
        ["udevadm", "info", "--query=property", f"--name={device}"],
        capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if line.startswith("ID_SERIAL_SHORT="):
            return line.split("=", 1)[1] or None
    return None


def wait_for_port(usb_serial: Optional[str], timeout_s: float = 10.0) -> Optional[str]:
    """The tty and the block device arrive as separate udev events in either
    order, so poll briefly for the same Node's serial port.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        port = find_node_port(usb_serial)
        if port:
            return port
        time.sleep(0.5)
    return None


def mount_readonly(device: Path, mountpoint: Path, run_as: str) -> None:
    mountpoint.mkdir(parents=True, exist_ok=True)
    # FAT has no owners; uid/gid set who can read. Numeric IDs avoid user name
    # lookups in mount(8).
    user = pwd.getpwnam(run_as)
    options = f"{MOUNT_OPTIONS},uid={user.pw_uid},gid={user.pw_gid}"
    subprocess.run(["mount", "-t", "vfat", "-o", options, str(device), str(mountpoint)],
                   check=True)


def unmount(mountpoint: Path) -> None:
    subprocess.run(["umount", str(mountpoint)], check=True)


def is_mounted(mountpoint: Path) -> bool:
    return subprocess.run(["mountpoint", "-q", str(mountpoint)], check=False).returncode == 0


def run_as_user(user: str, argv: list[str]) -> int:
    """Run a Hub script as the normal Hub user, so archive files aren't root's."""
    return subprocess.run(["runuser", "-u", user, "--", sys.executable, *argv],
                          check=False).returncode


def scan_card(mountpoint: Path, include_test_files: bool = True) -> list[SourceFile]:
    """SHA-256 of each relevant file on the card, before ingest touches it."""
    files: list[SourceFile] = []
    for path in sorted(mountpoint.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        upper = name.upper()
        # FAT is case-insensitive; Linux shows 8.3 names as stored (upper case).
        wanted = (upper == "EVENTS.CSV"
                  or (upper.startswith("R") and upper.endswith(".CSV"))
                  or (include_test_files and upper.startswith("T") and upper.endswith(".CSV")))
        if not wanted or not filename_allowed(name):
            continue
        files.append(SourceFile(name=name, size=path.stat().st_size, sha256=sha256_of(path)))
    return files


def _load_manifest(destination: Path) -> dict:
    try:
        return json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"files": {}}  # no manifest = nothing proven uploaded


def safe_to_delete(sources: list[SourceFile], archive: Path, destination: Path,
                   rehash_destination: bool = True) -> Decision:
    """Which files can be confirmed for deletion. Reads files, changes nothing.

    First failing rule wins, so the reason in the log is the earliest gap in
    the chain card -> archive -> upload.
    """
    manifest = _load_manifest(destination).get("files", {})
    confirm: list[SourceFile] = []
    keep: list[tuple[str, str]] = []

    for src in sources:
        # EVENTS.CSV: ingest replaces the archived copy with the Node's, so a
        # fresh file would shrink the archive. Never deleted (the Node refuses
        # too).
        if src.name.upper() == "EVENTS.CSV":
            keep.append((src.name, KEEP_EVENTS_FILE))
            continue
        if not filename_allowed(src.name):
            keep.append((src.name, KEEP_BAD_FILENAME))
            continue

        # Archived with the same bytes? Ingest picks the date folder;
        # quarantined files are not under archive/ and stay on the Node.
        copies = sorted(archive.glob(f"*/{src.name}"))
        if not copies:
            keep.append((src.name, KEEP_NOT_ARCHIVED))
            continue
        archived = next((c for c in copies if sha256_of(c) == src.sha256), None)
        if archived is None:
            keep.append((src.name, KEEP_ARCHIVE_MISMATCH))
            continue

        # Uploaded? The manifest key is "<date>/<name>", using the date folder
        # ingest chose.
        relative = f"{archived.parent.name}/{src.name}"
        entry = manifest.get(relative)
        if entry is None:
            keep.append((src.name, KEEP_NOT_UPLOADED))
            continue
        if entry.get("sha256") != src.sha256:
            keep.append((src.name, KEEP_UPLOAD_MISMATCH))
            continue

        # Re-hash the uploaded copy instead of trusting the manifest.
        target = destination / relative
        if not target.is_file():
            keep.append((src.name, KEEP_UPLOAD_MISSING))
            continue
        if rehash_destination and sha256_of(target) != src.sha256:
            keep.append((src.name, KEEP_UPLOAD_MISMATCH))
            continue

        confirm.append(src)
    return Decision(confirm=confirm, keep=keep)


def confirm_deletes(link: NodeLink, files: list[SourceFile]) -> list[DeleteResult]:
    """DELETE_OK each file in turn. "not_found" counts as done (an earlier ACK
    was lost). A timeout or serial error stops the loop; the rest wait for
    the next plug-in.
    """
    results: list[DeleteResult] = []
    for src in files:
        try:
            result = link.delete_ok(src.name, src.sha256)
        except (NodeTimeout, OSError) as error:
            results.append(DeleteResult(src.name, False, f"link_lost: {error}"))
            break
        except ProtocolError as error:
            results.append(DeleteResult(src.name, False, f"protocol: {error}"))
            continue
        results.append(result)
    return results


def node_housekeeping(link: NodeLink, log: TransferLog, state_dir: Path) -> None:
    """Battery and RTC. Failures are logged and never block the data copy."""
    try:
        log.step("ping", link.ping())
    except (ProtocolError, OSError) as error:
        log.step("ping", False, str(error))
        return

    try:
        batt = link.battery()
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "node_battery.json").write_text(json.dumps({
            "node_serial": log.node_serial,
            "percent": batt.percent,
            "voltage_mv": batt.voltage_mv,
            "charging": batt.charging,
            "read_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        log.step("battery", True, f"charging={batt.charging} percent={batt.percent}")
    except (ProtocolError, OSError) as error:
        log.step("battery", False, str(error))

    try:
        from rtc_sync import hub_clock_synced  # timedatectl NTPSynchronized
    except ImportError:
        hub_clock_synced = None
    if hub_clock_synced is None or not hub_clock_synced():
        log.step("rtc", False, "Pi clock not NTP-synced; not touching the Node RTC")
        return
    try:
        target = int(time.time())
        link.rtc_set(target)
        status = link.rtc_get()
        offset = status.epoch - int(time.time())
        ok = status.state == "SYNCED" and abs(offset) <= 2
        log.step("rtc", ok, f"state={status.state} offset={offset:+d}s")
    except (ProtocolError, OSError) as error:
        log.step("rtc", False, str(error))


def write_state(state_dir: Path, log: TransferLog) -> None:
    out = state_dir / "usb_export"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "transfers.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(log), ensure_ascii=False) + "\n")
    (out / "last.json").write_text(json.dumps(asdict(log), ensure_ascii=False, indent=2),
                                   encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    log = TransferLog(started_at=datetime.now(timezone.utc).isoformat(), device=str(args.device))
    hub_dir = Path(__file__).resolve().parent
    link: Optional[NodeLink] = None
    try:
        log.node_serial = usb_serial_of(args.device)
        log.port = wait_for_port(log.node_serial)
        log.step("find_port", log.port is not None, log.port or "no CDC port: copy only, no deletes")
        if log.port:
            link = NodeLink(log.port).open()

        if link:
            node_housekeeping(link, log, args.state_dir)

        mount_readonly(args.device, args.mountpoint, args.run_as)
        log.step("mount", True, str(args.mountpoint))
        sources = scan_card(args.mountpoint, include_test_files=not args.skip_test_files)
        log.step("scan", True, f"{len(sources)} file(s)")

        rc = run_as_user(args.run_as, [str(hub_dir / "hub_ingest.py"),
                                       "--inbox", str(args.mountpoint),
                                       "--archive", str(args.archive),
                                       "--quarantine", str(args.quarantine)])
        log.step("ingest", rc == 0, f"exit {rc}")
        rc = run_as_user(args.run_as, [str(hub_dir / "hub_uplink.py"),
                                       "--archive", str(args.archive),
                                       "--destination", str(args.destination)])
        log.step("uplink", rc == 0, f"exit {rc}")
        # A failed uplink is not fatal: only uploaded files get confirmed.

        decision = safe_to_delete(sources, args.archive, args.destination)
        log.kept = [{"file": name, "reason": reason} for name, reason in decision.keep]
        log.step("decide", True, f"confirm={len(decision.confirm)} keep={len(decision.keep)}")

        # Unmount before the Node takes the card back.
        unmount(args.mountpoint)
        log.step("umount", True)

        if args.no_delete:
            log.step("delete", True, "skipped (--no-delete)")
        elif link and decision.confirm:
            results = confirm_deletes(link, decision.confirm)
            log.deletes = [asdict(r) for r in results]
            done = sum(1 for r in results if r.ok or r.reason == "not_found")
            log.step("delete", done == len(decision.confirm),
                     f"{done}/{len(decision.confirm)} deleted")
        log.result = "OK"
        return 0
    except Exception as error:  # noqa: BLE001
        log.step("error", False, f"{type(error).__name__}: {error}")
        log.result = "FAILED"
        return 1
    finally:
        if is_mounted(args.mountpoint):
            subprocess.run(["umount", str(args.mountpoint)], check=False)
        if link:
            link.close()
        write_state(args.state_dir, log)


def main() -> int:
    home = Path("/home/xreon/hub")
    parser = argparse.ArgumentParser(description="Handle one Node USB export")
    parser.add_argument("--device", type=Path, required=True, help="e.g. /dev/sda1")
    parser.add_argument("--mountpoint", type=Path, default=Path("/run/respact/node"))
    parser.add_argument("--archive", type=Path, default=home / "archive")
    parser.add_argument("--quarantine", type=Path, default=home / "quarantine")
    parser.add_argument("--destination", type=Path, required=True,
                        help="upload target passed to hub_uplink.py")
    parser.add_argument("--state-dir", type=Path, default=home / "state")
    parser.add_argument("--run-as", default="xreon", help="user for ingest/uplink")
    parser.add_argument("--skip-test-files", action="store_true", help="ignore T*.CSV")
    parser.add_argument("--no-delete", action="store_true",
                        help="copy and verify, never send DELETE_OK")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
