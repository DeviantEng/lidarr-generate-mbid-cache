#!/usr/bin/env python3
import argparse
import configparser
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import requests

DEFAULT_CONFIG = '''# config.ini
# Generated automatically on first run. Edit and set your Lidarr API key.

[lidarr]
# Default Lidarr URL
base_url = http://192.168.1.103:8686
api_key  = REPLACE_WITH_YOUR_LIDARR_API_KEY

[probe]
# API to probe for each MBID
target_base_url = https://api.lidarr.audio/api/v0.4
max_attempts    = 10
delay_seconds   = 1
timeout_seconds = 5

[ledger]
# For Docker single-volume usage, keep this as /data/mbids.csv
csv_path = /data/mbids.csv

[run]
# Re-check successes if true or use --force CLI flag
force = false

[actions]
# If true, when a probe transitions from (no status or timeout) -> success,
# trigger a non-blocking refresh of that artist in Lidarr.
update_lidarr = false

[schedule]
# Used by entrypoint.py (scheduler) if you run that directly
interval_seconds = 3600
run_at_start = true
'''


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_lidarr_artists(base_url: str, api_key: str, timeout: int = 30) -> List[Dict]:
    """
    Fetch artists from Lidarr and return a list of dicts with {id, name, mbid}.
    Tries common Lidarr API paths and fields.
    """
    session = requests.Session()
    headers = {"X-Api-Key": api_key}

    candidates = [
        "/api/v1/artist",
        "/api/artist",
        "/api/v3/artist",
    ]

    last_exc = None
    for path in candidates:
        url = f"{base_url.rstrip('/')}{path}"
        try:
            r = session.get(url, headers=headers, timeout=timeout)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            artists = []
            for a in data:
                mbid = a.get("foreignArtistId") or a.get("mbId") or a.get("mbid")
                name = a.get("artistName") or a.get("name") or "Unknown"
                lidarr_id = a.get("id")  # internal Lidarr artist id
                if mbid:
                    artists.append({"id": lidarr_id, "name": name, "mbid": mbid})
            return artists
        except Exception as e:
            last_exc = e
            continue

    raise RuntimeError(
        f"Could not fetch artists from Lidarr using known endpoints. Last error: {last_exc}"
    )


def read_ledger(csv_path: str) -> Dict[str, Dict]:
    """
    Read existing CSV into a dict keyed by MBID.
    """
    ledger: Dict[str, Dict] = {}
    if not os.path.exists(csv_path):
        return ledger
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mbid = (row.get("mbid") or "").strip()
            if not mbid:
                continue
            ledger[mbid] = {
                "mbid": mbid,
                "artist_name": row.get("artist_name", ""),
                "status": (row.get("status") or "").lower().strip(),
                "attempts": int((row.get("attempts") or "0") or 0),
                "last_status_code": row.get("last_status_code", ""),
                "last_checked": row.get("last_checked", ""),
            }
    return ledger


def write_ledger(csv_path: str, ledger: Dict[str, Dict]) -> None:
    """
    Write the ledger dict back to CSV atomically.
    """
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    fieldnames = ["mbid", "artist_name", "status", "attempts", "last_status_code", "last_checked"]
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for _, row in sorted(ledger.items(), key=lambda kv: (kv[1].get("artist_name", ""), kv[0])):
            writer.writerow(row)
    os.replace(tmp_path, csv_path)


def check_mbid(
    mbid: str,
    target_base_url: str,
    max_attempts: int = 10,
    delay_seconds: float = 1.0,
    timeout: int = 5,
) -> Tuple[str, str, int]:
    """
    Try the target endpoint up to max_attempts, return (status, last_status_code, attempts_used)
      status: 'success' or 'timeout'
    """
    url = f"{target_base_url.rstrip('/')}/artist/{mbid}"
    session = requests.Session()

    last_code = ""
    for attempt in range(1, max_attempts + 1):
        try:
            resp = session.get(url, timeout=timeout)
            last_code = str(resp.status_code)
            if resp.status_code == 200:
                return "success", last_code, attempt
        except requests.RequestException as e:
            last_code = f"EXC:{type(e).__name__}"

        if attempt < max_attempts:
            time.sleep(delay_seconds)

    return "timeout", last_code, max_attempts


