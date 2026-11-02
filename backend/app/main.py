import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from backend.app.config import settings
from backend.app.database import init_db, SessionLocal
from backend.app.models.entities import Project, Character, Location, Scene, Shot
from backend.app.services.pipeline_orchestrator import initialize_project_pipeline
from backend.app.routers import projects, dashboard, cost, screenplay, storyboard, characters, locations, flow_handoff, media, editor, audio, export, system, jobs, pipeline

def seed_initial_demo_if_empty(force: bool = False):
    if not settings.seed_demo_data and not force:
        return

    db = SessionLocal()
    try:
        count = db.query(Project).count()
        if count == 0:
            demo = Project(
                name="Cyber Horizon: Echoes of 2088",
                logline="A rogue neural architect infiltrates a submerged data spire to recover the fragmented consciousness of humanity's first artificial sentience.",
                genre="Cyberpunk",
                aspect_ratio="2.39:1",
                fps=24,
                target_duration_sec=90,
                visual_style="Cinematic 35mm anamorphic lens, high-contrast neon chiaroscuro, volumetric rain, Tokyo neo-noir color grading",
                budget_total=150.0,
                budget_spent=0.0
            )
            db.add(demo)
            db.flush()

            # Seed character
            char = Character(
                project_id=demo.id,
                name="REN SHINODA",
                role="Protagonist",
                age="34",
                facial_characteristics="Sharp angular jawline, cybernetic titanium ocular graft over right eye, faint cheek scar",
                clothing="Distressed dark carbon-fiber trench coat with glowing amber collar piping, weathered combat boots",
                voice_profile="Deep resonant baritone, quiet intensity, calm precision",
                personality="Methodical, haunted by past neural incursions, intensely loyal to forgotten codes",
                visual_prompt_tags="34yo Japanese cybernetic operative, glowing amber eye implant, black carbon trenchcoat, rain-soaked hair"
            )
            db.add(char)

            # Seed location
            loc = Location(
                project_id=demo.id,
                name="Spire 9 Submerged Data Core",
                description="Flooded subterranean server chamber illuminated by vertical columns of phosphorescent fluid cooling pipes.",
                time_of_day="Midnight",
                weather="Submerged interior humidity, water droplets falling from conduits",
                lighting_setup="Cyan coolant tubes casting eerie blue ripples across reflective black steel floor",
                visual_prompt_tags="Submerged underwater server room, cyan neon glow, water ripple caustic reflections on brushed metal walls"
            )
            db.add(loc)
            db.flush()

            # Seed scene
            scene = Scene(
                project_id=demo.id,
                scene_number=1,
                slugline="INT. SPIRE 9 SUBMERGED DATA CORE - MIDNIGHT",
                title="Scene 1: The Breach",
                summary="Ren Shinoda breaches the flooded core, wading through calf-deep coolant to reach the primary terminal.",
                script_content="""INT. SPIRE 9 SUBMERGED DATA CORE - MIDNIGHT

Submerged hum of high-voltage transformers. Knee-deep black water ripples with caustic cyan light.

REN SHINODA (34, cybernetic ocular implant humming softly) wades forward. His long trenchcoat drags through the water.

He pauses before the Monolith Console. Frost coats the keys.

REN
(into comm link)
I'm at the primary node. Sentience integrity is down to seven percent.

COMM VOICE (V.O.)
Then don't waste time, Ren. Once the cooling cycle purges, you'll be trapped beneath forty fathoms of dead ocean.

Ren drives a neural link cable directly into the console slot.
A shockwave of electrical sparks erupts across the water's surface!

FADE TO BLACK.
""",
                location_id=loc.id,
                estimated_duration=18,
                budget_limit=25.0,
                order_index=1
            )
            db.add(scene)
            db.flush()

            # Seed shot 1 & 2
            sh1 = Shot(
                scene_id=scene.id,
                project_id=demo.id,
                shot_number=1,
                shot_code="SC01_SH01",
                duration_seconds=5.0,
                camera_angle="Wide Establishing Low Angle",
                camera_movement="Slow Smooth Dolly In",
                visual_description="Flooded subterranean data chamber, rippling dark water reflecting neon cyan conduits, silhouette of Ren walking forward in distance.",
                lighting="Volumetric cyan rim lighting with deep shadows",
                character_references='["REN SHINODA"]',
                video_prompt="Wide establishing shot, slow smooth dolly in. Flooded cyberpunk subterranean server core, knee-deep reflective black water. Ren Shinoda in weathered black carbon coat walking toward a glowing central monolith. 35mm anamorphic cinema lens, cyan caustic reflections, 8k resolution, photoreal.",
                negative_prompt="blurry, low quality, 3d render, watermark, extra limbs",
                generation_status="ready_for_flow",
                order_index=1
            )
            sh2 = Shot(
                scene_id=scene.id,
                project_id=demo.id,
                shot_number=2,
                shot_code="SC01_SH02",
                duration_seconds=4.0,
                camera_angle="Close-Up Reaction",
                camera_movement="Static locked off with subtle handheld vibration",
                visual_description="Close-up of Ren Shinoda's face. Cybernetic amber eye glowing, intense focus, rain/coolant mist glistening on cheek.",
                lighting="Warm amber eye light contrasting with cool ambient cyan background",
                character_references='["REN SHINODA"]',
                video_prompt="Cinematic close-up of Ren Shinoda's face. 34 year old male, glowing amber cybernetic ocular implant, focused expression, condensation on skin, shallow depth of field f/1.4, rich anamorphic bokeh, cinematic color grade.",
                negative_prompt="blurry, distorted face, low quality, cartoon, watermark",
                generation_status="pending",
                order_index=2
            )
            db.add_all([sh1, sh2])
            db.flush()

            # Atomically initialize pipeline stages and checkpoints within the same transaction
            initialize_project_pipeline(demo.id, db, auto_commit=False)
            db.commit()
            db.refresh(demo)

            # Scaffold folders only after successful atomic commit
            p_dir = settings.projects_dir / demo.id
            for sub in ["flow_packages", "imported_videos", "renders", "audio", "references"]:
                (p_dir / sub).mkdir(parents=True, exist_ok=True)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

from backend.app.services.job_manager import job_manager
from backend.app.services.upload_manager import upload_manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables via migrations and scaffold folders
    init_db()
    
    # Reconcile any interrupted background jobs and recover staged uploads from prior runs
    db = SessionLocal()
    try:
        recovered = job_manager.reconcile_interrupted_jobs(db)
        upload_stats = upload_manager.recover_staged_uploads(db)
    finally:
        db.close()

    seed_initial_demo_if_empty()
    yield

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    lifespan=lifespan
)

# CORS configuration with explicit trusted origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static media directory for assets, videos, previews
app.mount("/media", StaticFiles(directory=str(settings.data_dir)), name="media")

# Include Core Routers
app.include_router(projects.router)
app.include_router(dashboard.router)
app.include_router(cost.router)
app.include_router(screenplay.router)
app.include_router(storyboard.router)
app.include_router(characters.router)
app.include_router(locations.router)
app.include_router(flow_handoff.router)
app.include_router(media.router)
app.include_router(editor.router)
app.include_router(audio.router)
app.include_router(export.router)
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(pipeline.router)

@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.version,
        "ffmpeg_available": bool(settings.ffmpeg_path),
        "data_dir": str(settings.data_dir)
    }
