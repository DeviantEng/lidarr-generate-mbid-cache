# Lidarr MBID Probe

A tool to collect all artist MBIDs from your **Lidarr** instance and probe them against an API (default: `https://api.lidarr.audio/api/v0.4`).  
Results are stored in a CSV ledger (`mbids.csv`) with status, attempts, and timestamps.  
The script can run once manually or on a recurring schedule (inside Docker).

---

## 1. Run Locally

Clone and install:

```bash
git clone https://github.com/devianteng/lidarr-mbid-probe.git
cd lidarr-mbid-probe
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `config.ini`:

```ini
[lidarr]
base_url = http://172.16.100.203:15111
api_key  = REPLACE_WITH_YOUR_LIDARR_API_KEY

[probe]
target_base_url = https://api.lidarr.audio/api/v0.4
max_attempts    = 10
delay_seconds   = 1
timeout_seconds = 20

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

## 2. Run with Docker

### Docker Run
Mount a single folder containing `config.ini` (and where `mbids.csv` will be created):

```bash
docker run -d \
  --name lidarr-mbid-probe \
  -v $(pwd)/data:/data \
  ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
```

### Docker Compose
Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  lidarr-mbid-probe:
    image: ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
    container_name: lidarr-mbid-probe
    restart: unless-stopped
    volumes:
      - ./data:/data
```

Then:

```bash
docker compose up -d
docker compose logs -f lidarr-mbid-probe
```

---

## Config Options

- **[lidarr]**
  - `base_url`: Your Lidarr instance (e.g., `http://172.16.100.203:15111`)
  - `api_key`: Lidarr API key
- **[probe]** – retry/delay/timeout behavior  
- **[ledger]** – path to CSV ledger  
- **[run]** – force re-checks  
- **[schedule]** – interval and behavior when running via Docker/entrypoint  

---

## Notes
- Interrupt/resume safe: the CSV is updated after each MBID.
- By default the Docker container runs forever, following the schedule in `config.ini`.