def parse_bool(s: str, default: bool = False) -> bool:
    if s is None:
        return default
    s = s.strip().lower()
    return s in ("1", "true", "yes", "on")


def load_config(path: str) -> dict:
    """
    Load INI config and return a normalized dict of settings with defaults.
    If config.ini is missing, create it with DEFAULT_CONFIG and exit with code 1.
    """
    # Accept bare name without extension by appending .ini if needed
    if not os.path.exists(path) and os.path.exists(path + ".ini"):
        path = path + ".ini"

    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
        print(f"Created default config at {path}. Please edit api_key before running again.", file=sys.stderr)
        sys.exit(1)

    cp = configparser.ConfigParser()
    if not cp.read(path, encoding="utf-8"):
        raise FileNotFoundError(f"Config file not found or unreadable: {path}")

    # Defaults
    cfg = {
        "lidarr_url": cp.get("lidarr", "base_url", fallback="http://192.168.1.103:8686"),
        "api_key": cp.get("lidarr", "api_key", fallback=""),
        "target_base_url": cp.get("probe", "target_base_url", fallback="https://api.lidarr.audio/api/v0.4"),
        "max_attempts": cp.getint("probe", "max_attempts", fallback=10),
        "delay_seconds": cp.getfloat("probe", "delay_seconds", fallback=1.0),
        "timeout_seconds": cp.getint("probe", "timeout_seconds", fallback=5),
        "csv_path": cp.get("ledger", "csv_path", fallback="mbids.csv"),
        "force": parse_bool(cp.get("run", "force", fallback="false")),
        "update_lidarr": parse_bool(cp.get("actions", "update_lidarr", fallback="false")),
    }

    if not cfg["api_key"] or "REPLACE_WITH_YOUR_LIDARR_API_KEY" in cfg["api_key"]:
        raise ValueError("Missing [lidarr].api_key in config (or still using the placeholder).")

    return cfg


def trigger_lidarr_refresh(base_url: str, api_key: str, artist_id: Optional[int]) -> None:
    """
    Fire-and-forget refresh request to Lidarr for the given artist id.
    Uses the command endpoint; very short timeout so we don't block.
    """
    if artist_id is None:
        return
    session = requests.Session()
    headers = {"X-Api-Key": api_key}
    # Prefer v1 command; fall back to older if needed
    payloads = [
        {"name": "RefreshArtist", "artistIds": [artist_id]},
        {"name": "RefreshArtist", "artistId": artist_id},
    ]
    for path in ("/api/v1/command", "/api/command"):
        url = f"{base_url.rstrip('/')}{path}"
        for body in payloads:
            try:
                # short timeout; ignore response
                session.post(url, headers=headers, json=body, timeout=0.5)
                return
            except Exception:
                continue
    # Swallow all failures silently (intentionally non-blocking).


