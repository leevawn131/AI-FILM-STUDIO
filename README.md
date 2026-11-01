# AI Film Studio - Hybrid Edition

A complete local-first, AI-powered filmmaking application engineered with **Google Gemini**, **Google Flow Handoff**, and **Local FFmpeg Processing**.

Designed to automate the entire cinematic filmmaking workflow—from scriptwriting and character design to storyboard prompt formulation, watched-folder ingestion, timeline montage editing, audio ducking, and master video export—while minimizing external API expenses and remaining 100% functional without paid keys.

---

## 🏛 Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │   AI Film Studio - Hybrid Edition      │
                      └──────────────────┬─────────────────────┘
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                ▼                                ▼
┌───────────────┐              ┌──────────────────┐              ┌──────────────────┐
│ GOOGLE GEMINI │              │   GOOGLE FLOW    │              │ LOCAL PROCESSING │
│ Interactive   │              │ Handoff Module   │              │ FFmpeg Editing & │
│ Screenplay &  │              │ Prompt Sheets &  │              │ Video Rendering, │
│ Film Beats    │              │ Character Packs  │              │ Audio Ducking    │
└───────┬───────┘              └────────┬─────────┘              └────────┬─────────┘
        │ Fallback                      │ Download                        │
        ▼                               ▼                                 ▼
