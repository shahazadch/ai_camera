# SentinelVision AI — Enterprise Security Platform

Hybrid-deployment MVP foundation for an AI-powered smart security and activity monitoring platform.

## What's included

### Documentation Suite

Ten-volume enterprise documentation with reproducible PDF generation.

**Phase 1 engineering-ready (expanded):** Volumes 3, 4, and 5.

| Vol | Document | PDF |
|-----|----------|-----|
| 1 | [Product Requirements](docs/volumes/01-product-requirements.md) | [PDF](docs/pdf/01-product-requirements.pdf) |
| 2 | [Software Requirements](docs/volumes/02-software-requirements.md) | [PDF](docs/pdf/02-software-requirements.pdf) |
| 3 | [System Design](docs/volumes/03-system-design.md) **engineering-ready** | [PDF](docs/pdf/03-system-design.pdf) |
| 4 | [Database Design](docs/volumes/04-database-design.md) **engineering-ready** | [PDF](docs/pdf/04-database-design.pdf) |
| 5 | [REST API](docs/volumes/05-rest-api.md) **engineering-ready** | [PDF](docs/pdf/05-rest-api.pdf) |
| 6 | [AI Design](docs/volumes/06-ai-design.md) | [PDF](docs/pdf/06-ai-design.pdf) |
| 7 | [UI/UX](docs/volumes/07-ui-ux.md) | [PDF](docs/pdf/07-ui-ux.pdf) |
| 8 | [DevOps & Deployment](docs/volumes/08-devops-deployment.md) | [PDF](docs/pdf/08-devops-deployment.pdf) |
| 9 | [Testing](docs/volumes/09-testing.md) | [PDF](docs/pdf/09-testing.pdf) |
| 10 | [Project Management](docs/volumes/10-project-management.md) | [PDF](docs/pdf/10-project-management.pdf) |

**Combined suite:** [docs/pdf/enterprise-ai-smart-security-documentation-suite.pdf](docs/pdf/enterprise-ai-smart-security-documentation-suite.pdf)

**OpenAPI:** [docs/openapi-schema.yml](docs/openapi-schema.yml) · Swagger UI at `/api/docs/`

See [docs/README.md](docs/README.md) for build instructions, glossary, traceability matrix, and status legend.

Legacy single PRD: [docs/PRD.md](docs/PRD.md) | [docs/AI-Smart-Security-Platform-PRD.pdf](docs/AI-Smart-Security-Platform-PRD.pdf)

### Application stack

- **Backend** — Django + DRF multi-tenant API with JWT auth, cameras, events, alerts, audit
- **Frontend** — React + TypeScript dashboard (responsive / PWA-ready)
- **Edge** — Python edge service with simulated YOLO detection and event ingestion
- **Docker Compose** — Full local stack with PostgreSQL

## Quick start (Docker)

On any laptop (Mac, Windows or Linux):

1. Install Docker Desktop and give it at least 4 GB of memory (Settings → Resources).
2. `git clone` this repo and `cd` into it.
3. Run:

```bash
docker compose up --build
```

The first build downloads Python packages and the YOLO model (needs internet, ~5 min).
After that the app runs fully offline. No `.env` is needed; copy `.env.example` to `.env`
only to change passwords or tuning.

4. Open http://localhost:3000, log in, and add your camera under **Cameras** with its real
   RTSP address, e.g. `rtsp://user:pass@192.168.1.64:554/...`. Don't use `localhost`: inside
   Docker it points at the container, not your laptop.

| Service   | URL                          |
|-----------|------------------------------|
| Dashboard | http://localhost:3000        |
| API       | http://localhost:8005/api/v1 |
| API Docs  | http://localhost:8005/api/docs/ |
| Health    | http://localhost:8005/api/v1/health/ |

**Demo login:** `admin` / `admin123`

The default stack is db, redis, backend, a 2-process Celery worker, beat and frontend (~1.2 GB RAM).
Person detection (YOLOv4-tiny) and known/unknown face recognition (YuNet + SFace) run inside
the backend, which loads the models once. Optional extras:

```bash
docker compose --profile storage up -d   # MinIO (set EVIDENCE_STORAGE_BACKEND=minio)
docker compose --profile edge up -d      # simulated edge service
```