def main():
    parser = argparse.ArgumentParser(
        description="Query Lidarr for MBIDs, keep a CSV ledger, and probe each MBID against a target endpoint."
    )
    parser.add_argument("--config", required=True, help="Path to INI config (e.g., /data/config.ini)")
    # Optional overrides (CLI takes precedence over config if provided)
    parser.add_argument("--force", action="store_true",
                        help="Re-run checks even for MBIDs already marked success (also sets max_attempts=1 for a quick refresh)")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"ERROR loading config: {e}", file=sys.stderr)
        sys.exit(2)

    if args.force:
        cfg["force"] = True
        # For 'quick refresh', override attempts to 1 regardless of config
        cfg["max_attempts"] = 1
        print("[INFO] Force mode enabled: max_attempts hard-set to 1 for quick refresh.")

    # 1) Load ledger
    ledger = read_ledger(cfg["csv_path"])

    # 2) Fetch current artists/MBIDs (and Lidarr IDs) from Lidarr
    try:
        artists = get_lidarr_artists(cfg["lidarr_url"], cfg["api_key"])
    except Exception as e:
        print(f"ERROR fetching Lidarr artists: {e}", file=sys.stderr)
        sys.exit(2)

    # Build helper mappings
    mbid_to_lidarr_id: Dict[str, Optional[int]] = {}
    mbid_to_name: Dict[str, str] = {}
    for a in artists:
        mbid_to_lidarr_id[a["mbid"]] = a.get("id")
        mbid_to_name[a["mbid"]] = a.get("name", "")

    # 3) Merge in any new MBIDs (with empty status)
    new_count = 0
    for a in artists:
        mbid = a["mbid"]
        name = a["name"]
        if mbid not in ledger:
            ledger[mbid] = {
                "mbid": mbid,
                "artist_name": name,
                "status": "",
                "attempts": 0,
                "last_status_code": "",
                "last_checked": "",
            }
            new_count += 1
        else:
            if name and ledger[mbid].get("artist_name") != name:
                ledger[mbid]["artist_name"] = name

    # 4) Determine which MBIDs to (re)check
    to_check = []
    for mbid, row in ledger.items():
        status = (row.get("status") or "").lower()
        if cfg["force"] or status not in ("success",):
            to_check.append(mbid)

    print(f"Discovered {len(artists)} artists ({new_count} new).")
    print(f"Will check {len(to_check)} MBIDs ({'force' if cfg['force'] else 'pending-only'}).")

    # 5) Probe each MBID as needed
    transitioned_to_success_count = 0
    for i, mbid in enumerate(to_check, start=1):
        name = ledger[mbid].get("artist_name") or mbid_to_name.get(mbid, "")
        prev_status = (ledger[mbid].get("status") or "").lower()

        print(f"[{i}/{len(to_check)}] Checking {name or '(unknown)'} [{mbid}] ...", end="", flush=True)
        status, last_code, attempts_used = check_mbid(
            mbid,
            target_base_url=cfg["target_base_url"],
            max_attempts=cfg["max_attempts"],
            delay_seconds=cfg["delay_seconds"],
            timeout=cfg["timeout_seconds"],
        )
        ledger[mbid]["status"] = status
        ledger[mbid]["attempts"] = attempts_used
        ledger[mbid]["last_status_code"] = last_code
        ledger[mbid]["last_checked"] = iso_now()
        print(f" {status.upper()} (code={last_code}, attempts={attempts_used})")

        # NEW: If we transitioned from "" or "timeout" -> "success", trigger Lidarr refresh (fire-and-forget)
        if (
            cfg.get("update_lidarr", False)
            and status == "success"
            and prev_status in ("", "timeout")
        ):
            artist_id = mbid_to_lidarr_id.get(mbid)
            trigger_lidarr_refresh(cfg["lidarr_url"], cfg["api_key"], artist_id)
            transitioned_to_success_count += 1
            print(f"  -> Triggered Lidarr refresh for {name or '(unknown)'} [artist_id={artist_id}]")

        # Persist after each row so you can safely stop/restart
        write_ledger(cfg["csv_path"], ledger)

    # 6) Final write and summary
    write_ledger(cfg["csv_path"], ledger)

    successes = sum(1 for r in ledger.values() if r.get("status") == "success")
    timeouts = sum(1 for r in ledger.values() if r.get("status") == "timeout")

    print("\nSummary:")
    print(f"  Total in ledger: {len(ledger)}")
    print(f"  Success: {successes}")
    print(f"  Timeout: {timeouts}")
    print(f"  Refreshes triggered (new successes): {transitioned_to_success_count}")
    print(f"\nCSV written to: {cfg['csv_path']}")

    # 7) Write a timestamped results log into /data
    try:
        os.makedirs("/data", exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = f"/data/results_{ts}.log"
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"finished_at_utc={iso_now()}\n")
            lf.write(f"success={successes}\n")
            lf.write(f"timeout={timeouts}\n")
            lf.write(f"total={len(ledger)}\n")
            lf.write(f"force_mode={'true' if cfg['force'] else 'false'}\n")
            lf.write(f"refreshes_triggered={transitioned_to_success_count}\n")
        print(f"Results log written to: {log_path}")
    except Exception as e:
        print(f"WARNING: Failed to write results log to /data: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
