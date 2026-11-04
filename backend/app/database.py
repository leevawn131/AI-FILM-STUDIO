import os
import sqlite3
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Set, Tuple
from sqlalchemy import create_engine, inspect, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker
from alembic.config import Config
from alembic import command

from backend.app.config import settings

logger = logging.getLogger("database")

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """
    Enforces SQLite foreign-key constraints on every SQLite connection in production and tests.
    Avoids applying SQLite-specific PRAGMA to other database engines like PostgreSQL.
    """
    if type(dbapi_connection).__module__ == "sqlite3":
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

class MigrationError(Exception):
    """Base exception for database migration errors."""
    pass

class IncompatibleLegacySchemaError(MigrationError):
    """Raised when an unversioned database does not match the required legacy baseline schema."""
    pass

# Expected columns for all 9 tables in the legacy baseline (0001_initial_schema)
REQUIRED_LEGACY_SCHEMA: Dict[str, Set[str]] = {
    "projects": {
        "id", "name", "logline", "genre", "aspect_ratio", "fps",
        "target_duration_sec", "visual_style", "budget_total", "budget_spent",
        "created_at", "updated_at"
    },
    "characters": {
        "id", "project_id", "name", "role", "age", "facial_characteristics",
        "clothing", "voice_profile", "personality", "visual_prompt_tags",
        "reference_image_path", "created_at"
    },
    "locations": {
        "id", "project_id", "name", "description", "time_of_day", "weather",
        "lighting_setup", "visual_prompt_tags", "reference_image_path", "created_at"
    },
    "scenes": {
        "id", "project_id", "scene_number", "slugline", "title", "summary",
        "script_content", "location_id", "estimated_duration", "budget_limit",
        "order_index", "created_at"
    },
    "media_assets": {
        "id", "project_id", "filename", "file_path", "media_type", "width",
        "height", "duration_seconds", "fps", "codec", "file_size_bytes",
        "thumbnail_path", "auto_matched_shot_id", "is_validated",
        "validation_notes", "created_at"
    },
    "shots": {
        "id", "scene_id", "project_id", "shot_number", "shot_code",
        "duration_seconds", "camera_angle", "camera_movement", "visual_description",
        "lighting", "character_references", "video_prompt", "negative_prompt",
        "seed", "generation_status", "video_asset_id", "thumbnail_path",
        "order_index", "created_at"
    },
    "audio_tracks": {
        "id", "project_id", "track_type", "title", "file_path", "duration_seconds",
        "start_time_seconds", "volume_level", "ducking_enabled", "ducking_level",
        "scene_id", "shot_id", "created_at"
    },
    "cost_records": {
        "id", "project_id", "scene_id", "action_type", "credits_used", "cost_usd",
        "prompt_hash", "details", "created_at"
    },
    "export_jobs": {
        "id", "project_id", "preset_name", "resolution", "aspect_ratio", "fps",
        "include_subtitles", "status", "progress_pct", "output_path",
        "file_size_bytes", "error_message", "created_at", "completed_at"
    },
}

# SQLite configuration with check_same_thread=False for multithreaded background workers
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=settings.debug
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def backup_database(db_path: Optional[Path] = None, backup_dir: Optional[Path] = None) -> Path:
    """
    Creates an atomic online backup of the SQLite database.
    Uses sqlite3 online backup API to ensure snapshot consistency even during concurrent writes.
    """
    target_db = Path(db_path) if db_path else settings.db_path
    target_backup_dir = Path(backup_dir) if backup_dir else settings.backup_dir
    target_backup_dir.mkdir(parents=True, exist_ok=True)

    if not target_db.exists():
        raise FileNotFoundError(f"Database file does not exist at {target_db}")

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup_file = target_backup_dir / f"studio_backup_{timestamp}.db"

    source_conn = sqlite3.connect(str(target_db))
    try:
        dest_conn = sqlite3.connect(str(backup_file))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    return backup_file

def verify_database_integrity(db_path: Path) -> bool:
    """
    Verifies that an SQLite file exists and passes PRAGMA integrity_check.
    Returns True if healthy, False otherwise.
    """
    p = Path(db_path)
    if not p.exists() or p.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(str(p))
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            res = cur.fetchone()
            return bool(res and res[0] == "ok")
        finally:
            conn.close()
    except Exception:
        return False