Tuning env vars: `CAMERA_MONITOR_INTERVAL` (seconds, default 10, `0` = off),
`CONTINUOUS_RECORDING` (default `True`; each camera's *Auto record* switch still applies), `OPENCV_THREADS` (default 2), `LIVE_DETECT_FPS` (person-detection passes per second, default 4).

Inside containers `localhost` is the container itself. Use `host.docker.internal` for a stream on your Mac.

## Recording

Cameras with **Auto record** on (Cameras → Edit) are recorded continuously as 1-minute MP4
clips, listed under **Recordings**. ffmpeg copies the camera's own stream, so it needs almost
no CPU. On the Recordings page you can set:

- **Keep recordings for N days** (default 3). Older clips are deleted automatically every 30 minutes.
- **Quality**: *Standard* records the camera's sub-stream (~5 GB/day per camera); *High*
  records full resolution (~20 GB/day per camera).

## AI models & licences

Everything runs locally on the CPU with ONNX Runtime / OpenCV — no cloud, no API keys.

| Job | Model (default) | Licence | Switch |
|---|---|---|---|
| People, animals, phones, bags… | **D-FINE-S** (Objects365→COCO, 480 px) | Apache-2.0 code; Objects365-pretrained weights **not assumed commercially cleared** | `DETECTOR=dfine` (fallback `yolov4tiny`) |
| …same, commercial-safe | D-FINE-S (COCO only) | Apache-2.0 | `DFINE_WEIGHTS=coco` |
| Face detection + recognition | **InsightFace SCRFD-10G + ArcFace R50** | **Non-commercial only** | `FACE_MODEL=insightface` |
| Face detection + recognition (commercial-safe) | YuNet + SFace | MIT / Apache-2.0 | `FACE_MODEL=sface` |

Models are downloaded once into `backend/models/` (`python3 backend/scripts/fetch_models.py`)
and copied into the image; `docker compose build` downloads anything missing. Licences and
sources are listed in the generated `models/MODELS.md`.

**Before using this commercially, set `DFINE_WEIGHTS=coco` and `FACE_MODEL=sface`.**
Saved people are re-scanned automatically with the new face model; nobody has to enroll again.

Published accuracy: D-FINE-S 50.7 AP on COCO (48.5 COCO-only) vs YOLOv4-tiny 22.0 AP;
ArcFace R50 (buffalo_l) LFW 99.83 / CFP-FP 99.33 vs SFace LFW 99.40.

Measured on this site's own footage: D-FINE found 2.5× more of the hard (blurry, distant)
people than the old YOLOv4-tiny and labels farm animals as animals; the InsightFace detector
produced no fake faces on cow snapshots (YuNet: 5 of 16).

## Camera rules (sounds, photos, alerts)

Open **Camera Rules** in the sidebar. A rule says *what to detect*, *when* and *what to do*:

| Detect | When it fires |
|---|---|
| Any person | someone new walks into view |
| Unknown person | a clear face matches nobody saved |
| Saved person | a saved person (optionally a group or specific people) is recognized |
| Person using mobile | a person is holding / using a phone |
| Coming up the stairs | someone new appears at the bottom of the picture |

Each rule has a time window (Pakistan time, `ALERT_TIME_ZONE`; `22:00 → 06:00` covers the
night), optional weekdays, actions and a "don't repeat for N seconds" cooldown. Actions:
laptop sound (built-in chime, bell, doorbell, beep, horn, alarm, siren, or **your own tune**
uploaded under *Your tunes* on the Camera Rules page), a **spoken announcement** (e.g.
"{name} at {camera}"), save photo, alert info/warning/critical, and a dashboard pop-up.

Create a rule once, then switch it on per camera in **Cameras → Edit → Rules on this camera**,
or tick *Apply to all cameras*. Starter rules (Night intruder, Unknown visitor, Staff
arrived, Stairs) are created for you but aren't attached to any camera yet.

Face recognition only runs on a camera when its *Face recognition* module is ticked or one of
its rules needs faces, so cameras that only need "Any person" use much less CPU.

**Sound on the laptop.** Docker can't use your speakers, so a small player runs on the laptop
itself (Python 3, no installs) and works with no browser open:

```bash
python3 scripts/alert_player.py --test doorbell # hear a built-in tune
python3 scripts/alert_player.py --say "Hello"   # check the voice
python3 scripts/alert_player.py                 # run (Ctrl+C to stop)
./scripts/install_alert_player_mac.sh           # macOS: start automatically at login
./scripts/install_alert_player_mac.sh --uninstall
```

**Windows:** install Python 3 from python.org (tick *Add Python to PATH*), then in PowerShell:

```powershell
python scripts\alert_player.py --test siren
python scripts\alert_player.py
# start automatically at login (no window); add -Uninstall to remove
powershell -ExecutionPolicy Bypass -File scripts\install_alert_player_windows.ps1
```

Uploaded wav, mp3, m4a and aiff tunes play on Windows; ogg plays on Mac/Linux only.

If you set `ALERT_PLAYER_KEY` in `.env`, pass the same value with `--key`.

## Lite mode (no Docker, ~0.5 GB)

```bash
./scripts/run_lite.sh                       # backend on :8000, SQLite, no Redis/Celery
cd frontend && npm install && npm run dev   # dashboard on :5173
```

## Local development (without Docker)

### Backend

```bash
cd backend
pip install -r requirements-dev.txt   # includes pytest
cp .env.example .env   # uses SQLite by default
python manage.py migrate
python manage.py seed_dev_data
python manage.py runserver
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 — API requests proxy to http://localhost:8005

### AI Vision (free, no API key)

Default mode uses on-device YOLOv4-tiny COCO-80. No signup and no API key.

1. Start backend + frontend
2. Open Cameras → live view → **AI Scan**
3. Open `/vision` to browse saved captures

Optional richer local LLM (still free, still no key): install [Ollama](https://ollama.com), run `ollama pull llava`, set `VISION_LLM_PROVIDER=ollama`.

### Edge service

```bash
cd edge
pip install -r requirements.txt
cp .env.example .env
python -m edge_service.main
```

## Generate documentation PDFs

```bash
pip install -r tools/requirements-docs.txt
python tools/generate_volumes.py
python tools/build_documentation.py
python tools/validate_documentation.py
```

Outputs: `docs/pdf/01-product-requirements.pdf` through `docs/pdf/10-project-management.pdf`, plus combined suite PDF and `docs/document-manifest.json`.

## API overview

| Endpoint | Description |
|----------|-------------|
| `POST /api/v1/auth/token/` | JWT login |
| `GET /api/v1/dashboard/summary/` | Overview metrics |
| `GET /api/v1/cameras/` | Camera inventory |
| `GET /api/v1/events/` | Detection events |
| `POST /api/v1/events/ingest/` | Edge event ingestion |
| `POST /api/v1/cameras/{id}/vision/` | Capture frame + LLM scene analysis |
| `GET /api/v1/vision/captures/` | Saved AI Vision captures |
| `GET /api/v1/vision/status/` | Vision LLM configuration status |
| `GET /api/v1/alerts/` | Alert list |
| `POST /api/v1/edge/heartbeat/` | Edge heartbeat |
| `GET/POST /api/v1/persons/` | Person enrollment gallery |
| `POST /api/v1/face/match/` | Edge face recognition |
| `GET /api/v1/attendance/` | Attendance records |
| `GET/POST /api/v1/visitors/` | Visitor workflow |
| `GET /api/v1/unknown-sightings/` | Unknown person gallery |

Edge authentication uses the `X-Edge-API-Key` header.

## Testing

```bash
# Backend
cd backend && python -m pytest tests/ -q

# Edge
cd edge && python -m pytest tests/ -q

# Frontend
cd frontend && npm run lint && npm run build
```

## Architecture

```
Cameras → Edge AI (OpenCV/YOLO/InsightFace) → Django REST API → PostgreSQL
                                              ↓
                                         React Dashboard
```

## Milestones

**M2 detection pipeline** — [docs/M2-DETECTION-PIPELINE.md](docs/M2-DETECTION-PIPELINE.md)

**M3 face intelligence is implemented** — [docs/M3-FACE-INTELLIGENCE.md](docs/M3-FACE-INTELLIGENCE.md):

- Face detect / crop / blur / quality / align / best-face
- Person enrollment + biometric consent + embeddings
- InsightFace (or simulated) recognition → known / unknown
- Attendance clock-in/out with late & overtime
- Visitor request → approve → QR → check-in/out
- Unknown sighting gallery + alerts

**M4 advanced security analytics is implemented** — [docs/M4-ADVANCED-SECURITY-ANALYTICS.md](docs/M4-ADVANCED-SECURITY-ANALYTICS.md):

- Multi-zone intrusion with schedules, priority, entry/exit
- Loitering thresholds + escalation
- Crowd occupancy / density / heatmaps
- Object left / removed
- Risk correlation + configurable rule engine

**M5 life safety & emergency AI is implemented** — [docs/M5-LIFE-SAFETY-EMERGENCY-AI.md](docs/M5-LIFE-SAFETY-EMERGENCY-AI.md):

- Multi-frame fire verification + optional evidence clips
- Smoke density, trend, persistence, and escalation
- Weapon↔person-track association
- Fall confirmation, immobility, and recovery
- Zone/shift/role-aware PPE compliance
- Unified emergency incidents, actions, escalation, and command center

**M6 enterprise operations** — [docs/M6-ENTERPRISE-OPERATIONS.md](docs/M6-ENTERPRISE-OPERATIONS.md):

- Priority 1: composite edge dedupe, zone validation, unified ingest pipeline
- M6.1 Celery notification workers (retry, DLQ, rate limits, delivery logs, multi-channel)
- M6.2 Evidence storage (local/MinIO/S3) with signed URLs + rich metadata
- M6.3 Recording adapters (RTSP + vendor stubs: ONVIF/Hikvision/Dahua/Milestone/Axis)
- M6.4 SSO provider config, password/session policies, MFA flags
- M6.5 Prometheus metrics + ready/live probes
- M6.6–6.8 Compose Redis/Celery/MinIO, SIEM forwards, backup/restore scripts (RPO≤15m / RTO≤1h)

**GA remaining:** vendor NVR SDKs, full IdP handshake libs per deploy, load/security testing.

## License

Proprietary — Internal use only.
