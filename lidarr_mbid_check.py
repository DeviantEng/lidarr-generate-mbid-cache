#!/usr/bin/env python3
import argparse
import asyncio
import configparser
import csv
import os
import random
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
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
timeout_seconds = 10

# Per-artist retry settings (for cache warming)
max_attempts_per_artist = 25
delay_between_attempts = 0.5

# Concurrent request settings
max_concurrent_requests = 5
rate_limit_per_second = 3

# Circuit breaker settings (stops entire run if API is completely down)
circuit_breaker_threshold = 25
backoff_factor = 2.0
max_backoff_seconds = 60

[ledger]
# For Docker single-volume usage, keep this as /data/mbids.csv
csv_path = /data/mbids.csv

[run]
# Re-check successes if true or use --force CLI flag
force = false
batch_size = 25
batch_write_frequency = 5

[actions]
# If true, when a probe transitions from (no status or timeout) -> success,
# trigger a non-blocking refresh of that artist in Lidarr.
update_lidarr = false

[schedule]
# Used by entrypoint.py (scheduler) if you run that directly
interval_seconds = 3600
run_at_start = true

[monitoring]
log_progress_every_n = 25
log_level = INFO
'''


class SafeRateLimiter:
    """Production-safe rate limiter with circuit breaker and backoff"""
    
    def __init__(
        self,
        requests_per_second: float = 3.0,
        max_concurrent: int = 5,
        circuit_breaker_threshold: int = 10,
        backoff_factor: float = 2.0,
        max_backoff_seconds: float = 60.0
    ):
        self.base_rate = requests_per_second
        self.current_rate = requests_per_second
        self.max_concurrent = max_concurrent
        
        # Rate limiting
        self.request_times = deque()
        self.semaphore = asyncio.Semaphore(max_concurrent)
        
        # Circuit breaker
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.consecutive_failures = 0
        self.last_failure_time = 0
        self.backoff_factor = backoff_factor
        self.max_backoff_seconds = max_backoff_seconds
        
        # Statistics
        self.total_requests = 0
        self.total_successes = 0
        self.total_rate_limits = 0
        self.total_errors = 0
        self.circuit_breaker_trips = 0
    
    async def acquire(self) -> bool:
        """Acquire permission to make a request. Returns False if circuit breaker is open."""
        if self._is_circuit_breaker_open():
            return False
        
        await self.semaphore.acquire()
        
        try:
            await self._rate_limit()
            self.total_requests += 1
            return True
        except Exception:
            self.semaphore.release()
            raise
    
    def release(self, status_code: int, response_time_seconds: float):
        """Release the semaphore and record the result"""
        self.semaphore.release()
        
        if status_code == 200:
            self.total_successes += 1
            self.consecutive_failures = 0
            # Gradually restore rate after success
            if self.current_rate < self.base_rate:
                self.current_rate = min(self.current_rate * 1.05, self.base_rate)
                
        elif status_code == 429:  # Rate limited - this is bad, reduce rate
            self.total_rate_limits += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.time()
            self.current_rate *= 0.5
            print(f"⚠️  Rate limited! Reducing rate to {self.current_rate:.2f} req/sec")
            
        elif status_code in (0, "TIMEOUT") or str(status_code).startswith("EXC:"):  # Connection issues
            self.total_errors += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.time()
            self.current_rate *= 0.8
            print(f"⚠️  Connection error {status_code}! Reducing rate to {self.current_rate:.2f} req/sec")
            
        # For cache warming: 503, 404, and other HTTP errors are EXPECTED
        # Don't reduce rate for these - they're part of normal cache warming process
        else:
            self.consecutive_failures = 0  # Reset failures for expected responses
    
    async def _rate_limit(self):
        """Implement token bucket rate limiting"""
        now = time.time()
        
        # Remove old request timestamps
        while self.request_times and now - self.request_times[0] > 1.0:
            self.request_times.popleft()
        
        # Check if we're at the rate limit
        if len(self.request_times) >= self.current_rate:
            oldest_request = self.request_times[0]
            wait_time = 1.0 - (now - oldest_request)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                now = time.time()
                while self.request_times and now - self.request_times[0] > 1.0:
                    self.request_times.popleft()
        
        self.request_times.append(now)
    
    def _is_circuit_breaker_open(self) -> bool:
        """Check if circuit breaker should prevent requests"""
        if self.consecutive_failures < self.circuit_breaker_threshold:
            return False
        
        time_since_failure = time.time() - self.last_failure_time
        backoff_time = min(
            self.backoff_factor ** (self.consecutive_failures - self.circuit_breaker_threshold),
            self.max_backoff_seconds
        )
        
        if time_since_failure < backoff_time:
            self.circuit_breaker_trips += 1
            return True
        
        # Try to reset circuit breaker
        self.consecutive_failures = max(0, self.consecutive_failures - 1)
        return False
    
    def get_stats(self) -> dict:
        """Get current statistics"""
        success_rate = (self.total_successes / self.total_requests) if self.total_requests > 0 else 0
        
        return {
            "total_requests": self.total_requests,
            "success_rate": f"{success_rate:.1%}",
            "rate_limits_hit": self.total_rate_limits,
            "server_errors": self.total_errors,
            "current_rate": f"{self.current_rate:.2f} req/sec",
            "circuit_breaker_failures": self.consecutive_failures,
            "circuit_breaker_trips": self.circuit_breaker_trips,
            "circuit_breaker_open": self._is_circuit_breaker_open()
        }


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bool(s: str, default: bool = False) -> bool:
    if s is None:
        return default
    return s.strip().lower() in ("1", "true", "yes", "on")


def validate_config(cfg: dict) -> List[str]:
    """Return list of configuration issues"""
    issues = []
    
    # Check required fields
    if not cfg.get("api_key") or "REPLACE_WITH_YOUR" in cfg["api_key"]:
        issues.append("Missing or placeholder Lidarr API key")
    
    # Validate URLs
    for url_key in ["lidarr_url", "target_base_url"]:
        if not cfg.get(url_key, "").startswith(("http://", "https://")):
            issues.append(f"Invalid URL format for {url_key}")
    
    # Check numeric ranges
    if cfg.get("timeout_seconds", 0) < 1:
        issues.append("timeout_seconds must be >= 1")
    
    if cfg.get("rate_limit_per_second", 0) <= 0:
        issues.append("rate_limit_per_second must be > 0")
    
    if cfg.get("max_concurrent_requests", 0) < 1:
        issues.append("max_concurrent_requests must be >= 1")
        
    return issues


def check_api_health(target_base_url: str, timeout: int = 10) -> dict:
    """Pre-flight check of the target API"""
    health_info = {
        "available": False,
        "response_time_ms": None,
        "status_code": None,
        "error": None
    }
    
    try:
        start_time = time.time()
        response = requests.get(target_base_url, timeout=timeout)
        health_info["response_time_ms"] = (time.time() - start_time) * 1000
        health_info["status_code"] = response.status_code
        health_info["available"] = response.status_code < 500
        
    except Exception as e:
        health_info["error"] = str(e)
    
    return health_info


def estimate_runtime(to_check_count: int, cfg: dict) -> str:
    """Provide runtime estimates for cache warming workload"""
    concurrent = min(cfg.get("max_concurrent_requests", 5), to_check_count)
    rate_limit = cfg.get("rate_limit_per_second", 3)
    effective_rate = min(concurrent, rate_limit)
    
    # For cache warming: assume average 60% of max_attempts needed
    # (some artists cache quickly, others need full attempts)
    avg_attempts = cfg.get("max_attempts_per_artist", 25) * 0.6
    delay_per_attempt = cfg.get("delay_between_attempts", 0.5)
    
    # Time per artist = (attempts * delay) + (attempts * avg_response_time)
    estimated_time_per_artist = (avg_attempts * delay_per_attempt) + (avg_attempts * 0.3)  # 300ms avg response
    total_artist_time = to_check_count * estimated_time_per_artist
    
    # Adjust for concurrency
    estimated_seconds = total_artist_time / effective_rate
    
    if estimated_seconds < 60:
        return f"~{estimated_seconds:.0f} seconds"
    elif estimated_seconds < 3600:
        return f"~{estimated_seconds/60:.1f} minutes"
    else:
        return f"~{estimated_seconds/3600:.1f} hours"


async def check_mbid_with_cache_warming(
    session: aiohttp.ClientSession,
    mbid: str,
    target_base_url: str,
    max_attempts: int = 25,
    delay_between_attempts: float = 0.5,
    timeout: int = 10
) -> Tuple[str, str, int, float]:
    """Check single MBID with cache warming - keep trying until success or max attempts"""
    url = f"{target_base_url.rstrip('/')}/artist/{mbid}"
    total_response_time = 0
    
    for attempt in range(max_attempts):
        start_time = time.time()
        try:
            async with session.get(url) as resp:
                response_time = time.time() - start_time
                total_response_time += response_time
                status_code = resp.status
                
                if status_code == 200:
                    # SUCCESS! Cache warming worked
                    return "success", str(status_code), attempt + 1, total_response_time
                
                # For cache warming, we retry ALL non-200 responses
                # (503, 404, 429, etc. - keep trying until cache warms up)
                
        except asyncio.TimeoutError:
            response_time = time.time() - start_time
            total_response_time += response_time
            status_code = "TIMEOUT"
        except Exception as e:
            response_time = time.time() - start_time
            total_response_time += response_time
            # For cache warming, even exceptions are worth retrying
            status_code = f"EXC:{type(e).__name__}"
        
        # Wait between attempts (unless it's the last attempt)
        if attempt < max_attempts - 1:
            await asyncio.sleep(delay_between_attempts)
    
    # Exhausted all attempts without success
    return "timeout", str(status_code), max_attempts, total_response_time


async def check_mbids_concurrent(
    to_check: List[str],
    cfg: dict,
    ledger: dict,
    mbid_to_name: dict,
    mbid_to_lidarr_id: dict
) -> Tuple[int, int, int]:
    """Check MBIDs concurrently with individual logging. Returns (transitioned_count, new_successes, new_failures)"""
    
    rate_limiter = SafeRateLimiter(
        requests_per_second=cfg["rate_limit_per_second"],
        max_concurrent=cfg["max_concurrent_requests"],
        circuit_breaker_threshold=cfg.get("circuit_breaker_threshold", 10),
        backoff_factor=cfg.get("backoff_factor", 2.0),
        max_backoff_seconds=cfg.get("max_backoff_seconds", 60)
    )
    
    transitioned_count = 0
    new_successes = 0
    new_failures = 0
    timeout_obj = aiohttp.ClientTimeout(total=cfg["timeout_seconds"])
    
    async with aiohttp.ClientSession(timeout=timeout_obj) as session:
        for i, mbid in enumerate(to_check):
            # Check circuit breaker
            if not await rate_limiter.acquire():
                print(f"🚫 Circuit breaker open, skipping remaining {len(to_check) - i} MBIDs")
                break
            
            name = mbid_to_name.get(mbid, 'Unknown')
            prev_status = ledger[mbid].get("status", "").lower()
            
            print(f"[{i+1}/{len(to_check)}] Checking {name} [{mbid}] ...", end="", flush=True)
            
            try:
                status, last_code, attempts_used, response_time = await check_mbid_with_cache_warming(
                    session,
                    mbid,
                    cfg["target_base_url"],
                    cfg["max_attempts_per_artist"],
                    cfg["delay_between_attempts"],
                    cfg["timeout_seconds"]
                )
                
                rate_limiter.release(int(last_code) if last_code.isdigit() else last_code, response_time)
                
                # Update ledger
                ledger[mbid].update({
                    "status": status,
                    "attempts": attempts_used,
                    "last_status_code": last_code,
                    "last_checked": iso_now()
                })
                
                # Count results
                if status == "success":
                    new_successes += 1
                    print(f" SUCCESS (code={last_code}, attempts={attempts_used})")
                else:
                    new_failures += 1
                    print(f" TIMEOUT (code={last_code}, attempts={attempts_used})")
                
                # Trigger Lidarr refresh if configured
                if (cfg.get("update_lidarr", False) 
                    and status == "success" 
                    and prev_status in ("", "timeout")):
                    artist_id = mbid_to_lidarr_id.get(mbid)
                    trigger_lidarr_refresh(cfg["lidarr_url"], cfg["api_key"], artist_id)
                    transitioned_count += 1
                    print(f"  -> Triggered Lidarr refresh for {name} [artist_id={artist_id}]")
                
            except Exception as e:
                response_time = 1.0  # Estimate for failed requests
                rate_limiter.release(0, response_time)
                
                ledger[mbid].update({
                    "status": "timeout",
                    "attempts": 3,
                    "last_status_code": f"EXC:{type(e).__name__}",
                    "last_checked": iso_now()
                })
                
                new_failures += 1
                print(f" TIMEOUT (code=EXC:{type(e).__name__}, attempts=3)")
            
            # Batch writing
            if (i + 1) % cfg.get("batch_write_frequency", 5) == 0:
                write_ledger(cfg["csv_path"], ledger)
            
            # Progress reporting
            if (i + 1) % cfg.get("log_progress_every_n", 25) == 0:
                elapsed_time = time.time()
                rate = (i + 1) / max(elapsed_time - start_run_time, 1) if 'start_run_time' in globals() else 0
                eta_seconds = (len(to_check) - i - 1) / max(rate, 0.1)
                stats = rate_limiter.get_stats()
                print(f"Progress: {i+1}/{len(to_check)} ({((i+1)/len(to_check)*100):.1f}%) - "
                      f"Rate: {rate:.1f}/sec - ETA: {eta_seconds/60:.1f}min - "
                      f"API: {stats.get('current_rate', 'N/A')}")
    
    return transitioned_count, new_successes, new_failures


def get_lidarr_artists(base_url: str, api_key: str, timeout: int = 30) -> List[Dict]:
    """Fetch artists from Lidarr and return a list of dicts with {id, name, mbid}."""
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
                lidarr_id = a.get("id")
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
    """Read existing CSV into a dict keyed by MBID."""
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
    """Write the ledger dict back to CSV atomically."""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    fieldnames = ["mbid", "artist_name", "status", "attempts", "last_status_code", "last_checked"]
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for _, row in sorted(ledger.items(), key=lambda kv: (kv[1].get("artist_name", ""), kv[0])):
            writer.writerow(row)
    os.replace(tmp_path, csv_path)


def load_config(path: str) -> dict:
    """Load INI config and return a normalized dict of settings with defaults."""
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

    # Load configuration with updated defaults
    cfg = {
        # Core settings
        "lidarr_url": cp.get("lidarr", "base_url", fallback="http://192.168.1.103:8686"),
        "api_key": cp.get("lidarr", "api_key", fallback=""),
        "target_base_url": cp.get("probe", "target_base_url", fallback="https://api.lidarr.audio/api/v0.4"),
        "timeout_seconds": cp.getint("probe", "timeout_seconds", fallback=10),
        "csv_path": cp.get("ledger", "csv_path", fallback="mbids.csv"),
        "force": parse_bool(cp.get("run", "force", fallback="false")),
        "update_lidarr": parse_bool(cp.get("actions", "update_lidarr", fallback="false")),
        
        # Concurrent settings (your specified defaults)
        "max_concurrent_requests": cp.getint("probe", "max_concurrent_requests", fallback=5),
        "rate_limit_per_second": cp.getfloat("probe", "rate_limit_per_second", fallback=3),
        
        # Per-artist cache warming settings  
        "max_attempts_per_artist": cp.getint("probe", "max_attempts_per_artist", fallback=25),
        "delay_between_attempts": cp.getfloat("probe", "delay_between_attempts", fallback=0.5),
        
        # Circuit breaker settings (for completely broken API)
        "circuit_breaker_threshold": cp.getint("probe", "circuit_breaker_threshold", fallback=25),
        "backoff_factor": cp.getfloat("probe", "backoff_factor", fallback=2.0),
        "max_backoff_seconds": cp.getfloat("probe", "max_backoff_seconds", fallback=60),
        
        # Processing options (updated defaults)
        "batch_size": cp.getint("run", "batch_size", fallback=25),
        "batch_write_frequency": cp.getint("run", "batch_write_frequency", fallback=5),
        
        # Monitoring options
        "log_progress_every_n": cp.getint("monitoring", "log_progress_every_n", fallback=25),
        "log_level": cp.get("monitoring", "log_level", fallback="INFO"),
    }

    if not cfg["api_key"] or "REPLACE_WITH_YOUR_LIDARR_API_KEY" in cfg["api_key"]:
        raise ValueError("Missing [lidarr].api_key in config (or still using the placeholder).")

    return cfg


def trigger_lidarr_refresh(base_url: str, api_key: str, artist_id: Optional[int]) -> None:
    """Fire-and-forget refresh request to Lidarr for the given artist id."""
    if artist_id is None:
        return
    session = requests.Session()
    headers = {"X-Api-Key": api_key}
    payloads = [
        {"name": "RefreshArtist", "artistIds": [artist_id]},
        {"name": "RefreshArtist", "artistId": artist_id},
    ]
    for path in ("/api/v1/command", "/api/command"):
        url = f"{base_url.rstrip('/')}{path}"
        for body in payloads:
            try:
                session.post(url, headers=headers, json=body, timeout=0.5)
                return
            except Exception:
                continue


def process_mbids_in_batches(
    to_check: List[str], 
    cfg: dict, 
    ledger: dict,
    mbid_to_name: dict,
    mbid_to_lidarr_id: dict
) -> Tuple[int, int, int]:
    """Process MBIDs in batches. Returns (transitioned_count, total_new_successes, total_new_failures)"""
    batch_size = cfg.get("batch_size", 25)
    total_batches = (len(to_check) + batch_size - 1) // batch_size
    total_transitioned = 0
    total_new_successes = 0
    total_new_failures = 0
    
    # Track timing across all batches
    overall_start_time = time.time()
    total_processed = 0
    
    for batch_idx in range(0, len(to_check), batch_size):
        batch_num = batch_idx // batch_size + 1
        batch = to_check[batch_idx:batch_idx + batch_size]
        
        print(f"=== Batch {batch_num}/{total_batches} ({len(batch)} MBIDs) ===")
        
        batch_transitioned, batch_successes, batch_failures = asyncio.run(
            check_mbids_concurrent_with_timing(batch, cfg, ledger, mbid_to_name, mbid_to_lidarr_id, overall_start_time, total_processed)
        )
        
        total_transitioned += batch_transitioned
        total_new_successes += batch_successes
        total_new_failures += batch_failures
        total_processed += len(batch)
        
        # Write after each batch
        write_ledger(cfg["csv_path"], ledger)
        print(f"Batch {batch_num} complete. Ledger updated.")
        
        # Optional: brief pause between batches
        if batch_num < total_batches and cfg.get("batch_pause_seconds", 0) > 0:
            time.sleep(cfg["batch_pause_seconds"])
    
    return total_transitioned, total_new_successes, total_new_failures


async def check_mbids_concurrent_with_timing(
    to_check: List[str],
    cfg: dict,
    ledger: dict,
    mbid_to_name: dict,
    mbid_to_lidarr_id: dict,
    overall_start_time: float,
    offset: int
) -> Tuple[int, int, int]:
    """Check MBIDs concurrently with proper timing across batches"""
    
    rate_limiter = SafeRateLimiter(
        requests_per_second=cfg["rate_limit_per_second"],
        max_concurrent=cfg["max_concurrent_requests"],
        circuit_breaker_threshold=cfg.get("circuit_breaker_threshold", 25),
        backoff_factor=cfg.get("backoff_factor", 2.0),
        max_backoff_seconds=cfg.get("max_backoff_seconds", 60)
    )
    
    transitioned_count = 0
    new_successes = 0
    new_failures = 0
    timeout_obj = aiohttp.ClientTimeout(total=cfg["timeout_seconds"])
    
    async with aiohttp.ClientSession(timeout=timeout_obj) as session:
        for i, mbid in enumerate(to_check):
            # Check circuit breaker
            if not await rate_limiter.acquire():
                print(f"🚫 Circuit breaker open, skipping remaining {len(to_check) - i} MBIDs")
                break
            
            name = mbid_to_name.get(mbid, 'Unknown')
            prev_status = ledger[mbid].get("status", "").lower()
            
            # Use offset for proper numbering across batches
            global_position = offset + i + 1
            total_to_process = offset + len(to_check)
            
            print(f"[{global_position}/{total_to_process}] Checking {name} [{mbid}] ...", end="", flush=True)
            
            try:
                status, last_code, attempts_used, response_time = await check_mbid_with_cache_warming(
                    session,
                    mbid,
                    cfg["target_base_url"],
                    cfg["max_attempts_per_artist"],
                    cfg["delay_between_attempts"],
                    cfg["timeout_seconds"]
                )
                
                rate_limiter.release(int(last_code) if last_code.isdigit() else last_code, response_time)
                
                # Update ledger
                ledger[mbid].update({
                    "status": status,
                    "attempts": attempts_used,
                    "last_status_code": last_code,
                    "last_checked": iso_now()
                })
                
                # Count results
                if status == "success":
                    new_successes += 1
                    print(f" SUCCESS (code={last_code}, attempts={attempts_used})")
                else:
                    new_failures += 1
                    print(f" TIMEOUT (code={last_code}, attempts={attempts_used})")
                
                # Trigger Lidarr refresh if configured
                if (cfg.get("update_lidarr", False) 
                    and status == "success" 
                    and prev_status in ("", "timeout")):
                    artist_id = mbid_to_lidarr_id.get(mbid)
                    trigger_lidarr_refresh(cfg["lidarr_url"], cfg["api_key"], artist_id)
                    transitioned_count += 1
                    print(f"  -> Triggered Lidarr refresh for {name} [artist_id={artist_id}]")
                
            except Exception as e:
                response_time = 1.0  # Estimate for failed requests
                rate_limiter.release("EXC", response_time)
                
                ledger[mbid].update({
                    "status": "timeout",
                    "attempts": cfg["max_attempts_per_artist"],
                    "last_status_code": f"EXC:{type(e).__name__}",
                    "last_checked": iso_now()
                })
                
                new_failures += 1
                print(f" TIMEOUT (code=EXC:{type(e).__name__}, attempts={cfg['max_attempts_per_artist']})")
            
            # Batch writing
            if global_position % cfg.get("batch_write_frequency", 5) == 0:
                write_ledger(cfg["csv_path"], ledger)
            
            # Progress reporting with correct calculations across all batches
            if global_position % cfg.get("log_progress_every_n", 25) == 0:
                elapsed_time = time.time() - overall_start_time
                artists_per_sec = global_position / max(elapsed_time, 0.1)
                remaining_artists = total_to_process - global_position
                eta_seconds = remaining_artists / max(artists_per_sec, 0.01)
                
                # Calculate ETC (Estimated Time to Completion)
                etc_timestamp = datetime.now() + timedelta(seconds=eta_seconds)
                etc_str = etc_timestamp.strftime("%H:%M")
                
                stats = rate_limiter.get_stats()
                
                print(f"Progress: {global_position}/{total_to_process} ({(global_position/total_to_process*100):.1f}%) - "
                      f"Rate: {artists_per_sec:.1f} artists/sec - ETC: {etc_str} - "
                      f"API: {stats.get('current_rate', 'N/A')}")
    
    return transitioned_count, new_successes, new_failures


# Remove the global start_run_time since we're now tracking it properly per batch
def main():
    
    parser = argparse.ArgumentParser(
        description="Query Lidarr for MBIDs, keep a CSV ledger, and probe each MBID against a target endpoint with concurrent processing."
    )
    parser.add_argument("--config", required=True, help="Path to INI config (e.g., /data/config.ini)")
    parser.add_argument("--force", action="store_true",
                        help="Re-run checks even for MBIDs already marked success")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without making API calls")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"ERROR loading config: {e}", file=sys.stderr)
        sys.exit(2)

    # Apply CLI overrides
    if args.force:
        cfg["force"] = True
        print("Force mode enabled: will re-check all MBIDs.")

    # Validate configuration
    config_issues = validate_config(cfg)
    if config_issues:
        print("Configuration issues found:", file=sys.stderr)
        for issue in config_issues:
            print(f"  - {issue}", file=sys.stderr)
        sys.exit(2)

    # Load ledger
    ledger = read_ledger(cfg["csv_path"])

    # Fetch current artists/MBIDs from Lidarr
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

    # Merge in any new MBIDs
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

    # Determine which MBIDs to check
    to_check = []
    for mbid, row in ledger.items():
        status = (row.get("status") or "").lower()
        if cfg["force"] or status not in ("success",):
            to_check.append(mbid)

    # Show summary and estimates
    estimated_time = estimate_runtime(len(to_check), cfg)
    
    print(f"Discovered {len(artists)} artists ({new_count} new).")
    print(f"Will check {len(to_check)} MBIDs ({'force mode' if cfg['force'] else 'pending-only'}).")
    
    if args.dry_run:
        print("DRY RUN MODE - No API calls will be made")
        print("This would check the following MBIDs:")
        for i, mbid in enumerate(to_check[:10]):  # Show first 10
            name = mbid_to_name.get(mbid, 'Unknown')
            print(f"  {i+1}. {name} [{mbid}]")
        if len(to_check) > 10:
            print(f"  ... and {len(to_check) - 10} more")
        return

    if len(to_check) == 0:
        print("Nothing to check - all MBIDs are already successful")
        return

    # Start processing (no global timing needed)

    try:
        if cfg.get("batch_size", 25) < len(to_check):
            # Use batch processing for large sets
            transitioned_count, new_successes, new_failures = process_mbids_in_batches(
                to_check, cfg, ledger, mbid_to_name, mbid_to_lidarr_id
            )
        else:
            # Process all at once for smaller sets
            transitioned_count, new_successes, new_failures = asyncio.run(
                check_mbids_concurrent_with_timing(to_check, cfg, ledger, mbid_to_name, mbid_to_lidarr_id, time.time(), 0)
            )

    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user. Saving progress...")
        write_ledger(cfg["csv_path"], ledger)
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error during processing: {e}", file=sys.stderr)
        write_ledger(cfg["csv_path"], ledger)
        raise

    # Final write and summary
    write_ledger(cfg["csv_path"], ledger)

    # Calculate final statistics
    successes = sum(1 for r in ledger.values() if r.get("status") == "success")
    timeouts = sum(1 for r in ledger.values() if r.get("status") == "timeout")
    pending = len(ledger) - successes - timeouts

    # Console summary
    print(f"\nSummary:")
    print(f"  Total in ledger: {len(ledger)}")
    print(f"  Success: {successes}")
    print(f"  Timeout: {timeouts}")
    print(f"  Refreshes triggered (new successes): {transitioned_count}")
    print(f"\nCSV written to: {cfg['csv_path']}")

    # Write simple results log
    try:
        os.makedirs("/data", exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = f"/data/results_{ts}.log"
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"finished_at_utc={iso_now()}\n")
            lf.write(f"success={successes}\n")
            lf.write(f"timeout={timeouts}\n")
            lf.write(f"pending={pending}\n")
            lf.write(f"total={len(ledger)}\n")
            lf.write(f"force_mode={'true' if cfg['force'] else 'false'}\n")
            lf.write(f"refreshes_triggered={transitioned_count}\n")
            lf.write(f"new_successes_this_run={new_successes}\n")
            lf.write(f"new_failures_this_run={new_failures}\n")
            lf.write(f"checked_this_run={len(to_check)}\n")
        print(f"Results log written to: {log_path}")
    except Exception as e:
        print(f"WARNING: Failed to write results log to /data: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
