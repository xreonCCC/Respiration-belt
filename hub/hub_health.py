#!/usr/bin/env python3
"""hub-health: tiny status page for the Hub.

Serves two endpoints on the Raspberry Pi:
    /health   JSON: archive size, newest session, disk free
    /         human-readable one-page summary

This is the first step toward the "remote monitoring" goal from the lab
meeting. Later it can also listen on TCP 8765 for the Node's live
wifi_stream JSON; that is deliberately left out of this first version.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import html as html_lib

try:
    from user_store import hub_root_for, load_current_user, save_current_user
except ModuleNotFoundError:  # Allow ``import hub.hub_health`` in host tests.
    from .user_store import hub_root_for, load_current_user, save_current_user

STATE: dict = {}

EVENT_FAILURES = {
    "WRITE_FAILED",
    "RECORDING_WRITE_FAILED",
    "RECORDING_FAILED",
    "STORAGE_FAILED",
}

# The actual RTC worker is kept separate from the status server.  Publishing
# the policy here lets the client page and /health describe the deployment
# that the worker is expected to follow without coupling the HTTP process to
# serial-device handling.
RTC_SYNC_POLICY = {
    "frequency": "daily at 12:00 + every time the Node's USB-serial is plugged in",
    "triggered_by": "systemd timer hub-rtc-sync.timer + udev rule 99-respact-rtc.rules",
    "supported_firmware": "legacy RTCSET + DS3231 status protocol",
    "current_board_bringup_test": "unsupported until Node exposes RTCSET and RTC DS3231 status",
    "retries_on_failure": 3,
    "pass_tolerance_s": 3.0,
    "logged_to": "hub/state/rtc_sync.jsonl (every run), hub/state/rtc_sync_last.json (latest)",
}


def newest_session(archive: Path) -> dict:
    best: tuple[float, Path] | None = None
    for quality in archive.glob("*/*.quality.json"):
        try:
            payload = json.loads(quality.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("verdict") in ("EVENTS", "SD_TEST"):
            continue
        mtime = quality.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, quality)
    if best is None:
        return {}
    try:
        payload = json.loads(best[1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    payload["quality_file"] = str(best[1])
    return payload


def archived_session_count(archive: Path) -> int:
    count = 0
    for path in archive.glob("*/*.CSV"):
        upper = path.name.upper()
        if upper == "EVENTS.CSV" or (upper.startswith("T") and upper.endswith(".CSV")):
            continue
        count += 1
    return count


def events_summary(archive: Path) -> dict:
    """Summarize append-only EVENTS.CSV files from the Node firmware."""
    boot_count = 0
    started: set[str] = set()
    stopped: set[str] = set()
    failures: list[dict] = []
    files = 0
    parse_errors = 0
    for event_file in sorted(archive.glob("*/EVENTS.CSV")):
        files += 1
        try:
            with event_file.open(newline="", encoding="utf-8", errors="replace") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != ["unix_ms", "event", "session_file", "rows", "detail"]:
                    parse_errors += 1
                    continue
                for row in reader:
                    event = (row.get("event") or "").strip()
                    session_file = Path(row.get("session_file") or "").name
                    if event == "BOOT":
                        boot_count += 1
                    elif event == "RECORDING_STARTED" and session_file:
                        started.add(session_file)
                    elif event == "RECORDING_STOPPED" and session_file:
                        stopped.add(session_file)
                    if event in EVENT_FAILURES:
                        failures.append({
                            "file": event_file.name,
                            "event": event,
                            "session_file": session_file,
                            "detail": row.get("detail") or "",
                        })
        except (OSError, csv.Error):
            parse_errors += 1

    unmatched_started = sorted(started - stopped)
    unmatched_stopped = sorted(stopped - started)
    issues = []
    if unmatched_started:
        issues.append(f"recording not stopped: {', '.join(unmatched_started)}")
    if unmatched_stopped:
        issues.append(f"recording stopped without start: {', '.join(unmatched_stopped)}")
    if failures:
        issues.append(f"{len(failures)} storage failure event(s)")
    if parse_errors:
        issues.append(f"{parse_errors} EVENTS.CSV file(s) unreadable or incompatible")
    return {
        "files": files,
        "boot_count": boot_count,
        "recording_started": len(started),
        "recording_stopped": len(stopped),
        "unmatched_started": unmatched_started,
        "unmatched_stopped": unmatched_stopped,
        "failures": failures,
        "parse_errors": parse_errors,
        "healthy": not issues,
        "issues": issues,
    }


def snapshot(archive: Path) -> dict:
    usage = shutil.disk_usage(archive)
    current = load_current_user(hub_root_for(archive))
    return {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive": str(archive),
        "current_user": current,
        "rtc_sync_policy": RTC_SYNC_POLICY,
        "events": events_summary(archive),
        "sessions_archived": archived_session_count(archive),
        "newest": newest_session(archive),
        "disk_free_gb": round(usage.free / 1e9, 2),
        "disk_total_gb": round(usage.total / 1e9, 2),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        # Switch the active user; future ingests bind to the new id.
        if self.path != "/user":
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4096:
            self._send(b"empty form", "text/plain; charset=utf-8", 400)
            return
        fields = parse_qs(
            self.rfile.read(length).decode("utf-8", "replace"))
        raw = (fields.get("user_id") or [""])[0]
        try:
            save_current_user(hub_root_for(STATE["archive"]), raw)
        except ValueError as error:
            self._send(str(error).encode("utf-8"),
                       "text/plain; charset=utf-8", 400)
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        data = snapshot(STATE["archive"])
        if self.path == "/health":
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return
        if self.path != "/":
            self._send(b"not found", "text/plain; charset=utf-8", 404)
            return
        current = data.get("current_user") or {}
        user_id = html_lib.escape(str(current.get("user_id") or "UNKNOWN"))
        since = html_lib.escape(str(current.get("since") or "-"))
        newest = data.get("newest") or {}
        rows = "".join(
            f"<tr><td>{html_lib.escape(str(key))}</td>"
            f"<td>{html_lib.escape(str(value))}</td></tr>"
            for key, value in data.items()
            if key not in ("newest", "current_user", "rtc_sync_policy", "events")
        )
        newest_rows = "".join(
            f"<tr><td>{html_lib.escape(str(key))}</td>"
            f"<td>{html_lib.escape(str(value))}</td></tr>"
            for key, value in newest.items()
        )
        rtc_policy = data.get("rtc_sync_policy") or {}
        rtc_rows = "".join(
            f"<tr><td>{html_lib.escape(str(key))}</td>"
            f"<td>{html_lib.escape(str(value))}</td></tr>"
            for key, value in rtc_policy.items()
        )
        events = data.get("events") or {}
        event_issues = events.get("issues") or []
        event_class = " class='alarm'" if event_issues else ""
        event_rows = "".join(
            f"<tr><td>{html_lib.escape(str(key))}</td>"
            f"<td>{html_lib.escape(str(value))}</td></tr>"
            for key, value in events.items()
            if key not in ("failures", "issues")
        )
        event_alert = (
            "<p class='alarm'><strong>EVENTS alarm:</strong> "
            + html_lib.escape("; ".join(event_issues)) + "</p>"
            if event_issues else "<p>EVENTS 状态正常。</p>"
        )
        body = (
            "<html><head><meta charset='utf-8'><title>Sleep Belt Hub</title>"
            "<style>body{font-family:sans-serif;margin:2rem}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;"
            "padding:4px 10px}.alarm{color:#a00;background:#fee;padding:8px}"
            "form{margin:1rem 0}</style></head><body>"
            "<h1>Sleep Belt Hub</h1>"
            f"<h2>当前用户：{user_id}（自 {since}）</h2>"
            "<form method='POST' action='/user'>"
            "<label>切换用户 <input name='user_id' maxlength='32' "
            "placeholder='例如 S01'></label> "
            "<button type='submit'>切换</button></form>"
            "<p>之后入库的文件都归当前用户；已入库的不受影响。</p>"
            "<h2>Hub</h2><table>" + rows + "</table>"
            "<h2>RTC 同步策略</h2><table>" + rtc_rows + "</table>"
            f"<h2{event_class}>Node EVENTS 状态</h2><table>{event_rows}</table>"
            + event_alert
            + "<h2>Newest session</h2><table>" + newest_rows + "</table>"
            "</body></html>"
        ).encode("utf-8")
        self._send(body, "text/html; charset=utf-8")

    def log_message(self, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Hub status page")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    STATE["archive"] = args.archive
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"hub-health: http://0.0.0.0:{args.port}/ (archive={args.archive})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
