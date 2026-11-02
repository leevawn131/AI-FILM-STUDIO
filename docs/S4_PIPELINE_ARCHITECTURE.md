# AI FILM STUDIO — Sprint S4: End-to-End AI Filmmaking Pipeline Architecture

**Document Version:** 1.2.0 (Security & Consistency Hardened)  
**Status:** Architecture Blueprint & Implementation Plan  
**Target Architecture:** Autonomous AI Filmmaking Orchestration with Human Approval Gates  
**Underlying Hardened Baseline:** Sprints S1–S3 (Alembic Schema Safety, Worker Lease Fencing, Idempotent Cost Controller, Staged Upload Security)

---

## Architecture Review Corrections

This section documents the formal architectural corrections incorporated into this blueprint following the Sprint S4 architectural reviews:

### Initial Review Corrections (v1.1.0)
1. **Migration Compatibility (Alembic History & Head Revision):**
   - **Audit Finding:** An inspection of `backend/alembic/versions/` revealed seven sequential migrations already exist in the codebase:
     - `0001_initial_schema`
     - `0002_media_security_and_cost_ledger`
     - `0003_jobs_and_scene_budget`
     - `0004_job_idempotency_and_flow_attempt`
     - `0005_lease_fencing_and_upload_recovery`
     - `0006_fencing_token_and_ledger_unique`
     - `0007_upload_operation_lease_fencing` (Current HEAD)
   - **Correction:** The proposed migration identifier is corrected from the invalid `0003` to `0008_pipeline_orchestration_tables.py`. Its `down_revision` is strictly set to `'0007_upload_operation_lease_fencing'`.
2. **Explicit Waiting States & Job Lifecycle Decoupling:**
   - Short-lived worker execution (`StudioJob`: `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`) is strictly decoupled from long-lived pipeline states (`PipelineStageState`). Three explicit waiting states are introduced: `WAITING_FOR_APPROVAL`, `WAITING_FOR_EXTERNAL_MEDIA`, `WAITING_FOR_USER_INPUT`. No worker lease remains open while waiting.
3. **Stage Definitions & Checkpoint Separation:**
   - Project Setup (`STAGE_00: idea`) is distinguished from the 11 Automated/Semi-Automated Production Stages and 4 Human Approval Checkpoints. Checkpoints start in `LOCKED` status and never appear as awaiting review before outputs are ready.
4. **Worker & Session Safety (Thread Isolation):**
   - Background heartbeat routines instantiate their own dedicated `SessionLocal()` per thread/task, executing lease renewals under monotonic fencing tokens and closing the session in an explicit `finally` block.
5. **CostController Contract Verification:**
   - Fictional methods like `refund_charge()` were removed. All references align strictly with actual `CostController` methods: `authorize_and_charge`, `record_flow_credit_usage`, `check_and_deduplicate`, `compute_hash`, `compute_operation_identity`, `get_project_usage_summary`. Speculative budget charges are reversed via native `db.rollback()`.

---

### Final Security and Consistency Review (v1.2.0)

6. **Gatekeeper Ownership Validation & Exact Hash Enforcement:**
   - **Audit Finding:** Resolving `project_id` from route parameters without cross-entity validation allows subtle bypasses when both `project_id` and an entity ID (`scene_id`, `shot_id`, `character_id`, `location_id`) are supplied. Additionally, permitting a missing or NULL hash to pass, or executing mutations/commits inside a read authorization dependency, creates transaction pollution and security vulnerabilities.
   - **Correction:** 
     - The `require_checkpoint` dependency resolves all route parameters and validates entity ownership: if both `project_id` and an entity ID are present, it verifies `entity.project_id == project_id`. Cross-project mismatches consistently raise HTTP 404 or 403.
     - A missing, NULL, malformed, or uncomputed `artifact_content_hash` will **NEVER** authorize an approval. An exact string match against the canonical artifact hash is mandatory.
     - The authorization dependency is strictly read-only and will never commit transactions. Stale approval invalidation and audit logging occur exclusively within dedicated, isolated transactional service operations.

7. **Audit Log Retention & Immutability:**
   - **Audit Finding:** Proposing `ondelete="CASCADE"` on `PipelineCheckpointAudit` directly contradicted the requirement for an immutable audit history.
   - **Correction:** 
     - Foreign keys on the audit journal are configured with `ondelete="RESTRICT"`, or the audit table functions as an unconstrained, append-only ledger (`pipeline_checkpoint_audit`) storing immutable snapshots (`project_id`, `project_name_snapshot`, `checkpoint_type`, `decision`, `artifact_content_hash`, `artifact_version`, `reviewer_name`, `feedback_notes`, `created_at`).
     - Projects support soft deletion (`deleted_at`), retaining the historical audit record. Hard deletion of projects is blocked if audit records exist unless an explicit compliance purge policy is invoked.
     - The application possesses strictly `INSERT`-only capabilities on the audit log. `UPDATE` and `DELETE` operations are prohibited by application logic and database permissions.

8. **Complete Deterministic Artifact Hash Coverage:**
   - **Audit Finding:** Hashing file paths alone is insecure, and previous hash definitions lacked complete field coverage.
   - **Correction:** Deterministic canonical serialization (JSON sorted keys, `ensure_ascii=True`, UTF-8, SHA-256) is defined across all 4 checkpoints:
     - **Story Approval:** All ordered scenes (`scene_number`, `order_index`, `slugline`, `title`, `summary`, `script_content`, `estimated_duration`).
     - **Visual Approval:** All characters (`name`, `role`, `age`, `facial_characteristics`, `clothing`, `voice_profile`, `personality`, `visual_prompt_tags`, and reference image *content checksum* via `MediaAsset.checksum` or file SHA-256); all locations (`name`, `description`, `time_of_day`, `weather`, `lighting_setup`, `visual_prompt_tags`, and reference image *content checksum*).
     - **Production Approval:** All ordered shots (`shot_number`, `shot_code`, `duration_seconds`, `camera_angle`, `camera_movement`, `visual_description`, `lighting`, `character_references`, `video_prompt`, `negative_prompt`, `seed`, `order_index`) cryptographically bound to the approved `visual_artifact_hash`.
     - **Final Review:** Actual video media content checksums (`MediaAsset.checksum`), sequence clip ordering and trims, audio asset checksums (`AudioTrack.file_path` SHA-256), volume levels, ducking parameters, subtitle configuration, and preview render parameters.

