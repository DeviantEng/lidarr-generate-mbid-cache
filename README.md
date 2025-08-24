# Lidarr MBID Probe

Script that:
1) Pulls all artist MBIDs from your **Lidarr** instance  
2) Keeps a CSV ledger of MBIDs and probe results  
3) Probes each MBID against a target endpoint (default: `https://api.lidarr.audio/api/v0.4/artist/{MBID}`)  
4) Retries per MBID up to a configurable max with a delay; records `success` or `timeout`

## Features
- Config-driven via INI (no secrets on the CLI)
- Idempotent CSV ledger (`mbids.csv`): `mbid,artist_name,status,attempts,last_status_code,last_checked`
- Adds new MBIDs; re-checks only those that previously failed or have no status
- `--force` flag to re-check everything

---

## Setup (local / venv)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a config file (example):

```ini
# mbid_config.ini
[lidarr]
base_url = http://172.16.100.203:15111
api_key  = REPLACE_WITH_YOUR_LIDARR_API_KEY

[probe]
target_base_url = https://api.lidarr.audio/api/v0.4
max_attempts    = 10
delay_seconds   = 1.0
timeout_seconds = 20

[ledger]
csv_path = ./mbids.csv

[run]
force = false
```

Run it:

```bash
python lidarr_mbid_check.py --config mbid_config.ini
# or
python lidarr_mbid_check.py --config mbid_config.ini --force
```

---

## Docker (single volume)

Keep both your config and CSV in the same host folder (e.g., `./lidarr-data`):

```
./lidarr-data/
  ├─ mbid_config.ini
  └─ mbids.csv   (created automatically on first run)
```

Build:

```bash
docker build -t lidarr-mbid-probe:latest .
```

Run:

```bash
docker run --rm \
  -v "$(pwd)/lidarr-data:/data" \
  lidarr-mbid-probe:latest
```

Force re-check:

```bash
docker run --rm \
  -v "$(pwd)/lidarr-data:/data" \
  lidarr-mbid-probe:latest --force
```

---

## Config options

- **[lidarr]**
  - `base_url`: Your Lidarr base URL (e.g., `http://172.16.100.203:15111`)
  - `api_key`: Lidarr API Key

- **[probe]**
  - `target_base_url`: Base API to probe (default: `https://api.lidarr.audio/api/v0.4`)
  - `max_attempts`: Tries per MBID (default: 10)
  - `delay_seconds`: Sleep between attempts (default: 1.0)
  - `timeout_seconds`: Per-request timeout (default: 20)

- **[ledger]**
  - `csv_path`: Where to store the ledger

- **[run]**
  - `force`: `true|false` to re-check all entries

---

## Notes
- Interrupt/resume safe: the CSV is rewritten after each MBID.
- If the Lidarr endpoint differs (older/newer builds), the script tries multiple artist API paths.
- If you prefer JSON over CSV later, the read/write functions can be swapped easily.

## License
MIT (or your preference)

