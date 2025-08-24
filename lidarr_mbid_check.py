#!/usr/bin/env python3
import argparse
import configparser
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_lidarr_artists(base_url: str, api_key: str, timeout: int = 30) -> List[Dict]:
    """
    Fetch artists from Lidarr and return a list of dicts with {name, mbid}.
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
                if mbid:
                    artists.append({"name": name, "mbid": mbid})
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
    timeout: int = 20,
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
    """
    # Accept bare name without extension by appending .ini if needed
    if not os.path.exists(path) and os.path.exists(path + ".ini"):
        path = path + ".ini"

    cp = configparser.ConfigParser()
    if not cp.read(path, encoding="utf-8"):
        raise FileNotFoundError(f"Config file not found or unreadable: {path}")

    # Defaults
    cfg = {
        "lidarr_url": cp.get("lidarr", "base_url", fallback="http://127.0.0.1:8686"),
        "api_key": cp.get("lidarr", "api_key", fallback=""),
        "target_base_url": cp.get("probe", "target_base_url", fallback="https://api.lidarr.audio/api/v0.4"),
        "max_attempts": cp.getint("probe", "max_attempts", fallback=10),
        "delay_seconds": cp.getfloat("probe", "delay_seconds", fallback=1.0),
        "timeout_seconds": cp.getint("probe", "timeout_seconds", fallback=20),
        "csv_path": cp.get("ledger", "csv_path", fallback="mbids.csv"),
        "force": parse_bool(cp.get("run", "force", fallback="false")),
    }

    if not cfg["api_key"]:
        raise ValueError("Missing [lidarr].api_key in config.")
    return cfg


def main():
    parser = argparse.ArgumentParser(
        description="Query Lidarr for MBIDs, keep a CSV ledger, and probe each MBID against a target endpoint."
    )
    parser.add_argument("--config", required=True, help="Path to INI config (e.g., mbid_config or mbid_config.ini)")
    # Optional overrides (CLI takes precedence over config if provided)
    parser.add_argument("--force", action="store_true", help="Re-run checks even for MBIDs already marked success")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"ERROR loading config: {e}", file=sys.stderr)
        sys.exit(2)

    if args.force:
        cfg["force"] = True

    # 1) Load ledger
    ledger = read_ledger(cfg["csv_path"])

    # 2) Fetch current artists/MBIDs from Lidarr
    try:
        artists = get_lidarr_artists(cfg["lidarr_url"], cfg["api_key"])
    except Exception as e:
        print(f"ERROR fetching Lidarr artists: {e}", file=sys.stderr)
        sys.exit(2)

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
    for i, mbid in enumerate(to_check, start=1):
        name = ledger[mbid].get("artist_name", "")
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
    print(f"\nCSV written to: {cfg['csv_path']}")


if __name__ == "__main__":
    main()

