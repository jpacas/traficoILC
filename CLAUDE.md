# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Monitor de Flujo de Caña** — A real-time monitoring system for sugarcane flow rates at Ingenio La Cabaña.

- **`monitor.py`** (worker): Polls the intranet traffic table every 5 minutes, persists readings to PostgreSQL (100 most recent kept)
- **`dashboard.py`** (web server): HTTP API serving historical metrics and flow calculations as JSON; serves HTML frontend on `/`
- **`dashboard.html`** (frontend): Single-page app displaying pipeline visualization, frente status cards, and flow trends

Data persistence: PostgreSQL in production (Railway); local JSON in development (legacy fallback).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Development: Monitor (polls intranet, writes to local JSON via fallback)
python3 monitor.py

# Development: Dashboard (serves on localhost:8080)
python3 dashboard.py

# View API: 
curl http://localhost:8080/api/data

# Production: Railway + Netlify deployment (see below)
```

## Architecture

### Three Layers

1. **Scraper Worker** (`monitor.py`)
   - Fetches HTML table from intranet (grupolacabana.net/consultasilc/Home/Trafico)
   - Parses 14 data columns per frente (codigo, frente, umoli, tmoli, upatio, tpatio, uplantel, tplantel, uvienen, tvienen, ucampo, tcampo, uvan, tvan)
   - Detects new data: compares `timestamp` OR `total` dict to skip duplicates
   - Persists to PostgreSQL: `INSERT INTO readings (fetch_time, data) VALUES ...` with duplicate detection
   - Keeps last 100 readings; older ones auto-deleted
   - Polling interval: 300s; retry on no new data: 60s

2. **Web Server + API** (`dashboard.py`)
   - HTTP on `0.0.0.0:$PORT` (Railway sets PORT via env var)
   - `GET /` → serves `dashboard.html`
   - `GET /api/data` → computes and returns JSON with:
     - `meta`: reading counts, timestamps, server time
     - `frentes`: per-frente data (snapshot, flow, status, trend, stages)
     - `total`: aggregate metrics + global stage flows
     - `global_stages`: five-stage pipeline deltas (campo→vienen→patio→plantel→molino)

3. **Frontend** (`dashboard.html`)
   - Pure HTML/CSS/JS; no frameworks
   - SVG pipeline diagrams with animated flow arrows
   - Responsive frente cards (grid layout) with expandable details
   - Auto-refresh: fetches `/api/data` every 30s
   - Shows progress bar while < 100 readings collected
   - Flow visualization: bars in green showing tons/hour on pipeline stages

### Data Structure (PostgreSQL)

```sql
CREATE TABLE readings (
    id SERIAL PRIMARY KEY,
    fetch_time TIMESTAMPTZ NOT NULL UNIQUE,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

Reading payload (JSONB):
```json
{
  "timestamp": "20/04/2026 12:27 pm",
  "fetch_time": "2026-04-20T12:27:30.123456",
  "frentes": {
    "1": {
      "frente": "Ingenio Norte",
      "tmoli": 1234.56,
      "tcampo": 100.0,
      ...
    }
  },
  "total": {
    "tmoli": 15000.00,
    ...
  }
}
```

### API Response Example

```json
{
  "meta": {
    "last_timestamp": "20/04/2026 12:27 pm",
    "last_fetch_time": "2026-04-20T12:27:30.123456",
    "readings_count": 42
  },
  "frentes": {
    "1": {
      "codigo": "1",
      "nombre": "Ingenio Norte",
      "snapshot": { ... },
      "flow": {
        "current_tph": 45.23,
        "avg_tph": 40.15,
        "delta_ton": 12.34,
        "history_points": 41
      },
      "status": "ok",
      "trend": "up",
      "stages": {
        "campo": {"delta_ton": 2.5, "flow_tph": 8.3, ...},
        "vienen": {...},
        "patio": {...},
        "plantel": {...},
        "molino": {...}
      }
    }
  },
  "total": { ... },
  "global_stages": { ... }
}
```

### Flow Calculation

For each frente, comparing reading[i] vs reading[i-1]:

1. `delta_ton = tmoli[i] - tmoli[i-1]`
2. `elapsed_h = (fetch_time[i] - fetch_time[i-1]) / 3600` (minimum 60s)
3. `flow_tph = delta_ton / elapsed_h`
4. Average historical flow: `mean(all flow_tph values where history_points ≥ 3)`

Status classification:
- If history ≥ 3: compare `current_tph` against `avg_tph * threshold_ratio`
- Else: use absolute thresholds (OK > 20 tph, LOW > 5 tph, STOP ≤ 0)

Stage flows: same calculation applied to each pipeline stage (campo, vienen, patio, plantel, molino).

### Key Thresholds

```python
THRESHOLD_OK_ABS = 20.0      # Absolute threshold for "ok" status
THRESHOLD_LOW_ABS = 5.0      # Absolute threshold for "low" status
THRESHOLD_OK_REL = 0.70      # Relative to average: 70% = "ok"
THRESHOLD_LOW_REL = 0.30     # Relative to average: 30% = "low"
TREND_BAND = 0.15            # ±15% band = "stable"; outside = "up"/"down"
```

## Deployment: Railway + Netlify

### Prerequisites

- GitHub repo
- Railway account (railway.app)
- Netlify account

### Steps

1. **Push to GitHub**
   ```bash
   git init && git add . && git commit -m "Initial commit"
   git remote add origin https://github.com/<user>/traficoILC
   git push -u origin main
   ```

2. **Deploy to Railway**
   - New project → Connect repo → Select branch
   - Add PostgreSQL add-on (Railway auto-sets `DATABASE_URL` env var)
   - Procfile defines both services:
     - `web: python3 dashboard.py` (listens on `$PORT`)
     - `worker: python3 monitor.py` (scraper loop)
   - Railway will auto-build with `nixpacks` (defined in `railway.toml`)
   - Get the public URL: `https://TU-APP.up.railway.app`

3. **Deploy to Netlify**
   - New site → Deploy manually or connect repo
   - Publish directory: `.` (root; serves `dashboard.html` + `*.html`)
   - In `netlify.toml`: replace `RAILWAY_URL` with actual Railway domain
   - Netlify will proxy `/api/*` requests to Railway

4. **Verify**
   ```bash
   # Direct API call to Railway
   curl https://TU-APP.up.railway.app/api/data
   
   # Proxied call via Netlify
   curl https://TU-APP.netlify.app/api/data
   
   # Dashboard HTML
   curl https://TU-APP.netlify.app/
   ```

### Files for Deployment

- `Procfile`: Process definitions (web + worker)
- `railway.toml`: Railway build/deploy config
- `netlify.toml`: Netlify redirects (proxy rules)
- `.env.example`: Documents required env vars
- `requirements.txt`: Must include `psycopg2-binary`

### Local Development (PostgreSQL Optional)

If `DATABASE_URL` is not set, the code gracefully falls back to a warning and returns empty results. For full local testing:

```bash
# Install PostgreSQL locally, create a DB, then:
export DATABASE_URL="postgresql://user:password@localhost:5432/dbname"
python3 monitor.py   # Writes to DB
python3 dashboard.py # Reads from DB
```

## Important Context

- **Intranet access**: The page is public (not actually private). Railway containers can reach it directly.
- **Timestamp bug**: Server always reports "20/01/2022" in the page; code uses `fetch_time` (ISO) instead for accurate calculations.
- **Data duplication detection**: Compares both `timestamp` AND `total` dict to catch cases where server data doesn't update but timestamp repeats.
- **CORS headers**: Dashboard API includes `Access-Control-Allow-Origin: *` to enable frontend requests.
- **Stage flows**: Available only when `elapsed_h > 0` and previous reading exists; returns `null` otherwise.

## Testing

No automated tests. Verification:
1. Run `monitor.py` locally → check `history.json` or DB grows with new readings
2. Run `dashboard.py` → visit `http://localhost:8080` → verify HTML loads
3. Check `/api/data` → ensure JSON structure matches schema
4. After deploy to Railway → test public API endpoint
5. After deploy to Netlify → test `/api/*` proxy + HTML serving

## Dependencies

- `requests`: HTTP client (web scraping)
- `beautifulsoup4`: HTML parsing
- `tabulate`: Terminal table formatting (monitor.py display)
- `psycopg2-binary`: PostgreSQL adapter