┌───────────────┐              ┌──────────────────┐              ┌──────────────────┐
│  OFFLINE AI   │              │  MEDIA WATCHER   │              │   EXPORT & NLE   │
│ STUDIO ENGINE │              │ Auto-Ingest &    │              │ Multi-Track Cut, │
│ Zero-Cost Mode│              │ Shot Matcher     │              │ Master MP4s      │
└───────────────┘              └──────────────────┘              └──────────────────┘
```

### Key Pillars
1. **Google Gemini & Offline Hybrid Intelligence**:
   - Uses Gemini for interactive screenplay development, director beat breakdown, and dialogue polish when an API key is provided.
   - Built-in **Offline Studio Engine** enables full end-to-end operation at **zero API expense** if no key is configured or when offline.
2. **Google Flow Handoff**:
   - Adheres strictly to documented workflows without attempting undocumented API access or bypassing limits.
   - Generates engineered video prompts, negative prompts, camera movement specs, and character consistency sheets.
   - Generates downloadable production packages (`.zip` with Markdown, JSON, and quick-copy text).
3. **Local Media Watcher & Auto-Import**:
   - Continuously monitors a designated local folder (`data/watched_import`) for downloaded Google Flow videos.
   - Validates stream integrity using `ffprobe`.
   - Pattern-matches filenames (e.g. `SC01_SH01.mp4`) directly to storyboard slots.
   - Extracts crisp video frame thumbnails.
4. **Local FFmpeg Processing Engine**:
   - Handles all video stitching, timeline concatenation, resolution scaling, framerate normalization, sidechain audio ducking, and subtitle burn-in locally.
5. **Cost Controller & Budget Hub**:
   - Per-scene and per-project generation budget limits.
   - Cryptographic SHA-256 duplicate job prevention to eliminate redundant API or rendering charges.

---

## 🤖 The 6 Core Filmmaking Agents

| Agent | Responsibility |
|---|---|
| **1. Screenwriter Agent** | Interactive screenplay authoring, sluglines, character dialogue tuning, and Fountain formatting. |
| **2. Director Agent** | Dramatic beat analysis, pacing evaluation, camera blocking, and scene-to-shot breakdown. |
| **3. Character Designer Agent** | Formulates master facial traits, clothing anchors, voice profiles, and Google Flow consistency tokens. |
| **4. Cinematographer Agent** | Selects anamorphic lenses, camera motion (dolly, crane, pan), lighting profiles, and negative prompts. |
| **5. Continuity Supervisor Agent** | Audits shot sequences for wardrobe drift, time-of-day jumps, lighting mismatches, and outputs continuity score. |
| **6. Film Editor Agent** | Determines assembly cut order, rhythm, transition cuts (hard cut, dissolve, fade), and audio ducking levels. |

---

## 📦 Core Studio Modules

- **Project Manager**: Production setup, aspect ratios (`2.39:1 Anamorphic`, `16:9 Cinema`, `9:16 Vertical`), FPS settings, and budget caps.
- **AI Screenwriter**: Dual-mode screenplay studio (Rich Fountain formatted view & raw editor) with AI beat enhancement.
- **Character Database**: Consistency gallery locking character face, hair, clothing, voice, and visual tags with reference image uploads.
- **Location Database**: Set manager specifying atmosphere, weather, time-of-day, and lighting profiles.
- **Storyboard Generator**: Shot cards with camera angles, movements, durations, and Google Flow prompts with 1-click clipboard copy.
- **Flow Handoff Generator**: Exportable prompt sheets, character packets, and downloadable ZIP archives.
- **Media Import Manager**: Ingestion monitor with FFprobe validation, SHA-256 duplicate detection, auto-matching to shots, and local test clip synthesizer.
- **Audio Manager**: Multi-track mixer with procedural atmospheric synth BGM synthesis, scratch radio-comm voiceover generation, and automatic sidechain ducking (-14 LUFS target).
- **Automatic Film Editor**: Multi-track timeline (Video, Dialogue/Subtitles, BGM), clip inspector, transport scrubber, and FFmpeg preview compilation.
- **Export Center**: Master export console with presets (1080p Cinema Master, 4K Master, 9:16 Mobile, 720p Fast), progress tracking, and MP4 downloads.
- **Cost Controller & Usage Ledger**: Provider-aware accounting ledger distinguishing Google Flow subscription credits, Gemini token metrics, and local free FFmpeg processing, with budget caps and idempotent caching.
- **Database & System Manager**: Alembic schema migrations, atomic SQLite online backups (`/api/system/backup`), and configurable demo data seeding.

---

## 🛡️ Production Hardening & System Stability

### 1. Database Integrity (Sprint S1)
- **Alembic Schema Migrations**: Migration versioning (`backend/alembic/versions`) with SQLite batch alteration (`render_as_batch=True`).
- **Zero Data Loss Upgrades**: Programmatic migration runner (`run_migrations`) automatically detects unversioned legacy databases, stamps revision `0001_initial_schema`, and upgrades to head without dropping tables or losing project data.
- **Configurable Demo Seeding**: `SEED_DEMO_DATA` defaults to `false` in production. Demo data is never injected unexpectedly into production databases and can be manually seeded on demand (`POST /api/system/seed-demo`).
- **Atomic Online Backups**: SQLite online backup API (`backup_database`) creates consistent database snapshots in `backend/data/backups/studio_backup_<timestamp>.db` without database locks or write interruption (`POST /api/system/backup`).

### 2. Storage & Media Security (Sprint S2)
- **Path Traversal Protection**: All file path access, downloads, uploads, and deletions pass through strict boundary validation (`validate_safe_path`) asserting paths are strictly within the storage root (`settings.data_dir`).
- **Filename Sanitization & Unique IDs**: Untrusted upload filenames are sanitized against path separators and special characters, and stored using collision-resistant unique tokens (`generate_secure_filename`).
- **Media Header & Magic Byte Validation**: Protects against extension spoofing by inspecting binary magic signatures (PNG, JPEG, WebP, MP4/QuickTime, Matroska/WebM, MP3, WAV, OGG) combined with FFprobe stream inspection.
- **Configurable File Size Limits**: Streaming uploads enforce maximum size limits (`MAX_UPLOAD_SIZE_MB`, `MAX_IMAGE_UPLOAD_SIZE_MB`) in 64KB chunks, terminating with HTTP 413 if exceeded.
- **SHA-256 Checksums & Duplicate Import Prevention**: Checksums are computed during streaming and indexed in `media_assets.checksum`. Duplicate video uploads or imports into the same project are blocked with HTTP 409.
- **Atomic Writes & Safe Failure Cleanup**: Uploads are written to `.tmp` files in `backend/data/temp` and atomically moved upon validation; any validation or extraction failure cleans up temporary files immediately.

### 3. Provider-Aware Cost Accounting (Sprint S3)
- **Separation of Credits vs Currency**: Google Flow subscription credits (`flow_credits`) are recorded independently from monetary currency costs (`actual_cost`, `estimated_cost`, `currency`).
- **Transparent Ledger**: Replaces fake constant vendor price lists with honest ledger entries (`provider`, `model`, `operation`, `input_tokens`, `output_tokens`, `actual_cost_known`).
- **Support for Unknown Costs**: When exact vendor pricing is unknown, `actual_cost=None` and `actual_cost_known=False`.
- **Budget Limit Enforcement**: Enforces project-level (`budget_total`) and per-scene (`budget_limit`) caps atomically.
- **Idempotency & Concurrency Safety**: Cryptographic SHA-256 payload hashing ensures identical workflow requests are returned from the ledger without duplicate deductions. Thread re-entrant locking (`threading.RLock`) prevents race conditions under concurrent requests.
- **CORS Hardening**: Unrestricted wildcard CORS is replaced with explicit trusted origin whitelisting (`CORS_ORIGINS`).

---

## 🚀 Quickstart Guide

### 1. Requirements
- Linux / macOS / Windows (WSL2 recommended)
- **Python 3.11+**
- **Node.js 18+** & `npm`
- **FFmpeg 5.0+** & `ffprobe` (installed and in PATH)

### 2. Installation

```bash
# Clone or enter directory
cd /home/leevawn/Phim

