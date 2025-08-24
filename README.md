# lidarr-generate-mbid-cache

Collect all artist MBIDs from your **Lidarr** instance and probe them against an API (default: `https://api.lidarr.audio/api/v0.4`).  
Results are stored in a CSV ledger (`mbids.csv`) with status, attempts, and timestamps.  
Run once locally or continuously on a schedule (inside Docker).

---

## Key Features

- **Auto-config bootstrap**: On first run, creates `/data/config.ini` with sensible defaults.
- **CSV ledger**: Keeps `mbids.csv` up to date; safe to stop/restart mid-run.
- **Force quick refresh**: `--force` (or `[run] force = true`) makes a fast pass with `max_attempts = 1` to quickly re-evaluate cache status.
- **Results log per run**: After every run, writes `/data/results_YYYYMMDDTHHMMSSZ.log` with success/timeout counts, total, force flag, and timestamp.
- **Optional Lidarr refresh**: When `[actions] update_lidarr = true`, any MBID that transitions from `""` (no status) or `timeout` → `success` triggers a **non-blocking** refresh request to your local Lidarr (fire-and-forget).

---

## 1) Run Locally

Clone and install:

```bash
git clone https://github.com/devianteng/lidarr-generate-mbid-cache.git
cd lidarr-generate-mbid-cache
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `config.ini` (auto-created on first run if missing):

```ini
[lidarr]
base_url = http://192.168.1.103:8686
api_key  = REPLACE_WITH_YOUR_LIDARR_API_KEY

[probe]
target_base_url = https://api.lidarr.audio/api/v0.4
max_attempts    = 10
delay_seconds   = 1
timeout_seconds = 5

[ledger]
csv_path = ./mbids.csv

[run]
# When true or when passing --force, the run re-checks all MBIDs
# and hard-sets max_attempts=1 for a fast refresh pass.
force = false

[actions]
# When true, if a probe transitions from no status/timeout -> success,
# trigger a non-blocking refresh of that artist in Lidarr.
update_lidarr = false

[schedule]
interval_seconds = 3600
run_at_start = true
```

Run it once:

```bash
python lidarr_mbid_check.py --config config.ini
```

Force quick refresh pass (max_attempts=1):

```bash
python lidarr_mbid_check.py --config config.ini --force
```

Or run continuously on the schedule:

```bash
python entrypoint.py
```

---

## 2) Run with Docker

### Docker Run (single volume)

Mount one folder containing `config.ini` (and where `mbids.csv` and results logs will be created):

```bash
docker run -d   --name lidarr-generate-mbid-cache   -v $(pwd)/data:/data   ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
```

(Optional) Force quick refresh run via env from the scheduler:

```bash
docker run -d   --name lidarr-generate-mbid-cache   -e FORCE_RUN=true   -v $(pwd)/data:/data   ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
```

### Docker Compose

Example `docker-compose.yml`:

```yaml
version: '3.8'

services:
  lidarr-generate-mbid-cache:
    image: ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
    container_name: lidarr-generate-mbid-cache
    restart: unless-stopped
    volumes:
      - ./data:/data
    # environment:
    #   FORCE_RUN: "true"   # optional: pass --force to the scheduled runs
```

Then:

```bash
docker compose up -d
docker compose logs -f lidarr-generate-mbid-cache
```

---

## Outputs

- **CSV Ledger**: `/data/mbids.csv` (or as configured)
- **Results Logs**: `/data/results_YYYYMMDDTHHMMSSZ.log` per run, containing:
  ```
  finished_at_utc=2025-08-24T15:30:12.345678+00:00
  success=<count>
  timeout=<count>
  total=<count>
  force_mode=true|false
  refreshes_triggered=<count>
  ```

---

## Scheduling Notes

- The scheduler runs the script, then sleeps for `[schedule] interval_seconds`.  
  For example, with `interval_seconds = 3600`, you get: _run duration_ + **1 hour** idle before the next run.  
- To align to wall-clock times (cron-style), we can switch to a cron expression system on request.

---

## Tips

- First run inside Docker will create `/data/config.ini` and exit—fill in your API key and re-run.
- If you change probe parameters (attempts, delay), re-running will pick up the changes from `config.ini`.
- `update_lidarr = true` fires a **non-blocking** refresh only when a row transitions from no status/timeout → success.