9. **Approval Versioning & TOCTOU (Time-of-Check to Time-of-Use) Protection:**
   - **Audit Finding:** A race condition could allow an artifact to be modified after gatekeeper verification but before downstream stage compute executes.
   - **Correction:**
     - `artifact_version` starts at 1 on creation and increments monotonically on every edit, refinement, reordering, or regeneration.
     - Downstream stages record the exact `consumed_artifact_hash` they are processing.
     - Inside the execution transaction, the orchestrator asserts that `checkpoint.status == 'APPROVED'`, `checkpoint.artifact_content_hash == consumed_artifact_hash`, and `current_artifact_hash == consumed_artifact_hash`. If a concurrent edit modifies the artifact, the mismatch is detected and the execution is aborted immediately before any expensive generation occurs.

10. **Pipeline Stage Worker Fencing:**
    - **Audit Finding:** Stale workers whose leases expired could update `PipelineStageState` upon late completion.
    - **Correction:** Every automated stage execution references its active `StudioJob` via `active_job_id`. Updating stage completion requires a conditional atomic database update verifying `active_job_id` and the worker's current monotonic `fencing_token`. Stale workers with expired fencing tokens are rejected via `JobOwnershipLostError` and cannot corrupt stage results.

11. **Configurable Flow Credit Accounting (Unset Default):**
    - **Audit Finding:** A hardcoded default of 1.0 credit per shot was assumed.
    - **Correction:** The default estimate is now `None` (unset/unknown). The UI only displays an estimate when project-level configuration or user-provided shot parameters exist. If unconfigured, the UI clearly displays "External credit estimate: Dependent on platform settings / Unconfigured". Actual credit consumption is recorded idempotently via `cost_controller.record_flow_credit_usage()` with `generation_attempt_id` and remains preserved regardless of workflow state changes or rollbacks.

---

## Table of Contents

