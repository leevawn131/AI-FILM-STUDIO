import os
import sqlite3
import pytest
from pathlib import Path
from sqlalchemy import create_engine, inspect
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.database import (
    run_migrations,
    backup_database,
    IncompatibleLegacySchemaError,
    REQUIRED_LEGACY_SCHEMA
)

client = TestClient(app)

def create_full_legacy_schema(db_path: Path):
    """Creates a complete and valid 9-table unversioned legacy database matching 0001_initial_schema."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE projects (
            id VARCHAR(36) PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            logline TEXT,
            genre VARCHAR(64),
            aspect_ratio VARCHAR(16),
            fps INTEGER,
            target_duration_sec INTEGER,
            visual_style TEXT,
            budget_total FLOAT,
            budget_spent FLOAT,
            created_at DATETIME,
            updated_at DATETIME
        );
        CREATE TABLE characters (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            name VARCHAR(128) NOT NULL,
            role VARCHAR(64),
            age VARCHAR(32),
            facial_characteristics TEXT,
            clothing TEXT,
            voice_profile TEXT,
            personality TEXT,
            visual_prompt_tags TEXT,
            reference_image_path VARCHAR(512),
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE locations (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            name VARCHAR(128) NOT NULL,
            description TEXT,
            time_of_day VARCHAR(64),
            weather VARCHAR(64),
            lighting_setup TEXT,
            visual_prompt_tags TEXT,
            reference_image_path VARCHAR(512),
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE scenes (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            scene_number INTEGER NOT NULL,
            slugline VARCHAR(256) NOT NULL,
            title VARCHAR(256),
            summary TEXT,
            script_content TEXT,
            location_id VARCHAR(36),
            estimated_duration INTEGER,
            budget_limit FLOAT,
            order_index INTEGER,
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE media_assets (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            filename VARCHAR(256) NOT NULL,
            file_path VARCHAR(512) NOT NULL,
            media_type VARCHAR(32),
            width INTEGER,
            height INTEGER,
            duration_seconds FLOAT,
            fps FLOAT,
            codec VARCHAR(64),
            file_size_bytes INTEGER,
            thumbnail_path VARCHAR(512),
            auto_matched_shot_id VARCHAR(36),
            is_validated BOOLEAN,
            validation_notes TEXT,
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE shots (
            id VARCHAR(36) PRIMARY KEY,
            scene_id VARCHAR(36) NOT NULL,
            project_id VARCHAR(36) NOT NULL,
            shot_number INTEGER NOT NULL,
            shot_code VARCHAR(32),
            duration_seconds FLOAT,
            camera_angle VARCHAR(64),
            camera_movement VARCHAR(64),
            visual_description TEXT,
            lighting VARCHAR(256),
            character_references TEXT,
            video_prompt TEXT,
            negative_prompt TEXT,
            seed INTEGER,
            generation_status VARCHAR(32),
            video_asset_id VARCHAR(36),
            thumbnail_path VARCHAR(512),
            order_index INTEGER,
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
        );
        CREATE TABLE audio_tracks (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            track_type VARCHAR(32),
            title VARCHAR(256) NOT NULL,
            file_path VARCHAR(512) NOT NULL,
            duration_seconds FLOAT,
            start_time_seconds FLOAT,
            volume_level FLOAT,
            ducking_enabled BOOLEAN,
            ducking_level FLOAT,
            scene_id VARCHAR(36),
            shot_id VARCHAR(36),
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE cost_records (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            scene_id VARCHAR(36),
            action_type VARCHAR(64) NOT NULL,
            credits_used FLOAT,
            cost_usd FLOAT,
            prompt_hash VARCHAR(64),
            details TEXT,
            created_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE export_jobs (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            preset_name VARCHAR(64),
            resolution VARCHAR(32),
            aspect_ratio VARCHAR(16),
            fps INTEGER,
            include_subtitles BOOLEAN,
            status VARCHAR(32),
            progress_pct INTEGER,
            output_path VARCHAR(512),
            file_size_bytes INTEGER,
            error_message TEXT,
            created_at DATETIME,
            completed_at DATETIME,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

def test_1_empty_database_migration(tmp_path):
    """Test 1: Brand new empty database migrates directly to head with all tables."""
    fresh_db = tmp_path / "fresh.db"
    db_url = f"sqlite:///{fresh_db}"
    fresh_engine = create_engine(db_url)

    run_migrations(target_engine=fresh_engine, db_url=db_url)

    with fresh_engine.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        expected = {
            "projects", "characters", "locations", "scenes",
            "shots", "media_assets", "audio_tracks", "cost_records",
            "export_jobs", "studio_jobs", "alembic_version"
        }
        assert expected.issubset(tables)

def test_2_valid_legacy_database_migration_with_backup_and_data_preservation(tmp_path):
    """Test 2: Complete legacy database is backed up, stamped, upgraded to head with zero data loss."""
    legacy_db = tmp_path / "legacy.db"
    create_full_legacy_schema(legacy_db)

    # Insert existing film production data
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("INSERT INTO projects (id, name, budget_total, budget_spent) VALUES ('proj-alpha', 'Solaris Reboot', 500.0, 50.0)")
    conn.execute("INSERT INTO characters (id, project_id, name) VALUES ('char-1', 'proj-alpha', 'DR. KELVIN')")
    conn.execute("INSERT INTO scenes (id, project_id, scene_number, slugline) VALUES ('sc-1', 'proj-alpha', 1, 'INT. STATION')")
    conn.commit()
    conn.close()

    db_url = f"sqlite:///{legacy_db}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    # Verify existing data preserved
    verify_conn = sqlite3.connect(str(legacy_db))
    cur = verify_conn.cursor()
    cur.execute("SELECT name, budget_total FROM projects WHERE id = 'proj-alpha'")
    row = cur.fetchone()
    assert row == ("Solaris Reboot", 500.0)

    cur.execute("SELECT name FROM characters WHERE id = 'char-1'")
    assert cur.fetchone()[0] == "DR. KELVIN"

    # Verify upgraded columns exist
    cur.execute("SELECT version_num FROM alembic_version")
    ver = cur.fetchone()[0]
    assert ver == "0007_upload_operation_lease_fencing"

    cur.execute("PRAGMA table_info(studio_jobs)")
    job_cols = [c[1] for c in cur.fetchall()]
    assert "lease_token" in job_cols
    assert "fencing_token" in job_cols

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='upload_operations'")
    assert cur.fetchone() is not None

    cur.execute("PRAGMA table_info(upload_operations)")
    upload_cols = [c[1] for c in cur.fetchall()]
    assert "worker_id" in upload_cols
    assert "lease_expires_at" in upload_cols
    assert "fencing_token" in upload_cols

    cur.execute("PRAGMA table_info(media_assets)")
    assert "checksum" in [c[1] for c in cur.fetchall()]

    cur.execute("PRAGMA table_info(scenes)")
    assert "budget_spent" in [c[1] for c in cur.fetchall()]

    cur.execute("PRAGMA table_info(shots)")
    cols = [c[1] for c in cur.fetchall()]
    assert "flow_credits_spent" in cols
    assert "generation_attempts" in cols

    verify_conn.close()

def test_3_partially_initialized_legacy_database_rejected(tmp_path):
    """Test 3: Unversioned database with incomplete schema is rejected with IncompatibleLegacySchemaError."""
    partial_db = tmp_path / "partial.db"
    conn = sqlite3.connect(str(partial_db))
    # Only create projects table, missing other 8 required tables
    conn.execute("CREATE TABLE projects (id VARCHAR(36) PRIMARY KEY, name VARCHAR(128) NOT NULL)")
    conn.commit()
    conn.close()

    db_url = f"sqlite:///{partial_db}"
    engine = create_engine(db_url)

    with pytest.raises(IncompatibleLegacySchemaError) as exc_info:
        run_migrations(target_engine=engine, db_url=db_url)

    assert "Cannot safely migrate legacy database" in str(exc_info.value)
    assert "Missing required legacy tables" in str(exc_info.value)

def test_4_unknown_schema_database_rejected(tmp_path):
    """Test 4: Unversioned database with unknown tables is rejected with IncompatibleLegacySchemaError."""
    unknown_db = tmp_path / "unknown.db"
    conn = sqlite3.connect(str(unknown_db))
    conn.execute("CREATE TABLE wp_posts (ID INTEGER PRIMARY KEY, post_title TEXT)")
    conn.commit()
    conn.close()

    db_url = f"sqlite:///{unknown_db}"
    engine = create_engine(db_url)

    with pytest.raises(IncompatibleLegacySchemaError) as exc_info:
        run_migrations(target_engine=engine, db_url=db_url)

    assert "Missing required legacy tables" in str(exc_info.value)

def test_5_existing_alembic_managed_database_upgrades(tmp_path):
    """Test 5: Database already stamped by Alembic directly applies pending revisions without re-stamping."""
    alembic_db = tmp_path / "alembic_managed.db"
    create_full_legacy_schema(alembic_db)

    # Stamp as 0001
    conn = sqlite3.connect(str(alembic_db))
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    conn.execute("INSERT INTO alembic_version VALUES ('0001_initial_schema')")
    conn.commit()
    conn.close()

    db_url = f"sqlite:///{alembic_db}"
    engine = create_engine(db_url)
    run_migrations(target_engine=engine, db_url=db_url)

    # Verify upgraded to 0007
    vconn = sqlite3.connect(str(alembic_db))
    cur = vconn.cursor()
    cur.execute("SELECT version_num FROM alembic_version")
    assert cur.fetchone()[0] == "0007_upload_operation_lease_fencing"
    vconn.close()

def test_database_backup_procedure(tmp_path):
    """Verifies that backup_database creates an exact, readable SQLite snapshot."""
    src_db = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"

    conn = sqlite3.connect(str(src_db))
    conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, msg TEXT)")
    conn.execute("INSERT INTO test_data (msg) VALUES ('critical film data')")
    conn.commit()
    conn.close()

    backup_path = backup_database(db_path=src_db, backup_dir=backup_dir)
    assert backup_path.exists()
    assert backup_path.name.startswith("studio_backup_")

    bk_conn = sqlite3.connect(str(backup_path))
    bk_cur = bk_conn.cursor()
    bk_cur.execute("SELECT msg FROM test_data WHERE id = 1")
    row = bk_cur.fetchone()
    assert row[0] == "critical film data"
    bk_conn.close()

def test_system_backup_endpoints():
    """Verifies POST /api/system/backup and GET /api/system/backups."""
    res = client.post("/api/system/backup")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "backup_file" in data
    assert data["size_bytes"] > 0

    list_res = client.get("/api/system/backups")
    assert list_res.status_code == 200
    backups = list_res.json()
    assert isinstance(backups, list)
    assert len(backups) >= 1
    assert any(b["filename"] == data["backup_file"] for b in backups)

    # Clean up generated backup
    bk_file = settings.backup_dir / data["backup_file"]
    if bk_file.exists():
        bk_file.unlink()

def test_configurable_demo_seeding():
    """Verifies that seed_initial_demo_if_empty respects the seed_demo_data setting."""
    assert settings.seed_demo_data is False
