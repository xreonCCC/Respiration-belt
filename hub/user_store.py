"""Shared user-binding helpers for the Hub.

The Hub never edits Node firmware. Ownership is bound at ingest time:
whatever user_id is active in current_user.json when a file is ingested
owns that file. The binding is frozen into quality.json + meta.json.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

CURRENT_USER_FILENAME = "current_user.json"
DEFAULT_USER_ID = "UNKNOWN"
DEFAULT_NODE_ID = "node-01"

_SAFE_USER = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def hub_root_for(archive: Path) -> Path:
    # archive is ~/hub/archive, so hub root is its parent.
    return archive.parent


def current_user_path(hub_root: Path) -> Path:
    return hub_root / CURRENT_USER_FILENAME


def sanitize_user_id(raw: str) -> str:
    cleaned = (raw or "").strip()
    if not _SAFE_USER.match(cleaned):
        raise ValueError(
            "user_id must be 1-32 chars of letters/digits/_/- (e.g. S01)")
    return cleaned


def load_current_user(hub_root: Path) -> dict:
    path = current_user_path(hub_root)
    if not path.is_file():
        return {"user_id": DEFAULT_USER_ID, "since": None,
                "node_id": DEFAULT_NODE_ID}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"user_id": DEFAULT_USER_ID, "since": None,
                "node_id": DEFAULT_NODE_ID}
    user_id = payload.get("user_id") or DEFAULT_USER_ID
    if not isinstance(user_id, str) or not _SAFE_USER.match(user_id.strip()):
        user_id = DEFAULT_USER_ID
    else:
        user_id = user_id.strip()
    return {
        "user_id": user_id,
        "since": payload.get("since"),
        "node_id": payload.get("node_id") or DEFAULT_NODE_ID,
    }


def save_current_user(hub_root: Path, user_id: str,
                      node_id: str = DEFAULT_NODE_ID) -> dict:
    cleaned = sanitize_user_id(user_id)
    record = {
        "user_id": cleaned,
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "node_id": (node_id or DEFAULT_NODE_ID).strip() or DEFAULT_NODE_ID,
    }
    hub_root.mkdir(parents=True, exist_ok=True)
    current_user_path(hub_root).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def session_id_for(user_id: str, date_part: str) -> str:
    return f"{user_id}_{date_part}"