def validate_legacy_schema(inspector) -> Tuple[bool, str]:
    """
    Validates whether an unversioned database matches the exact legacy baseline schema (0001).
    Returns (True, "valid_legacy") if all 9 tables exist with all required columns.
    Returns (False, reason) otherwise.
    """
    tables = set(inspector.get_table_names())
    if not tables:
        return False, "empty"
    if "alembic_version" in tables:
        return False, "alembic_managed"

    required_tables = set(REQUIRED_LEGACY_SCHEMA.keys())
    missing_tables = required_tables - tables
    if missing_tables:
        return False, f"Missing required legacy tables: {sorted(list(missing_tables))}"

    for table_name, expected_cols in REQUIRED_LEGACY_SCHEMA.items():
        actual_cols = {col["name"] for col in inspector.get_columns(table_name)}
        missing_cols = expected_cols - actual_cols
        if missing_cols:
            return False, f"Table '{table_name}' is missing expected legacy columns: {sorted(list(missing_cols))}"

    # Verify primary keys on all tables
    for table_name in REQUIRED_LEGACY_SCHEMA.keys():
        pk = inspector.get_pk_constraint(table_name)
        pk_cols = pk.get("constrained_columns", []) if pk else []
        if not pk_cols or "id" not in pk_cols:
            return False, f"Table '{table_name}' is missing expected primary key constraint on 'id'"

    # Verify relevant foreign keys on child tables
    child_tables = ["characters", "locations", "scenes", "media_assets", "shots", "audio_tracks", "cost_records", "export_jobs"]
    for table_name in child_tables:
        fks = inspector.get_foreign_keys(table_name)
        has_proj_fk = any(fk.get("referred_table") == "projects" and "project_id" in fk.get("constrained_columns", []) for fk in fks)
        if not has_proj_fk:
            return False, f"Table '{table_name}' is missing required foreign key constraint referencing 'projects.id'"

    shot_fks = inspector.get_foreign_keys("shots")
    has_scene_fk = any(fk.get("referred_table") == "scenes" and "scene_id" in fk.get("constrained_columns", []) for fk in shot_fks)
    if not has_scene_fk:
        return False, "Table 'shots' is missing required foreign key constraint referencing 'scenes.id'"

    return True, "valid_legacy"

def run_migrations(target_engine=None, db_url: Optional[str] = None) -> None:
    """
    Safely applies Alembic migrations up to head.
    - Validates legacy schema before stamping 0001.
    - Creates an automatic pre-migration backup of valid legacy databases.
    - Never stamps unknown or partially initialized databases (raises IncompatibleLegacySchemaError).
    - Preserves existing user data.
    """
    import backend.app.models.entities  # ensure all models are registered

    if isinstance(target_engine, str):
        url = target_engine
        active_engine = create_engine(url)
        owns_engine = True
    else:
        active_engine = target_engine or engine
        url = db_url or settings.database_url
        owns_engine = False

    settings.data_dir.mkdir(parents=True, exist_ok=True)

    alembic_ini_path = settings.root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.attributes["configure_logger"] = False

    # Extract SQLite file path if applicable for pre-migration backup
    db_file_path: Optional[Path] = None
    if url.startswith("sqlite:///"):
        raw_path = url[len("sqlite:///"):]
        if raw_path and raw_path != ":memory:":
            p = Path(raw_path)
            db_file_path = p if p.is_absolute() else (settings.root_dir / p).resolve()

    with active_engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        is_alembic_managed = "alembic_version" in tables
        is_empty = len(tables) == 0

        if not is_empty and not is_alembic_managed:
            # Unversioned database: strict compatibility validation
            is_valid_legacy, validation_reason = validate_legacy_schema(inspector)
            if not is_valid_legacy:
                raise IncompatibleLegacySchemaError(
                    f"Cannot safely migrate legacy database: {validation_reason}. "
                    "The database schema is unknown or partially initialized. "
                    "Automatic stamping was aborted to protect existing data integrity."
                )

            # Create an automatic pre-migration backup before modifying any legacy database
            if db_file_path and db_file_path.exists():
                try:
                    backup_file = backup_database(db_path=db_file_path)
                    if not backup_file.exists() or backup_file.stat().st_size == 0:
                        raise MigrationError(f"Pre-migration backup file is missing or empty at {backup_file}")
                    
                    # Validate that the backup file can be reopened and queried before continuing
                    bk_conn = sqlite3.connect(str(backup_file))
                    try:
                        cur = bk_conn.cursor()
                        cur.execute("PRAGMA integrity_check")
                        res = cur.fetchone()
                        if not res or res[0] != "ok":
                            raise MigrationError(f"Backup verification failed: integrity check returned {res}")
                        cur.execute("SELECT count(*) FROM sqlite_master")
                    finally:
                        bk_conn.close()
                except Exception as b_err:
                    if isinstance(b_err, MigrationError):
                        raise
                    raise MigrationError(
                        f"Pre-migration backup failed: {b_err}. Migration aborted to protect legacy data."
                    ) from b_err

            # Stamping valid legacy database to 0001_initial_schema
            with active_engine.connect() as stamp_conn:
                alembic_cfg.attributes["connection"] = stamp_conn
                command.stamp(alembic_cfg, "0001_initial_schema")
                stamp_conn.commit()

    # Apply migrations up to head
    try:
        with active_engine.connect() as upgrade_conn:
            alembic_cfg.attributes["connection"] = upgrade_conn
            command.upgrade(alembic_cfg, "head")
            upgrade_conn.commit()
    except Exception as e:
        if isinstance(e, MigrationError):
            raise
        raise MigrationError(f"Database migration failed: {str(e)}") from e
    finally:
        if owns_engine:
            active_engine.dispose()

def init_db():
    """
    Initialize database schema via Alembic migrations.
    Replaces create_all() to ensure proper schema migration tracking.
    """
    run_migrations()
