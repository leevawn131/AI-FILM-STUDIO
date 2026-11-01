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
- **Media Import Manager**: Ingestion monitor with FFprobe validation, auto-matching to shots, and local test clip synthesizer.
- **Audio Manager**: Multi-track mixer with procedural atmospheric synth BGM synthesis, scratch radio-comm voiceover generation, and automatic sidechain ducking (-14 LUFS target).
- **Automatic Film Editor**: Multi-track timeline (Video, Dialogue/Subtitles, BGM), clip inspector, transport scrubber, and FFmpeg preview compilation.
- **Export Center**: Master export console with presets (1080p Cinema Master, 4K Master, 9:16 Mobile, 720p Fast), progress tracking, and MP4 downloads.
- **Cost Controller**: Transparent credit ledger, per-scene budget limiter, and SHA-256 duplicate job cache.

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

The studio features an automated test suite covering all 6 phases:

```bash
PYTHONPATH=. backend/venv/bin/pytest backend/tests/ -v
```

Tests verify:
- `test_phase1.py`: Project management, SQLite initialization, dashboard metrics, cost controller.
- `test_phase2.py`: Screenwriter scene creation, Director Agent breakdown, Cinematographer prompt optimization, Continuity Supervisor audit.
- `test_phase3.py`: Character Designer Agent profiles, reference image upload, Location set atmospheric anchors.
- `test_phase4.py`: Google Flow handoff package generation, ZIP download, Watched folder scanner, FFprobe video validation, and auto-matching.
- `test_phase5.py`: Film Editor Agent assembly cut, timeline normalization, FFmpeg sequence stitching.
- `test_phase6.py`: Procedural BGM synthesis, scratch voiceover generation, multi-track audio ducking, and master MP4 export.

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
