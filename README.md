# AQI Dashboard

> A multi-source air quality monitoring desktop app for Windows. Auto-launches on boot, surfaces a consensus AQI from independent sensor networks, and lets you query any locality on demand.

---

## The problem this solves

Delhi air quality is unpredictable, hyperlocal, and consequential. The official CPCB website is slow and shows one station at a time. Phone apps work but require unlocking, opening, navigating. None of them tell you whether the reading you're seeing is corroborated by other sensors or is an outlier from a single malfunctioning monitor.

This app addresses three specific gaps:

1. **Zero-friction surfacing** — air quality is the first thing you see when your computer boots, not something you have to remember to check.
2. **Cross-source validation** — instead of trusting a single API, three independent sources are queried in parallel and a median consensus is shown. Sensor disagreements are flagged.
3. **Locality awareness** — alongside your current location, you can search any neighbourhood by name and immediately see the nearest station's AQI.

The target user is someone living in a high-pollution area who treats AQI as actionable data — deciding whether to run outdoors, whether to mask up, whether to keep windows open — and wants that decision input present passively rather than actively pursued.

---

## What it does

On every Windows login:

- A dashboard window appears (no console, no terminal)
- Three reference cards show your locality AQI, city overview, and consensus across all sources
- A nearby-stations table lists every monitor within a configurable radius (default 10 km)
- A hotspots table ranks the worst-AQI stations within a wider radius (default 30 km)
- A search bar accepts any place name — types it through a geocoder, then resolves to the nearest sensor
- A status bar reports which sources responded, when the data was last fetched, and warnings if any
- If every live source fails, a cached reading is shown with a stale-data warning

All of this is read-only and runs in the background; it consumes ~50 MB of RAM and 0% CPU at idle.

---

## How it works (high-level)

### The data sources

| Source | What it provides | Why it's included |
|---|---|---|
| **WAQI** (aqicn.org) | Aggregates CPCB + US embassy + private monitors, normalized to US AQI | Best Indian station coverage; the primary data backbone |
| **OpenAQ** | Open-source ingestion of CPCB raw data | Cross-pipeline check on WAQI's CPCB-derived numbers |
| **OpenWeatherMap** | Model + satellite-derived PM2.5, converted to US AQI via EPA breakpoints | Genuinely independent methodology — not based on the same physical sensors |
| **IQAir** (optional) | Independent commercial sensor network | Adds a fourth perspective if enabled |

The architecture deliberately treats no single source as canonical. WAQI is queried first because of its station-level depth, but the consensus AQI shown to the user is the **median** of every source that responded — not WAQI's number with the others as backup.

### The fallback chain

```
                       LOCALITY AQI
                            │
        ┌───────────┬───────┴───────┬────────────┐
        │           │               │            │
      WAQI        IQAir          OpenAQ        OWM
   (priority 1) (priority 2)  (priority 3)  (priority 4)
        │           │               │            │
        └───────────┴───────┬───────┴────────────┘
                            ▼
                    Median = Consensus AQI

                  ALL FOUR FAILED?
                            │
                            ▼
                  Load cached reading
                  Display "stale" warning
```

The app shows the highest-priority source's reading as the "locality" number (because WAQI gives station-level granularity that OWM doesn't), but the consensus card aggregates everything. If three sources read 165–170 and one reads 240, you see "spread — sensors disagree" as a warning, prompting you not to trust the apparent precision of any single number.

### The search flow

When you type a locality:

```
  "West Patel Nagar Delhi"
            │
            ▼  (Nominatim geocoder — OpenStreetMap)
   28.6483, 77.1686
            │
            ▼  (WAQI /feed/geo/ endpoint)
   Nearest station: Punjabi Bagh, AQI 171
```

This is fundamentally different from keyword-searching WAQI's station database, which fails for residential neighbourhoods that don't have their own monitors. By geocoding the user's text first, then asking WAQI which physical station is closest to those coordinates, the search works for any place name OpenStreetMap recognizes — which is essentially every populated locality globally.

### Auto-start mechanism

