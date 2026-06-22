# EPANET Hydraulic Simulation Service (EPyT-Flow)

A standalone **FastAPI** microservice that reads `.gpkg` pipe-network files
from a shared Docker volume, runs Extended Period Simulations using
**EPyT-Flow** (EPANET Python Toolkit — Flow), and exposes all results
through a REST API secured by API-key authentication.

---

## Why EPyT-Flow?

EPyT-Flow wraps the official **EPANET C library** directly and adds:

| Feature | Benefit for DUWAS |
|---------|------------------|
| `AbruptLeakage` event | Inject a reported leak as a physical pipe-burst — simulate the pressure drop it causes across the network |
| `ScenarioSimulator` | High-level runner; handles timing, quality, and events in one call |
| `ScadaData` | Structured result object with per-sensor time series — maps exactly to your sensor network |
| Water-age quality | Track water age at every node across the 24-hour EPS |
| Sensor config | Declare which nodes/pipes are monitored — mirrors your real sensor layout |

---

## Quick start

```bash
cp .env.example .env
# Edit .env — generate a secure key:
#   python -c "import secrets; print(secrets.token_hex(32))"

docker compose up -d --build

# Copy your cleaned network file into the shared volume
docker cp duwas_network_clean.gpkg epanet_service:/data/gpkg/

# Open interactive API docs
open http://localhost:8080/docs
```

---

## Project structure

```
epanet_service/
├── app/
│   ├── main.py                      FastAPI app entry point
│   ├── core/
│   │   ├── config.py                Settings from .env
│   │   ├── auth.py                  X-API-Key dependency
│   │   ├── database.py              Async SQLAlchemy + SQLite
│   │   └── exceptions.py            HTTP error helpers
│   ├── models/simulation.py         ORM: sim_scenarios + sim_results
│   ├── routers/
│   │   ├── files.py                 GET /files
│   │   └── simulation.py            All /simulate endpoints
│   ├── services/
│   │   ├── network_builder.py       .gpkg → EPANET .inp (pure stdlib)
│   │   └── simulation_service.py    EPyT-Flow ScenarioSimulator runner
│   └── workers/simulation_worker.py Background task orchestrator
├── tests/test_api.py                pytest integration tests
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## API reference

All endpoints require `X-API-Key` header.

### Files
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/files` | List `.gpkg` files in the shared volume |

### Simulations
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/simulate` | Queue simulation (202 response) |
| GET | `/simulate` | List all scenarios |
| GET | `/simulate/{id}` | Status + summary — poll this |
| GET | `/simulate/{id}/nodes` | Node results (`?time_step=0`) |
| GET | `/simulate/{id}/pipes` | Pipe results (`?time_step=0`) |
| GET | `/simulate/{id}/alerts` | Low-pressure + high-velocity anomalies |
| GET | `/simulate/{id}/geojson/nodes` | Node GeoJSON for Leaflet/MapLibre |
| GET | `/simulate/{id}/geojson/pipes` | Pipe GeoJSON for Leaflet/MapLibre |
| DELETE | `/simulate/{id}` | Delete scenario + all results |

### Health
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness probe (no auth needed) |

---

## Calling from your main system

### Submit a simulation with leak events

```json
POST /simulate
X-API-Key: your-key

{
  "gpkg_filename":  "duwas_network_clean.gpkg",
  "name":           "Morning peak with active leaks",
  "pipe_status":    "OPERATIONAL",
  "base_demand":    0.001,
  "duration_hrs":   24,
  "time_step_min":  60,
  "reservoir_head": 50.0,
  "leak_events": [
    {
      "lat":             -2.5160,
      "lon":             32.9012,
      "demand_m3s":      0.005,
      "leak_diameter_m": 0.01,
      "start_time_s":    3600,
      "end_time_s":      86400
    }
  ]
}
```

Each `leak_event` is:
- Snapped to the nearest network node in the `.inp`
- Injected as an `EPyT-Flow AbruptLeakage` event
- Simulated as a physical orifice with the given `leak_diameter_m`
- The pressure drop around that node is visible in `/nodes` results

### Poll until done

```http
GET /simulate/7
X-API-Key: your-key
```

When `status = "DONE"`, the `summary` field contains:

```json
{
  "pressure_min_m":       12.4,
  "pressure_max_m":       48.2,
  "pressure_avg_m":       31.7,
  "flow_max_m3s":         0.0183,
  "water_age_max_hrs":    18.5,
  "low_pressure_nodes":   3,
  "high_velocity_pipes":  1,
  "leak_events_injected": 1,
  "total_nodes":          412,
  "total_pipes":          389,
  "engine":               "EPyT-Flow"
}
```

### Fetch GeoJSON for the map

```http
GET /simulate/7/geojson/nodes?time_step=6
X-API-Key: your-key
```

Returns a standard GeoJSON `FeatureCollection` ready for Leaflet:

```js
const geoJson = await fetch('/simulate/7/geojson/nodes?time_step=6', {
  headers: { 'X-API-Key': 'your-key' }
}).then(r => r.json());

L.geoJSON(geoJson, {
  pointToLayer: (feature, latlng) =>
    L.circleMarker(latlng, {
      color: feature.properties.is_low_pressure ? 'red' : 'green'
    })
}).addTo(map);
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEYS` | `change-me-key-1` | Comma-separated valid API keys |
| `GPKG_DIR` | `/data/gpkg` | Path to `.gpkg` files inside container |
| `DATABASE_URL` | `sqlite+aiosqlite:////data/db/epanet_service.db` | Async DB URL |
| `DEFAULT_DURATION_HRS` | `24` | Default simulation duration |
| `DEFAULT_TIMESTEP_MIN` | `60` | Default time step (minutes) |
| `DEFAULT_BASE_DEMAND` | `0.001` | Default demand per node (m³/s) |
| `MIN_PRESSURE_M` | `7.0` | Low-pressure alert threshold (m) |
| `MAX_VELOCITY_MS` | `3.0` | High-velocity alert threshold (m/s) |
| `DEBUG` | `false` | Enable SQLAlchemy query logging |

---

## Putting .gpkg files on the volume

**Option A — docker cp (quick)**
```bash
docker cp duwas_network_clean.gpkg epanet_service:/data/gpkg/
```

**Option B — bind-mount a host directory** (edit `docker-compose.yml`):
```yaml
volumes:
  gpkg_data:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /home/ubuntu/gpkg_files
```

---

## Running tests

```bash
pip install -r requirements.txt
TEST_GPKG_FILE=duwas_network_clean.gpkg \
GPKG_DIR=/path/to/local/gpkg \
pytest tests/ -v
```

---

## Production notes

| Concern | Recommendation |
|---------|---------------|
| Long simulations | Replace `BackgroundTasks` with **Celery + Redis** |
| Multiple workers | Keep `--workers 1` (EPyT-Flow/EPANET C lib not thread-safe); scale with multiple containers behind a load balancer |
| Elevation data | Replace `elevation = 0.0` in `network_builder.py` with a DEM lookup (e.g. SRTM) |
| Reservoir head | Pass actual tank water level from your SCADA sensors via `reservoir_head` in the POST body |
| HTTPS | Terminate TLS at **nginx** or **Traefik** in front of this service |
| Secrets | Store `API_KEYS` in Docker secrets or Vault — not plain `.env` |