# Setup Python Virtual Environment
python3 -m venv backend/venv
backend/venv/bin/pip install --upgrade pip
backend/venv/bin/pip install -r backend/requirements.txt   # or see dependencies below

# Setup React Frontend
cd frontend
npm install
cd ..
```

### 3. Running the Studio

Simply execute the unified startup script:
```bash
./start_studio.sh
```

Or run services independently:

**Backend (FastAPI):**
```bash
export PYTHONPATH=.
backend/venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Frontend (Vite + React):**
```bash
cd frontend
npm run dev
```

Visit **http://localhost:5173** in your browser.

---

## 🧪 Testing

The studio features an automated test suite covering all 6 phases and core stabilization requirements:

```bash
PYTHONPATH=. backend/venv/bin/pytest backend/tests/ -v
```

Tests verify:
- `test_database_integrity.py`: Alembic migration lifecycle, strict legacy schema compatibility verification, automatic pre-migration backups, unknown/partial DB rejection, and configurable demo seeding.
- `test_cost_controller.py`: Provider-aware ledger, separation of monetary costs from Flow credits, unknown costs support, idempotent hash deduplication, and atomic conditional budget limits.
- `test_media_security.py`: Filename sanitization, secure unique filename generation, streaming size validation, path traversal prevention, magic-byte header inspection, duplicate asset rejection (409 Conflict), and temp file cleanup.
- `test_integration_sprints.py`: 10 comprehensive cross-cutting scenarios covering fresh database initialization, legacy data preservation, crash/interrupted migration safety, duplicate job submission, job retryability, concurrent multi-threaded budget updates, upload traversal defense, audio/image header validation, and application restart state persistence.
- `test_phase1.py` - `test_phase6.py`: Full end-to-end filmmaking workflow tests (screenplay, storyboard, characters, locations, Google Flow handoff, FFmpeg assembly cut, procedural audio, and MP4 master rendering).

---

## 🔒 Enterprise Stabilization & Architecture Hardening

