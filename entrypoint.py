#!/usr/bin/env python3
import configparser
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

DEFAULT_CONFIG = '''# config.ini
# Generated automatically on first run. Edit and set your Lidarr API key.

[lidarr]
# Default Lidarr URL
base_url = http://192.168.1.103:8686
api_key  = REPLACE_WITH_YOUR_LIDARR_API_KEY

[probe]
# API to probe for each MBID
target_base_url = https://api.lidarr.audio/api/v0.4
timeout_seconds = 10

# Concurrent request settings
max_concurrent_requests = 5
rate_limit_per_second = 3

# Circuit breaker settings
circuit_breaker_threshold = 25
backoff_factor = 2.0
max_backoff_seconds = 60

[ledger]
# For Docker single-volume usage, keep this as /data/mbids.csv
csv_path = /data/mbids.csv

[run]
# Re-check successes if true (or pass --force via env FORCE_RUN=true)
force = false
batch_size = 25
batch_write_frequency = 5

[actions]
# If true, when a probe transitions from (no status or timeout) -> success,
# trigger a non-blocking refresh of that artist in Lidarr.
update_lidarr = false

[schedule]
# Run every N seconds (>=1). Example: 3600 = hourly
interval_seconds = 3600
run_at_start = true

[monitoring]
log_progress_every_n = 25
log_level = INFO
'''

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

    # If config is missing, create and exit so the user can fill in the API key.
    if not os.path.exists(config_path):
        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
        print(f"[{datetime.now().isoformat()}] Created default config at {config_path}. Please edit api_key and restart.", flush=True)
        sys.exit(1)

    # Load schedule settings
    cp = configparser.ConfigParser()
    if not cp.read(config_path, encoding="utf-8"):
        print(f"ERROR: Could not read config: {config_path}", file=sys.stderr)
        sys.exit(2)

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
            first_loop = False
            delay = interval_seconds
            print(f"[{datetime.now().isoformat()}] Waiting {delay}s before first run...", flush=True)
            time.sleep(delay)
            if STOP:
                break

        first_loop = False

        # Optional jitter
        delay_before = 0
        if jitter_seconds > 0:
            try:
                delay_before = int.from_bytes(os.urandom(2), "big") % (jitter_seconds + 1)
            except Exception:
                delay_before = 0

        if delay_before > 0:
            print(f"[{datetime.now().isoformat()}] Sleeping jitter {delay_before}s before run...", flush=True)
            time.sleep(delay_before)
            if STOP:
                break

        # Run the main script once
        print(f"[{datetime.now().isoformat()}] Starting lidarr MBID check...", flush=True)
        extra = []
        # Optional: FORCE_RUN=true to pass --force through the scheduler
        if os.environ.get("FORCE_RUN", "false").lower() in ("1", "true", "yes", "on"):
            extra.append("--force")

        proc = subprocess.run(
            ["python", "/app/lidarr_mbid_check.py", "--config", config_path] + extra,
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