A registry key under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` points to `pythonw.exe dashboard.py`. Windows runs this on every login. `pythonw` is the silent variant of Python — no console window flashes during startup. No services, no scheduled tasks, no admin rights needed.

---

## Setup

### Prerequisites

- Windows 10/11
- Python 3.10+ (avoid Microsoft Store version — path quirks break the autostart registry trick)

### Install

```powershell
cd path\to\aqi-dashboard
pip install -r requirements.txt
copy config.example.json config.json
```

### Configure

Edit `config.json`:

```json
{
  "waqi_token": "GET_THIS_FROM_AQICN",
  "openweather_key": "OPTIONAL",
  "iqair_key": "",
  "openaq_key": "OPTIONAL",
  "location": {
    "auto": false,
    "city": "Delhi",
    "lat": 28.6139,
    "lng": 77.2090
  },
  "radius_km": 10,
  "hotspot_radius_km": 30
}
```

API keys (all free):
- WAQI: https://aqicn.org/data-platform/token/ (required)
- OpenWeatherMap: https://openweathermap.org/api (recommended — adds independent methodology)
- OpenAQ: https://explore.openaq.org (optional — same data as WAQI ultimately)
- IQAir: skip unless explicitly wanted (their email verification flow has been flagged as suspicious)

Set `auto: false` and hardcode `lat`/`lng` to avoid IP-based geolocation misfires (ISPs route traffic through other cities — Delhi users frequently get pegged as Noida).

### Run

```powershell
python dashboard.py
```

### Install as startup item

```powershell
python setup_autostart.py
```

To remove later: `python uninstall_autostart.py`

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Fast iteration, rich ecosystem, good API libraries |
| GUI | PySide6 (Qt 6) | Native look, excellent threading model, no Electron bloat |
| HTTP | `requests` | Synchronous; threading is handled at the QThread layer |
| Persistence | JSON file | Single cached snapshot, no database overhead |
| Auto-start | Registry (HKCU Run key) | No services, no admin rights, no scheduled tasks |

No frontend framework, no database, no background daemon, no native dependencies beyond Python. Total install footprint is around 80 MB (mostly PySide6's Qt bundle).

---

## Project structure

```
aqi-dashboard/
├── dashboard.py            # PySide6 UI + main entry point
├── aggregator.py           # Multi-source orchestration, consensus logic
├── waqi.py                 # WAQI API client
├── openaq.py               # OpenAQ API client
├── openweather.py          # OpenWeatherMap client + PM2.5→AQI conversion
├── iqair.py                # IQAir client (optional)
├── geolocation.py          # IP-based geolocation
├── geocoder.py             # Place name → lat/lng (Nominatim/Photon)
├── cache.py                # JSON-backed last-known-reading cache
├── setup_autostart.py      # Adds Windows registry entry
├── uninstall_autostart.py  # Removes Windows registry entry
├── config.example.json     # Config template
├── config.json             # User config (gitignored, created from example)
├── aqi_cache.json          # Auto-generated runtime cache (gitignored)
├── requirements.txt        # PySide6, requests
├── README.md               # This file
└── DOCUMENTATION.md        # Code-level architecture deep-dive
```

For implementation details — module breakdowns, function-level explanations, threading model, data flow — see `DOCUMENTATION.md`.

---

## Limitations

- **CPCB native AQI is not used.** WAQI normalizes to US EPA AQI; CPCB uses Indian breakpoints which differ in the 100–200 band. Numbers shown are EPA-equivalent, not CPCB-equivalent.
- **Hotspots are real-time, not chronic.** A station can spike from a momentary fire/firework/traffic event. For chronic hotspot tracking, historical data logging would need to be added.
- **No alerts.** The app surfaces info on boot but doesn't wake up to notify you if AQI crosses a threshold mid-day.
- **No mobile parity.** Windows-only by design; the autostart hook is registry-based.

---

## Possible extensions

- Historical AQI tracking (SQLite + 7/30-day rolling aggregates)
- Threshold-based desktop notifications (`winotify` toast on AQI > N)
- Map view with station overlay (Folium HTML embedded via `QWebEngineView`)
- Direct CPCB scraping for native Indian AQI numbers
- PyInstaller bundle for distribution as a single `.exe` with no Python prerequisite
