#!/usr/bin/env python3
"""hub-uplink: push archived sessions to a server, resumable.

Version 1 keeps it honest and simple:
- walks the archive, oldest date folder first
- copies every file that the destination does not have yet (size match)
- verifies with SHA-256 after copy
- writes upload.log next to this script's caller output via stdout only

The destination can be:
- an external drive mount point (offline "upload")
- a server path already mounted (sshfs/NFS)
Later versions can switch to rclone/rsync/HTTP; the manifest logic stays.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_NAME = "upload.manifest.json"


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(destination: Path) -> dict:
    manifest_path = destination / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("manifest unreadable; rebuilding", file=sys.stderr)
    return {"files": {}}


def save_manifest(destination: Path, manifest: dict) -> None:
    manifest_path = destination / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def uplink(archive: Path, destination: Path) -> int:
    if not archive.is_dir():
        print(f"archive not found: {archive}", file=sys.stderr)
        return 2
    destination.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(destination)

    pending: list[Path] = sorted(
        (p for p in archive.glob("*/*") if p.is_file() and p.suffix.upper() in (".CSV", ".JSON")),
        key=lambda p: (p.parent.name, p.name),
    )
    uploaded = skipped = failed = 0
    for path in pending:
        relative = f"{path.parent.name}/{path.name}"
        size = path.stat().st_size
        record = manifest["files"].get(relative)
        target = destination / relative
        source_hash = None
        if record and record.get("size") == size and target.is_file():
            try:
                source_hash = sha256_of(path)
                if record.get("sha256") == source_hash and sha256_of(target) == source_hash:
                    skipped += 1
                    continue
            except OSError:
                source_hash = None
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(path.read_bytes())
            checksum = sha256_of(target)
            if source_hash is None:
                source_hash = sha256_of(path)
            if checksum != source_hash:
                raise IOError("checksum mismatch after copy")
            manifest["files"][relative] = {
                "size": size,
                "sha256": checksum,
            }
            uploaded += 1
            print(f"uploaded {relative}")
        except OSError as error:
            failed += 1
            print(f"FAILED {relative}: {error}", file=sys.stderr)
        save_manifest(destination, manifest)

    print(f"uplink done: uploaded={uploaded} skipped={skipped} failed={failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload archived sessions")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return uplink(parser.parse_args().archive, parser.parse_args().destination)


if __name__ == "__main__":
    raise SystemExit(main())
