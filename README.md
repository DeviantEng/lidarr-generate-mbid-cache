# lidarr-generate-mbid-cache

Collect all artist MBIDs from your **Lidarr** instance and probe them against an API (default: `https://api.lidarr.audio/api/v0.4`).  
Results are stored in a CSV ledger (`mbids.csv`) with status, attempts, and timestamps.  
Run once locally or continuously on a schedule (inside Docker).

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
force = false

[schedule]
interval_seconds = 3600
run_at_start = true
```

Run it once:

```bash
python lidarr_mbid_check.py --config config.ini
```

Or run continuously on the schedule:

```bash
python entrypoint.py
```

---

## 2) Run with Docker

### Docker Run (single volume)

Mount one folder containing `config.ini` (and where `mbids.csv` will be created):

```bash
docker run -d \
  --name lidarr-generate-mbid-cache \
  -v $(pwd)/data:/data \
  ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
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
```

Then:

```bash
docker compose up -d
docker compose logs -f lidarr-generate-mbid-cache
```

---

## Config Options

- **[lidarr]**
  - `base_url`: Your Lidarr instance (default `http://192.168.1.103:8686`)
  - `api_key`: Lidarr API key
- **[probe]** – retry/delay/timeout behavior (default timeout is **5s**)  
- **[ledger]** – path to CSV ledger  
- **[run]** – force re-checks  
- **[schedule]** – interval and behavior when running via Docker/entrypoint  

---

## Notes

- First run will create a default `config.ini` if missing and exit with a message so you can fill in the API key.  
- CSV is updated after each MBID; safe to interrupt and resume.  
- By default the Docker container runs forever, following the schedule in `config.ini`.

