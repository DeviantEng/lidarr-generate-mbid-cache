# DEPRECATED -- USE THIS INSTEAD:  
https://github.com/DeviantEng/lidarr-cache-warmer
---
.
.
.
# lidarr-generate-mbid-cache

Cache warming tool for **Lidarr** artist MusicBrainz IDs (MBIDs). Fetches all artists from your Lidarr instance and repeatedly probes each MBID against an API endpoint (default: `api.lidarr.audio`) until successful, triggering cache generation in the backend.

**Perfect for new APIs with limited cache coverage** - keeps trying each artist until the cache warms up and returns data.

## What It Does

1. **Fetches all artists** from your Lidarr instance
2. **Repeatedly queries** each MBID against the target API (up to 25 attempts by default)
3. **Tracks status** in a CSV ledger (`mbids.csv`) - safe to stop/restart anytime
4. **Concurrent processing** - checks 5 artists simultaneously at 3 requests/second
5. **Optional Lidarr refresh** - triggers metadata refresh when cache warming succeeds

## Requirements

- **Lidarr instance** with API access
- **Target API** to warm (default: `https://api.lidarr.audio/api/v0.4`)
- **Docker** (recommended) or **Python 3.8+**

---

## 🐳 Docker (Recommended)

### Quick Start

```bash
# Create data directory
mkdir -p ./data

# Run container (will create config and exit)
docker run -d --name lidarr-cache -v $(pwd)/data:/data ghcr.io/devianteng/lidarr-generate-mbid-cache:latest

# Edit config with your Lidarr API key
nano ./data/config.ini

# Restart container
docker restart lidarr-cache

# Monitor logs
docker logs -f lidarr-cache
```

### Docker Compose

```yaml
version: '3.8'

services:
  lidarr-generate-mbid-cache:
    image: ghcr.io/devianteng/lidarr-generate-mbid-cache:latest
    container_name: lidarr-cache
    restart: unless-stopped
    volumes:
      - ./data:/data
    # Optional environment variables:
    # environment:
    #   FORCE_RUN: "true"    # Pass --force to scheduled runs
```

Then:
```bash
docker compose up -d
docker compose logs -f lidarr-generate-mbid-cache
```

---

## 🐍 Manual Python Installation

```bash
# Clone and setup
git clone https://github.com/devianteng/lidarr-generate-mbid-cache.git
cd lidarr-generate-mbid-cache
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Run once (creates config.ini)
python lidarr_mbid_check.py --config config.ini

# Edit config.ini with your Lidarr API key, then:
python lidarr_mbid_check.py --config config.ini

# Or run on schedule:
python entrypoint.py
```

---

## ⚙️ Configuration

On first run, creates `config.ini` with these key settings:

```ini
[lidarr]
base_url = http://192.168.1.103:8686
api_key  = REPLACE_WITH_YOUR_LIDARR_API_KEY

[probe]
target_base_url = https://api.lidarr.audio/api/v0.4
max_attempts_per_artist = 25    # Try each artist up to 25 times
delay_between_attempts = 0.5    # Wait between attempts
max_concurrent_requests = 5     # Simultaneous requests
rate_limit_per_second = 3       # Max API calls per second

[schedule]
interval_seconds = 3600         # Run every hour
max_runs = 50                   # Stop after 50 scheduled runs
```

### Key Settings

- **`max_attempts_per_artist`**: How many times to retry each artist (default: 25)
- **`rate_limit_per_second`**: API rate limit protection (default: 3 req/sec)
- **`max_concurrent_requests`**: Simultaneous artists being processed (default: 5)
- **`update_lidarr`**: Set to `true` to refresh Lidarr when cache warming succeeds

---

## 📊 Output & Monitoring

### Console Output
```
Discovered 1247 artists (23 new).
Will check 156 MBIDs (pending-only).

[1/156] Checking Artist Name [mbid-here] ... SUCCESS (code=200, attempts=8)
[2/156] Checking Another Artist [mbid-here] ... TIMEOUT (code=503, attempts=25)
[3/156] Checking Third Artist [mbid-here] ... SUCCESS (code=200, attempts=1)

Progress: 25/156 (16.0%) - Rate: 2.8/sec - ETA: 3.1min - API: 2.95 req/sec

Summary:
  Total in ledger: 1247
  Success: 1198
  Timeout: 49
  Refreshes triggered (new successes): 12
```

### Generated Files
- **`/data/mbids.csv`** - Main ledger with all MBID statuses
- **`/data/results_YYYYMMDDTHHMMSSZ.log`** - Simple metrics per run

---

## 🔧 CLI Options

```bash
# Force re-check all MBIDs (including successful ones)
python lidarr_mbid_check.py --config config.ini --force

# Preview what would be checked without API calls
python lidarr_mbid_check.py --config config.ini --dry-run
```

---

## 💡 Tips

- **Start conservative** with default settings (3 req/sec, 25 attempts)
- **Monitor the logs** for rate limiting warnings
- **Large libraries**: Processing happens in batches with frequent progress saves
- **Interruption-safe**: Can stop/restart anytime - progress is saved to CSV
- **Cache warming**: Once an artist succeeds, it's cached and won't need re-processing

---

## 🔗 Integration

Set `update_lidarr = true` in config to automatically trigger Lidarr artist refreshes when cache warming succeeds. This helps keep your Lidarr metadata up-to-date as the backend cache grows.