### 1. Legacy Database Migration Safety
- **Strict Compatibility Validation**: Verifies all 9 legacy tables and required columns before stamping unversioned databases to `0001_initial_schema`. Unversioned databases with incomplete or unknown schemas raise `IncompatibleLegacySchemaError` to prevent corruption.
- **Automatic Pre-Migration Backups**: Unversioned legacy databases are automatically backed up to `backend/data/backups/studio_backup_*.db` using SQLite's online backup API before running migrations.
- **Schema Management**: Managed via Alembic (`0001_initial_schema`, `0002_media_security_and_cost_ledger`, `0003_jobs_and_scene_budget`).

### 2. Persistent Idempotent Job Lifecycle
- **Execution States**: Dedicated `StudioJob` entities separate execution lifecycle (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`) from financial ledger records (`CostRecord`).
- **Result Reuse & Concurrency**: Completed requests reuse cached outputs without re-running or charging. In-progress jobs return HTTP 409 Conflict to prevent concurrent duplicate execution.
- **Retryability & Error Sanitization**: Failed jobs transition cleanly and can be retried. Sensitive provider credentials (API keys, authorization tokens, system paths) are redacted before storage or exposure to users.
- **Crash Reconciliation**: Unfinished jobs from unexpected server shutdowns are reconciled on startup into retryable `FAILED` states.

### 3. Database-Level Atomic Budget Enforcement
- Conditional SQL updates (`UPDATE ... WHERE budget_spent + charge <= budget_total`) eliminate race conditions across concurrent requests and threads.
- Automatic transactional rollbacks guarantee neither project nor scene budget limits can ever be exceeded.

### 4. Unified Secure Upload Pipeline
- Shared streaming upload validation (`save_upload_file_securely`) across all media, audio, character, and location endpoints.
- Verifies true binary magic-byte signatures (JPEG, PNG, WebP, MP4, MOV, MKV, AVI, WAV, MP3, OGG, FLAC, AAC, M4A) to defeat browser MIME-type spoofing.
- Writes to temporary `.tmp` files in `settings.temp_dir`, calculates SHA-256 checksums in-stream, prevents duplicate imports with HTTP 409, and ensures atomic moves and temp cleanup on failure.

### 5. Google Flow Credit Accounting
- **Local Preparation vs Platform Generation**: Local prompt pack generation and opening Google Flow are accounted at 0 Flow credits.
- **Shot-Level Tracking**: Actual Google Flow credits consumed are recorded explicitly via `POST /api/flow-handoff/record-usage/{project_id}`, capturing generation attempts and regeneration tracking.

---

## 🎬 Filmmaking Workflow Walkthrough

1. **Create Project**: Open Dashboard and click `+ New Film`. Choose genre (e.g. *Cyberpunk*), aspect ratio (`2.39:1`), and budget limit.
2. **Write Screenplay**: Navigate to **Screenplay Studio**, prompt the *Screenwriter Agent*, or refine dialogue.
3. **Lock Characters & Sets**: Use **Character Database** and **Location Manager** to define facial traits and set lighting anchors.
4. **Break Down Storyboard**: In **Storyboard & Shots**, select the scene and click `Director Agent Breakdown`. Run `Continuity Audit` to check consistency.
5. **Export Google Flow Package**: Go to **Google Flow Handoff** and download the ZIP package or copy prompts directly into Google Flow.
6. **Ingest Media**: Drop downloaded MP4s (named e.g. `SC01_SH01.mp4`) into `backend/data/watched_import` or upload via **Media Library**. They are instantly validated and linked to shots. (You can also click *Generate Local Test Clip* to test immediately without external downloads).
7. **Assemble Timeline**: Open **Film Timeline & Cut**, click `AI Assembly Cut`, inspect camera blocks, and click `Render Timeline MP4`.
8. **Add Music & Voiceover**: Use **Audio & Voiceover** to synthesize local ambient BGM and scratch voiceover tracks.
9. **Master Export**: Go to **Export Center**, choose a preset (e.g., `1080p Cinematic Master`), and render your final film!
