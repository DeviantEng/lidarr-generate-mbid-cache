#!/usr/bin/env python3
import configparser
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

STOP = False

def _sig_handler(signum, frame):
    global STOP
    STOP = True
    print(f"[{datetime.now().isoformat()}] Received signal {signum}. Shutting down after current run...", flush=True)

def parse_bool(s: str, default: bool = False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in ("1", "true", "yes", "on")

def main():
    # Allow overriding the config path via env var, default to /data/config.ini
    config_path = os.environ.get("CONFIG_PATH", "/data/config.ini")
    if not os.path.exists(config_path):
        print(f"ERROR: Config not found: {config_path}", file=sys.stderr)
        sys.exit(2)

    # Load schedule settings
    cp = configparser.ConfigParser()
    if not cp.read(config_path, encoding="utf-8"):
        print(f"ERROR: Could not read config: {config_path}", file=sys.stderr)
        sys.exit(2)

    # Defaults if [schedule] is missing
    interval_seconds = cp.getint("schedule", "interval_seconds", fallback=3600)
    run_at_start     = parse_bool(cp.get("schedule", "run_at_start", fallback="true"))
    jitter_seconds   = cp.getint("schedule", "jitter_seconds", fallback=0)  # optional, default 0
    max_runs         = cp.getint("schedule", "max_runs", fallback=0)        # 0 = unlimited

    if interval_seconds < 1:
        print("ERROR: [schedule].interval_seconds must be >= 1", file=sys.stderr)
        sys.exit(2)

    # Signal handling for graceful exit
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    run_count = 0
    first_loop = True

    while not STOP:
        if first_loop and not run_at_start:
            # Wait first, then run
            first_loop = False
            delay = interval_seconds
            print(f"[{datetime.now().isoformat()}] Waiting {delay}s before first run...", flush=True)
            time.sleep(delay)
            if STOP: break

        first_loop = False

        # Optionally apply small jitter to avoid synchronized calls across hosts
        delay_before = 0
        if jitter_seconds > 0:
            try:
                # Basic, dependency-free jitter
                delay_before = int.from_bytes(os.urandom(2), "big") % (jitter_seconds + 1)
            except Exception:
                delay_before = 0

        if delay_before > 0:
            print(f"[{datetime.now().isoformat()}] Sleeping jitter {delay_before}s before run...", flush=True)
            time.sleep(delay_before)
            if STOP: break

        # Run the main script once
        print(f"[{datetime.now().isoformat()}] Starting lidarr MBID check...", flush=True)
        proc = subprocess.run(
            ["python", "/app/lidarr_mbid_check.py", "--config", config_path],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        print(f"[{datetime.now().isoformat()}] Run complete (exit={proc.returncode}).", flush=True)

        run_count += 1
        if max_runs > 0 and run_count >= max_runs:
            print(f"[{datetime.now().isoformat()}] Reached max_runs={max_runs}. Exiting.", flush=True)
            break
        if STOP:
            break

        # Sleep until next run
        print(f"[{datetime.now().isoformat()}] Sleeping {interval_seconds}s until next run...", flush=True)
        for _ in range(interval_seconds):
            if STOP:
                break
            time.sleep(1)

    print(f"[{datetime.now().isoformat()}] Exited entrypoint loop.", flush=True)

if __name__ == "__main__":
    main()

