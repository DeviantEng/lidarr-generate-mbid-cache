# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY lidarr_mbid_check.py .
COPY entrypoint.py .

# Work directory for mounted data (config.ini + mbids.csv)
WORKDIR /data

# Optional: allow overriding the config path at runtime
ENV CONFIG_PATH=/data/config.ini

# Default entrypoint runs the scheduler
ENTRYPOINT ["python", "/app/entrypoint.py"]