1. [Current-State Architecture Audit](#1-current-state-architecture-audit)
2. [Target End-to-End Pipeline Architecture](#2-target-end-to-end-pipeline-architecture)
3. [Pipeline State Machine & Human Approval Checkpoints](#3-pipeline-state-machine--human-approval-checkpoints)
4. [Database Schema Additions & Migration Plan](#4-database-schema-additions--migration-plan)
5. [Backend API & Orchestration Layer Specification](#5-backend-api--orchestration-layer-specification)
6. [Frontend Workflow & User Experience Design](#6-frontend-workflow--user-experience-design)
7. [Agent Responsibilities & Structured Communication Contracts](#7-agent-responsibilities--structured-communication-contracts)
8. [Approval & Budget Enforcement Design](#8-approval--budget-enforcement-design)
9. [Failure Recovery, Lease Fencing & Retry Strategy](#9-failure-recovery-lease-fencing--retry-strategy)
10. [Prioritized Implementation Roadmap](#10-prioritized-implementation-roadmap)

---

## 1. Current-State Architecture Audit

### 1.1 Functional Modules

The following subsystems are fully operational and verified:
1. **Screenplay Scene Generation (`backend/app/routers/screenplay.py`):**
   - Connects to `ScreenwriterAgent` and `GeminiService` (with `OfflineAIEngine` fallback).
   - Generates sluglines, dramatic summaries, and script content formatted in Fountain/screenplay style.
   - Refines scene beats based on directorial feedback.
   - Reuses `job_manager.prepare_or_claim_job()` and charges usage via `cost_controller.authorize_and_charge()`.
2. **Storyboard Shot Breakdown (`backend/app/routers/storyboard.py`):**
   - Connects to `DirectorAgent` to decompose scenes into shots.
   - Generates camera angles, movements, visual descriptions, and lighting notes.
   - Implements continuity audit via `ContinuityAgent` and `offline_ai.analyze_continuity()`.
   - Optimizes shot prompts for Google Flow via `CinematographerAgent`.
3. **Google Flow Handoff Generator (`backend/app/routers/flow_handoff.py`, `backend/app/services/flow_handoff.py`):**
   - Generates comprehensive markdown production instructions.
   - Compiles quick-copy prompt text sheets.
   - Packages prompt text and reference assets into a downloadable zip file (`validate_safe_path` secured).
   - Explicitly records actual credit usage via `cost_controller.record_flow_credit_usage()`.
4. **Media Ingestion & Auto-matching (`backend/app/routers/media.py`, `backend/app/services/media_watcher.py`, `upload_manager.py`):**
   - Ingests uploaded video files and scans watched directories.
   - Validates magic bytes, container integrity, and calculates SHA-256 checksums without bypasses.
   - Automatically correlates filenames (`SCxx_SHxx`) to corresponding `Shot` entities.
   - Atomic staged uploads with crash recovery (`recover_staged_uploads()`).
5. **Timeline Assembly & Master Export (`backend/app/routers/editor.py`, `export.py`, `timeline_editor.py`, `export_manager.py`):**
   - Sequences shots into timeline representations.
   - Normalizes aspect ratios, frame rates, and pixel formats via FFmpeg.
   - Blends background music and voiceover with sidechain ducking.
   - Exports master MP4 videos across multiple resolution presets (1080p, 4K, 720p, 9:16).
6. **Core Reliability Infrastructure (S1–S3):**
   - Database migrations via Alembic with legacy validation and pre-migration backups.
   - Worker leasing with heartbeat renewal and monotonic fencing tokens (`job_manager.py`).
   - Provider-aware cost ledger with idempotency keys and generation attempt tracking (`cost_controller.py`).

### 1.2 Partially Implemented Capabilities

1. **Character Design (`backend/app/routers/characters.py`, `backend/app/agents/character_agent.py`):**
   - Generates character sheets using static archetypes (Kaien Cross, Valeria Voss, Dr. Aris Thorne) or offline procedural random selection.
   - Characters are not dynamically extracted or synthesized from the project's screenplay or logline.
2. **Location Design (`backend/app/routers/locations.py`):**
   - Relies on hardcoded presets (`LOCATION_PRESETS`) or manual CRUD entries.
   - No automatic extraction of settings and environments from screenplay sluglines.
3. **Cinematic Prompt Decoration:**
   - `CinematographerAgent.optimize_flow_prompt()` is implemented, but must be manually triggered shot-by-shot via `/api/storyboard/{shot_id}/optimize-prompt`. It is not automatically run across all generated shots.
4. **Audio Track Generation (`backend/app/services/audio_service.py`):**
   - Generates procedural atmospheric BGM drones and modulated tone scratch voiceover beeps using FFmpeg lavfi filters.
   - Does not perform automated dialogue synthesis for character lines written in the screenplay.

### 1.3 Disconnected Stages & Gaps

1. **Absence of Pipeline State Machine:**
   - The system lacks a central concept of production phase or stage progression.
   - Projects only have metadata (`budget_total`, `budget_spent`, `target_duration_sec`). There is no persistent stage status or execution tracking.
2. **No Human Approval Checkpoints:**
   - Users can jump to any tab in `Sidebar.tsx` at any time.
   - A user can generate a storyboard before scenes exist; download a Flow handoff package before shots are reviewed; or launch a master render with empty or placeholder media.
   - None of the 4 required human approvals (Story, Visual, Production, Final) are represented in the database or enforced by the API.
3. **No Automatic Agent Chaining:**
   - Agents operate in isolation. Outputs of `ScreenwriterAgent` do not trigger `DirectorAgent`; director shots do not automatically trigger `CinematographerAgent`; media ingestion does not notify `EditorAgent`.
4. **Hardcoded UI Pipeline Display:**
   - `DashboardView.tsx` derives "Done" status simply by checking `scenesCount > 0`, `shotsCount > 0`, and `readyShots > 0`. It does not reflect actual workflow completion or human approval.

### 1.4 Implemented Code vs README Claims

| Capability | README Claim | Actual Implementation Status | Gap to Close in S4 |
| :--- | :--- | :--- | :--- |
| **End-to-End Orchestration** | "Autonomous AI filmmaking from idea to final render" | Disconnected modules requiring manual UI navigation between 10 separate screens. | Implement `PipelineOrchestrator` engine with state machine. |
| **Human Approvals** | "Director-in-the-loop checkpoints" | Zero approval models or database fields; zero API route gatekeeping. | Add `pipeline_checkpoints` entity and server-side gatekeeping. |
| **Character Generation** | "AI-generated visual character bibles" | Random selection from 3 hardcoded archetypes. | Chained extraction from screenplay dialogue and prompts. |
| **Location Generation** | "Automated location and set design" | Random selection from 3 static template dictionaries. | Automated extraction from scene sluglines. |
| **Google Flow Integration** | "Google Flow Video Generation" | Explicit manual handoff (zip/text copy) + credit tracking. | Keep as explicit handoff; add gatekeeping before credit spend. |
| **Assembly Cut** | "Automated rhythm and pace editing" | Functional EditorAgent heuristics, but must be manually invoked and not linked to project export. | Wire EditorAgent assembly directly into timeline export. |

### 1.5 Existing Database Schema & Entity Relationships

The current relational model consists of 11 tables:
`projects`, `scenes`, `shots`, `characters`, `locations`, `media_assets`, `audio_tracks`, `export_jobs`, `cost_records`, `studio_jobs`, `upload_operations`.

**Alembic Migration History:**
- `0001_initial_schema`
- `0002_media_security_and_cost_ledger`
- `0003_jobs_and_scene_budget`
- `0004_job_idempotency_and_flow_attempt`
- `0005_lease_fencing_and_upload_recovery`
- `0006_fencing_token_and_ledger_unique`
- `0007_upload_operation_lease_fencing` (**Current HEAD**)

---

## 2. Target End-to-End Pipeline Architecture

### 2.1 Architectural Vision

The target architecture transforms AI FILM STUDIO from a collection of isolated tools into an automated, stateful filmmaking production line. The user provides a high-level creative prompt (Idea). The system autonomously drives the creative and technical pipeline forward, pausing only at 4 strategic human approval checkpoints where human creative direction is essential.

```
[Idea Prompt / Project Setup]
      │
      ▼
┌────────────────────────────────────────────────────────┐
│ 1. Story Development (Scene Outlines & Beats)          │
│ 2. Screenplay Writing (Screenwriter Agent)             │
└────────────────────────────────────────────────────────┘
      │
      ▼
 [CHECKPOINT 1: STORY APPROVAL] ◄── User reviews logline, scenes & script
      │ (Approved)
      ▼
┌────────────────────────────────────────────────────────┐
│ 3. Character Design (Visual Prompts & Wardrobe Anchors)│
│ 4. Location Design (Lighting & Environmental Setups)   │
└────────────────────────────────────────────────────────┘
      │
      ▼
 [CHECKPOINT 2: VISUAL APPROVAL] ◄── User reviews cast & set visual bibles
      │ (Approved)
      ▼
┌────────────────────────────────────────────────────────┐
│ 5. Storyboard Breakdown (Director Agent)               │
│ 6. Cinematic Prompt Generation (Cinematographer Agent) │
└────────────────────────────────────────────────────────┘
      │
      ▼
 [CHECKPOINT 3: PRODUCTION APPROVAL] ◄── User reviews shots before credit spend
      │ (Approved)
      ▼
┌────────────────────────────────────────────────────────┐
│ 7. Google Flow Handoff (Package Zip & Instruction Gen) │
│ 8. Media Ingestion & Auto-Match (MediaWatcher / Upload)│
│ 9. Film Editing & Rhythm Assembly (Editor Agent)       │
│ 10. Audio Mixing & Dialogue Ducking (Audio Service)    │
└────────────────────────────────────────────────────────┘
      │
      ▼
 [CHECKPOINT 4: FINAL REVIEW] ◄── User previews assembled film in player
      │ (Approved)
      ▼
┌────────────────────────────────────────────────────────┐
│ 11. Final Rendering & Master Export (FFmpeg Presets)   │
└────────────────────────────────────────────────────────┘
      │
      ▼
 [Delivery MP4 Film Master]
```

### 2.2 Core Design Principles

1. **Routine Automation by Default:** Whenever a step does not incur unrecoverable financial expense or make fundamental creative pivots, it executes automatically.
2. **Explicit Human Gates for Irreversible Steps:** Human approvals are required at four critical junctures (Story, Visual, Production, Final).
3. **Decoupled Worker Leases from Waiting States:** Short-lived compute workers execute inside `StudioJob` records; when complete, the pipeline enters a persistent waiting state without holding worker locks or leases.
4. **Cryptographically Bound Approvals & TOCTOU Protection:** Approvals are cryptographically tied to exact artifact content hashes and monotonic versions. Execution transactions re-verify hash freshness before consuming inputs.
5. **No Fabricated External APIs:** Google Flow video generation remains an explicit, transparent handoff package with configurable credit estimation and idempotent usage tracking.
6. **Strict Concurrency & Fencing:** Monotonic fencing tokens prevent stale workers from overwriting results across all state transitions.

---

## 3. Pipeline State Machine & Human Approval Checkpoints

### 3.1 Definition of Production Stages & Human Checkpoints

The production pipeline clearly distinguishes the initial user project creation from the 11 automated/semi-automated production stages and the 4 human approval checkpoints:

#### Project Setup
- `STAGE_00: idea`: Project creation, logline, genre, target duration, and aspect ratio.

#### Production Stages (11 Stages)
1. `story_development`: Formulates dramatic structure, act beats, and scene breakdown from logline.
2. `screenplay`: Writes full dialogue and scene action in Fountain format via `ScreenwriterAgent`.
   *(Followed by Checkpoint 1: Story Approval)*
3. `character_design`: Extracts character cast and generates visual prompt tags and wardrobe anchors.
4. `location_design`: Extracts scene locations and generates atmospheric lighting setups.
   *(Followed by Checkpoint 2: Visual Approval)*
5. `storyboard`: Deconstructs approved scenes into visual camera shots via `DirectorAgent`.
6. `cinematic_prompts`: Decorates shot prompts with optical lenses, lighting, and negative tags via `CinematographerAgent`.
   *(Followed by Checkpoint 3: Production Approval)*
7. `flow_handoff`: Compiles downloadable zip package, instructions, and prompt copy sheet.
8. `media_import`: Monitors watched folders / uploads and auto-links video assets to `Shot` records.
9. `film_editing`: Builds assembly cut order, cut timings, and transitions via `EditorAgent`.
10. `audio_mixing`: Synthesizes ambient BGM, voiceover cues, and sidechain ducks audio via `audio_service`.
   *(Followed by Checkpoint 4: Final Review)*
11. `final_rendering`: Executes FFmpeg master export render to delivery MP4 format via `export_manager`.

### 3.2 The 4 Mandatory Human Approval Checkpoints

Checkpoints are modeled separately from stages. They represent formal governance gates:

| Checkpoint Type | Prerequisite Stage | Reviewed Artifact | Validation / Canonical Hash Target | Unlocks Downstream Stage |
| :--- | :--- | :--- | :--- | :--- |
| `story_approval` | `screenplay` | Scene sluglines, summaries, Fountain script content. | SHA-256 of all ordered scenes (`scene_number`, `order_index`, `slugline`, `title`, `summary`, `script_content`, `estimated_duration`). | `character_design` & `location_design` |
| `visual_approval` | `location_design` | Character prompt anchors, reference images, location setups. | SHA-256 of all characters (traits, tags, reference image content checksum) and locations (lighting, descriptions, reference image content checksum). | `storyboard` |
| `production_approval` | `cinematic_prompts` | Shot choreographies, lens profiles, prompt sheets, credit estimate. | SHA-256 of all ordered shots (`shot_code`, `camera_angle`, `camera_movement`, `visual_description`, `lighting`, `video_prompt`, `negative_prompt`, `seed`, `order_index`) cryptographically bound to approved `visual_artifact_hash`. | `flow_handoff` & external generation |
| `final_review` | `audio_mixing` | Assembled preview video, audio balance, transition cuts. | SHA-256 of timeline sequence clip checksums, trims, transitions, audio asset checksums, ducking parameters, and subtitle configuration. | `final_rendering` master render |

### 3.3 Formal State Transition Specifications

#### Pipeline Stage Lifecycle (`PipelineStageState.status`):
- `NOT_STARTED`: Preceding stage or checkpoint has not yet completed.
- `IN_PROGRESS`: Currently executing compute via an active short-lived `StudioJob`.
- `WAITING_FOR_APPROVAL`: Stage output generated; pipeline paused waiting for human review at a checkpoint.
- `WAITING_FOR_EXTERNAL_MEDIA`: Flow handoff package prepared; pipeline paused waiting for external video generation and file ingestion.
- `WAITING_FOR_USER_INPUT`: Pipeline paused waiting for required creative direction or prompt inputs.
- `COMPLETED`: Automated stage finished successfully and was verified.
- `FAILED`: Execution failed with an error; retryable.
- `STALE`: Invalidated due to upstream edits; requires re-execution.

#### Pipeline Checkpoint Lifecycle (`PipelineCheckpoint.status`):
- `LOCKED`: Prerequisite stage has not finished generating its output. Checkpoints never appear as awaiting review before outputs are ready.
- `WAITING_FOR_APPROVAL`: Prerequisite stage output is verified and ready for human review.
- `APPROVED`: Human user approved the specific artifact hash.
- `REJECTED`: Human user rejected the artifact with feedback notes.
- `STALE`: Previously approved artifact was modified upstream; approval is invalidated.

---

## 4. Database Schema Additions & Migration Plan

### 4.1 New Entity: PipelineStageState

```python
# backend/app/models/entities.py (Specification)

class PipelineStageState(Base):
    """
    Tracks execution status, outputs, and associated background job
    for each discrete stage of a film project's pipeline.
    """
    __tablename__ = "pipeline_stage_states"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    stage_name = Column(String(64), nullable=False, index=True)
    # Stage name values:
    # 'story_development', 'screenplay', 'character_design', 'location_design',
    # 'storyboard', 'cinematic_prompts', 'flow_handoff', 'media_import',
    # 'film_editing', 'audio_mixing', 'final_rendering'

    status = Column(String(32), nullable=False, default="NOT_STARTED", index=True)
    # Status values:
    # 'NOT_STARTED', 'IN_PROGRESS', 'WAITING_FOR_APPROVAL',
    # 'WAITING_FOR_EXTERNAL_MEDIA', 'WAITING_FOR_USER_INPUT',
    # 'COMPLETED', 'FAILED', 'STALE'

    active_job_id = Column(String(36), ForeignKey("studio_jobs.id", ondelete="SET NULL"), nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    content_hash = Column(String(64), nullable=True)  # SHA-256 of stage outputs
    consumed_input_hash = Column(String(64), nullable=True)  # SHA-256 of approved upstream inputs consumed
    error_message = Column(Text, nullable=True)
    stage_metadata = Column(Text, nullable=True)  # JSON dictionary of outputs

    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "stage_name", name="uq_project_stage_name"),
    )
```

### 4.2 New Entity: PipelineCheckpoint & Immutable Audit Log

```python
# backend/app/models/entities.py (Specification)

class PipelineCheckpoint(Base):
    """
    Persists current human approval status, bound to an exact artifact content hash.
    """
    __tablename__ = "pipeline_checkpoints"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    project_id = Column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    checkpoint_type = Column(String(64), nullable=False, index=True)
    # Checkpoint values: 'story_approval', 'visual_approval', 'production_approval', 'final_review'

    status = Column(String(32), nullable=False, default="LOCKED", index=True)
    # Status values: 'LOCKED', 'WAITING_FOR_APPROVAL', 'APPROVED', 'REJECTED', 'STALE'

    artifact_type = Column(String(64), nullable=False)  # 'screenplay', 'visual_bible', 'storyboard', 'assembly_cut'
    artifact_version = Column(Integer, default=1, nullable=False)
    artifact_content_hash = Column(String(64), nullable=True)  # SHA-256 bound to approval

    reviewer_name = Column(String(128), default="Director / User", nullable=False)
    feedback_notes = Column(Text, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "checkpoint_type", name="uq_project_checkpoint_type"),
    )

class PipelineCheckpointAudit(Base):
    """
    Immutable, append-only compliance journal of all human approval decisions, rejections, and invalidations.
    Audit records are retained permanently. Deleting a project soft-deletes the project;
    the audit log retains immutable snapshot columns.
    """
    __tablename__ = "pipeline_checkpoint_audit"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    project_id = Column(String(36), nullable=False, index=True)
    project_name_snapshot = Column(String(128), nullable=False)
    checkpoint_id = Column(String(36), nullable=False, index=True)
    checkpoint_type = Column(String(64), nullable=False)
    decision = Column(String(32), nullable=False)  # 'APPROVED', 'REJECTED', 'INVALIDATED'
    artifact_content_hash = Column(String(64), nullable=False)
    artifact_version = Column(Integer, nullable=False)
    reviewer_name = Column(String(128), nullable=False)
    feedback_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

### 4.3 Entity Relationships & Project Model Updates

- `Project` entity adds:
  - `stage_states = relationship("PipelineStageState", backref="project", cascade="all, delete-orphan")`
  - `checkpoints = relationship("PipelineCheckpoint", backref="project", cascade="all, delete-orphan")`
  - `deleted_at = Column(DateTime, nullable=True)`  # Soft delete to preserve audit integrity
  - `current_pipeline_stage = Column(String(64), default="story_development", nullable=False)`
  - `pipeline_overall_status = Column(String(32), default="in_progress", nullable=False)`

### 4.4 Non-Destructive Alembic Migration Plan

- **Migration ID:** `0008_pipeline_orchestration_tables.py`
- **Down Revision:** `'0007_upload_operation_lease_fencing'`
- **Operations:**
  - `op.create_table('pipeline_stage_states', ...)`
  - `op.create_table('pipeline_checkpoints', ...)`
  - `op.create_table('pipeline_checkpoint_audit', ...)`
  - `op.add_column('projects', sa.Column('deleted_at', sa.DateTime(), nullable=True))`
  - `op.add_column('projects', sa.Column('current_pipeline_stage', sa.String(64), server_default='story_development', nullable=False))`
  - `op.add_column('projects', sa.Column('pipeline_overall_status', sa.String(32), server_default='in_progress', nullable=False))`
- **Reversibility:** Fully reversible `downgrade()` dropping the created tables and columns.

---

## 5. Backend API & Orchestration Layer Specification

### 5.1 Pipeline Orchestrator Service (`PipelineOrchestrator`)

Located in `backend/app/services/pipeline_orchestrator.py`:
- `initialize_project_pipeline(db, project_id) -> None`: Seeds the 11 `PipelineStageState` rows (`NOT_STARTED`) and the 4 `PipelineCheckpoint` rows (`LOCKED`).
- `compute_artifact_hash(db, project_id, artifact_type) -> str`: Computes deterministic SHA-256 hash using canonical JSON serialization.
- `record_checkpoint_decision(db, project_id, checkpoint_type, decision, notes, reviewer) -> PipelineCheckpoint`:
  - Computes `artifact_content_hash`. If outputs are missing or hash is NULL, raises HTTP 400.
  - Updates `PipelineCheckpoint` (`APPROVED` or `REJECTED`) and records an immutable entry in `PipelineCheckpointAudit`.
  - If approved, transitions checkpoint to `APPROVED` and unlocks downstream stages.
  - If rejected, records notes and sets upstream stage to `FAILED` or `WAITING_FOR_USER_INPUT`.
- `invalidate_downstream_stages(db, project_id, changed_stage) -> None`:
  - When an artifact changes, marks dependent checkpoints as `STALE`.
  - Sets derived downstream stages to `STALE` or `NOT_STARTED` in a dedicated transaction.

### 5.2 Server-Side Gatekeeping Dependency (`require_checkpoint`)

Located in `backend/app/services/gatekeeper.py`:

```python
import json
import hashlib
from fastapi import HTTPException, Depends, Request
from sqlalchemy.orm import Session
from backend.app.database import get_db
from backend.app.models.entities import PipelineCheckpoint, Scene, Shot, Character, Location

def require_checkpoint(checkpoint_type: str):
    """
    Enforces that a route cannot execute unless the specified checkpoint is APPROVED
    and matches the exact canonical artifact content hash.
    Resolves and validates entity ownership across all supplied route parameters.
    Read-only dependency: does NOT commit unrelated mutations.
    """
    async def dependency(request: Request, db: Session = Depends(get_db)):
        path_params = request.path_params
        route_project_id = path_params.get("project_id")
        resolved_project_id = route_project_id

        # 1. Resolve and validate ownership across all present entity IDs
        if "scene_id" in path_params:
            scene = db.query(Scene.id, Scene.project_id).filter(Scene.id == path_params["scene_id"]).first()
            if not scene:
                raise HTTPException(status_code=404, detail=f"Scene '{path_params['scene_id']}' not found")
            if resolved_project_id and resolved_project_id != scene.project_id:
                raise HTTPException(status_code=403, detail="Cross-project entity reference forbidden")
            resolved_project_id = scene.project_id

        if "shot_id" in path_params:
            shot = db.query(Shot.id, Shot.project_id).filter(Shot.id == path_params["shot_id"]).first()
            if not shot:
                raise HTTPException(status_code=404, detail=f"Shot '{path_params['shot_id']}' not found")
            if resolved_project_id and resolved_project_id != shot.project_id:
                raise HTTPException(status_code=403, detail="Cross-project entity reference forbidden")
            resolved_project_id = shot.project_id

        if "character_id" in path_params:
            char = db.query(Character.id, Character.project_id).filter(Character.id == path_params["character_id"]).first()
            if not char:
                raise HTTPException(status_code=404, detail=f"Character '{path_params['character_id']}' not found")
            if resolved_project_id and resolved_project_id != char.project_id:
                raise HTTPException(status_code=403, detail="Cross-project entity reference forbidden")
            resolved_project_id = char.project_id

        if "location_id" in path_params:
            loc = db.query(Location.id, Location.project_id).filter(Location.id == path_params["location_id"]).first()
            if not loc:
                raise HTTPException(status_code=404, detail=f"Location '{path_params['location_id']}' not found")
            if resolved_project_id and resolved_project_id != loc.project_id:
                raise HTTPException(status_code=403, detail="Cross-project entity reference forbidden")
            resolved_project_id = loc.project_id

        if not resolved_project_id:
            raise HTTPException(status_code=400, detail="Cannot resolve valid project_id from request parameters")

        # 2. Query checkpoint status
        cp = db.query(PipelineCheckpoint).filter(
            PipelineCheckpoint.project_id == resolved_project_id,
            PipelineCheckpoint.checkpoint_type == checkpoint_type
        ).first()

        if not cp or cp.status != "APPROVED":
            raise HTTPException(
                status_code=403,
                detail=f"Operation blocked: Project requires an approved '{checkpoint_type}' checkpoint."
            )

        # 3. Exact artifact hash validation (NULL or missing hash must NEVER authorize)
        if not cp.artifact_content_hash or len(cp.artifact_content_hash) != 64:
            raise HTTPException(
                status_code=403,
                detail=f"Security error: Invalid or missing artifact content hash for '{checkpoint_type}'."
            )

        from backend.app.services.pipeline_orchestrator import pipeline_orchestrator
        current_hash = pipeline_orchestrator.compute_artifact_hash(db, resolved_project_id, cp.artifact_type)

        if not current_hash or cp.artifact_content_hash != current_hash:
            raise HTTPException(
                status_code=403,
                detail=f"Operation blocked: Content modified since '{checkpoint_type}' approval. Re-approval required."
            )

        return cp
    return dependency
```

### 5.3 Gatekeeper Enforcement Attachments

- `POST /api/storyboard/generate/{scene_id}` -> `Depends(require_checkpoint("story_approval"))`
- `POST /api/storyboard/{shot_id}/optimize-prompt` -> `Depends(require_checkpoint("visual_approval"))`
- `GET /api/flow-handoff/download/{project_id}` -> `Depends(require_checkpoint("production_approval"))`
- `POST /api/flow-handoff/generate/{project_id}` -> `Depends(require_checkpoint("production_approval"))`
- `POST /api/export/render/{project_id}` -> `Depends(require_checkpoint("final_review"))`

### 5.4 Authorization Boundary & Single-Tenant Scope Limitations

> [!WARNING]
> **Known Architectural Limitation — Single-Tenant Project Scoping vs. User Authorization:**
> - Pipeline endpoints currently use `project_id` scoping to isolate project state and associated background jobs, but do **not** enforce user-level project ownership or authentication.
> - The existing codebase does not include a `User` or `Account` model; reviewer names in checkpoints and audit logs are nominal snapshot strings.
> - **Cross-project query filtering is not equivalent to user authorization.**
> - When the application introduces multi-tenant/multi-user collaboration, or exposes the API beyond a trusted local/internal boundary, a dedicated authentication and authorization layer (e.g. JWT authentication, project ownership tables, and RBAC / ABAC enforcement) must be implemented.

---

## 6. Frontend Workflow & User Experience Design

### 6.1 Studio Header Stepper & Pipeline Hub

The top header in `frontend/src/components/Header.tsx` and the dashboard view in `frontend/src/views/DashboardView.tsx` will display an interactive, 11-stage progress tracker categorized by filmmaking phases:
- **Phase 1: Story Development** (`story_development` -> `screenplay` -> 🛡️ Story Approval)
- **Phase 2: Visual Development** (`character_design` -> `location_design` -> 🛡️ Visual Approval)
- **Phase 3: Cinematic Pre-Production** (`storyboard` -> `cinematic_prompts` -> 🛡️ Production Approval)
- **Phase 4: Production & Assembly** (`flow_handoff` -> `media_import` -> `film_editing` -> `audio_mixing` -> 🛡️ Final Review)
- **Phase 5: Master Delivery** (`final_rendering`)

### 6.2 Checkpoint Review Modals

When a checkpoint enters `WAITING_FOR_APPROVAL`, an approval banner is displayed:
1. **Story Approval Modal:** Displays scene sluglines, estimated duration, and screenplay Fountain text.
2. **Visual Approval Modal:** Renders character portraits and wardrobe prompt tags alongside location lighting specs.
3. **Production Approval Modal:** Renders shot choreographies, lens profiles, prompt copies, and configurable Google Flow credit estimate.
4. **Final Review Modal:** Interactive video player rendering the ducked audio and assembled timeline cut.

---

## 7. Agent Responsibilities & Structured Communication Contracts

### 7.1 Canonical Deterministic Artifact Hash Specifications

All hashes use `hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')).hexdigest()`:

1. **Screenplay Canonical Hash (`artifact_type: "screenplay"`):**
   ```python
   payload = [
       {
           "scene_number": s.scene_number,
           "order_index": s.order_index,
           "slugline": s.slugline,
           "title": s.title,
           "summary": s.summary,
           "script_content": s.script_content,
           "estimated_duration": s.estimated_duration
       }
       for s in db.query(Scene).filter(Scene.project_id == project_id).order_by(Scene.order_index).all()
   ]
   ```

2. **Visual Bible Canonical Hash (`artifact_type: "visual_bible"`):**
   ```python
   chars = [
       {
           "name": c.name,
           "role": c.role,
           "age": c.age,
           "facial_characteristics": c.facial_characteristics,
           "clothing": c.clothing,
           "voice_profile": c.voice_profile,
           "personality": c.personality,
           "visual_prompt_tags": c.visual_prompt_tags,
           "ref_image_checksum": compute_file_sha256(c.reference_image_path) if c.reference_image_path else ""
       }
       for c in db.query(Character).filter(Character.project_id == project_id).order_by(Character.name).all()
   ]
   locs = [
       {
           "name": l.name,
           "description": l.description,
           "time_of_day": l.time_of_day,
           "weather": l.weather,
           "lighting_setup": l.lighting_setup,
           "visual_prompt_tags": l.visual_prompt_tags,
           "ref_image_checksum": compute_file_sha256(l.reference_image_path) if l.reference_image_path else ""
       }
       for l in db.query(Location).filter(Location.project_id == project_id).order_by(Location.name).all()
   ]
   payload = {"characters": chars, "locations": locs}
   ```

3. **Storyboard Canonical Hash (`artifact_type: "storyboard"`):**
   ```python
   shots = [
       {
           "shot_number": s.shot_number,
           "shot_code": s.shot_code,
           "duration_seconds": s.duration_seconds,
           "camera_angle": s.camera_angle,
           "camera_movement": s.camera_movement,
           "visual_description": s.visual_description,
           "lighting": s.lighting,
           "character_references": s.character_references,
           "video_prompt": s.video_prompt,
           "negative_prompt": s.negative_prompt,
           "seed": s.seed,
           "order_index": s.order_index
       }
       for s in db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
   ]
   payload = {"shots": shots, "approved_visual_hash": approved_visual_checkpoint.artifact_content_hash}
   ```

4. **Assembly Cut Canonical Hash (`artifact_type: "assembly_cut"`):**
   ```python
   clips = [
       {
           "shot_code": s.shot_code,
           "order_index": s.order_index,
           "duration_seconds": s.duration_seconds,
           "media_asset_checksum": s.media_asset.checksum if s.media_asset else ""
       }
       for s in db.query(Shot).filter(Shot.project_id == project_id).order_by(Shot.order_index).all()
   ]
   tracks = [
       {
           "track_type": t.track_type,
           "title": t.title,
           "duration_seconds": t.duration_seconds,
           "volume_level": t.volume_level,
           "ducking_enabled": t.ducking_enabled,
           "audio_checksum": compute_file_sha256(t.file_path) if t.file_path else ""
       }
       for t in db.query(AudioTrack).filter(AudioTrack.project_id == project_id).order_by(AudioTrack.created_at).all()
   ]
   payload = {"clips": clips, "tracks": tracks}
   ```

---

## 8. Approval & Budget Enforcement Design

### 8.1 Integration with CostController

1. **Transactional Charging Strategy:**
   - Pre-authorization and cost recording occur via `cost_controller.authorize_and_charge()`.
   - Budget deduction executes as an atomic conditional SQL `UPDATE` on `Project.budget_spent`.
   - If an unhandled exception or budget violation occurs, the caller issues `db.rollback()`, ensuring speculative charges are reversed cleanly.
   - Ledger entries are immutable once committed.
2. **Configurable Flow Credit Estimation:**
   - External Google Flow credits default to `None` (unset/unknown).
   - If configured via project settings (`project.flow_credit_estimate_per_shot`), an estimate is calculated and displayed. If unset, the UI explicitly shows "External credit estimate: Dependent on platform settings / Unconfigured".
   - Actual usage is recorded idempotently via `cost_controller.record_flow_credit_usage(..., generation_attempt_id=...)`. Even if a stage rolls back or is invalidated, recorded external usage remains preserved in `cost_records` for financial auditability.

---

## 9. Failure Recovery, Lease Fencing & Retry Strategy

### 9.1 Pipeline Stage Worker Fencing

To ensure stale workers whose leases expired cannot update stage states:
```python
def complete_pipeline_stage(
    db: Session,
    project_id: str,
    stage_name: str,
    active_job_id: str,
    worker_id: str,
    fencing_token: int,
    result_hash: str
):
    """
    Atomically completes a pipeline stage verified by worker fencing token.
    """
    # 1. Complete underlying StudioJob with mandatory ownership and fencing check
    job_manager.complete_job(
        db=db,
        job_id=active_job_id,
        result={"status": "completed", "content_hash": result_hash},
        worker_id=worker_id,
        fencing_token=fencing_token
    )

    # 2. Atomic conditional stage state update
    stmt = (
        update(PipelineStageState)
        .where(PipelineStageState.project_id == project_id)
        .where(PipelineStageState.stage_name == stage_name)
        .where(PipelineStageState.active_job_id == active_job_id)
        .values(
            status="COMPLETED",
            content_hash=result_hash,
            completed_at=datetime.utcnow()
        )
    )
    res = db.execute(stmt)
    if res.rowcount == 0:
        db.rollback()
        raise JobOwnershipLostError("Pipeline stage is no longer owned by active job")

    db.commit()
```

### 9.2 Thread-Safe Heartbeat Implementation

```python
def run_worker_heartbeat(job_id: str, worker_id: str, fencing_token: int, stop_event: threading.Event):
    """
    Executes in a dedicated background thread with its own isolated database session.
    """
    while not stop_event.wait(timeout=30.0):
        db = SessionLocal()
        try:
            renewed = job_manager.renew_lease(
                db=db,
                job_id=job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                extend_seconds=300
            )
            if not renewed:
                break
        except Exception:
            break
        finally:
            db.close()
```

---

## 10. Prioritized Implementation Roadmap

The 5-milestone implementation plan is strictly maintained:

### Milestone 1: Orchestration Core & Story Approval Gate (Smallest Useful Vertical Slice)
- **Deliverables:**
  1. Alembic migration `0008_pipeline_orchestration_tables.py` creating `pipeline_stage_states`, `pipeline_checkpoints`, and `pipeline_checkpoint_audit`.
  2. `PipelineOrchestrator` service (`backend/app/services/pipeline_orchestrator.py`).
  3. Orchestration router `/api/pipeline` (`status`, `decide`, `advance`, `retry`).
  4. Server-side `require_checkpoint("story_approval")` gatekeeper supporting cross-entity parameter resolution and exact hash validation.
  5. Frontend Pipeline Stepper in `Header.tsx` and `DashboardView.tsx`.
  6. Frontend `StoryApprovalModal` in `ScreenplayView.tsx`.
- **Verification:** Unit and integration tests in `test_pipeline_orchestrator.py` and `test_story_approval_gate.py`.

### Milestone 2: Visual Development & Visual Gate
- Automated character and location extraction from screenplay scenes.
- `VisualApprovalModal` and `require_checkpoint("visual_approval")` gatekeeper.

### Milestone 3: Pre-Production & Production Gate
- Automated Director -> Cinematographer shot breakdown chain.
- Configurable Flow credit estimation and `production_approval` gatekeeper.

### Milestone 4: Assembly, Audio & Final Review Gate
- Automated EditorAgent timeline assembly upon media import.
- Multi-track BGM and voiceover ducking mix.
- `FinalReviewModal` and `require_checkpoint("final_review")` gatekeeper.

### Milestone 5: End-to-End Hardening & Concurrency
- Concurrency verification under PostgreSQL.
- Stale approval invalidation testing and crash recovery integration tests.
