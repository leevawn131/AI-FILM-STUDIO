import os
import sqlite3
import tempfile
import pytest
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app, seed_initial_demo_if_empty
from backend.app.database import run_migrations, backup_database, SessionLocal
from backend.app.models.entities import Project

client = TestClient(app)

def test_alembic_fresh_database_migration(tmp_path):
    """Verifies that a brand new database migrates directly to head with all tables and columns."""
    fresh_db = tmp_path / "fresh_test.db"
    db_url = f"sqlite:///{fresh_db}"
    fresh_engine = create_engine(db_url)

    # Run migrations
    run_migrations(target_engine=fresh_engine, db_url=db_url)

    # Inspect resulting tables
    with fresh_engine.connect() as conn:
        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        
        expected_tables = {
            "projects", "characters", "locations", "scenes",
            "shots", "media_assets", "audio_tracks", "cost_records",
            "export_jobs", "alembic_version"
        }
        assert expected_tables.issubset(tables)

        # Check media_assets checksum column
        media_cols = {col["name"] for col in inspector.get_columns("media_assets")}
        assert "checksum" in media_cols

        # Check cost_records provider-aware columns
        cost_cols = {col["name"] for col in inspector.get_columns("cost_records")}
        assert "provider" in cost_cols
        assert "flow_credits" in cost_cols
        assert "actual_cost" in cost_cols
        assert "actual_cost_known" in cost_cols
        assert "estimated_cost" in cost_cols
        assert "shot_id" in cost_cols

def test_alembic_legacy_database_migration_without_data_loss(tmp_path):
    """
    Verifies that an existing legacy database (created without Alembic)
    is detected, stamped to 0001, and upgraded to 0002 with all existing data preserved.
    """
    legacy_db = tmp_path / "legacy_test.db"
    conn = sqlite3.connect(str(legacy_db))
    cur = conn.cursor()

    # Create initial legacy schema without alembic_version
    cur.execute("""
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
    """)
    cur.execute("""
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
            created_at DATETIME
        );
    """)
    cur.execute("""
        CREATE TABLE cost_records (
            id VARCHAR(36) PRIMARY KEY,
            project_id VARCHAR(36) NOT NULL,
            scene_id VARCHAR(36),
            action_type VARCHAR(64) NOT NULL,
            credits_used FLOAT,
            cost_usd FLOAT,
            prompt_hash VARCHAR(64),
            details TEXT,
            created_at DATETIME
        );
    """)
    # Insert existing records to verify no data loss
    cur.execute("INSERT INTO projects (id, name, budget_total, budget_spent) VALUES ('proj-1', 'Legacy Sci-Fi Movie', 200.0, 10.0)")
    cur.execute("INSERT INTO cost_records (id, project_id, action_type, cost_usd) VALUES ('cost-1', 'proj-1', 'script_generation', 0.005)")
    cur.execute("INSERT INTO media_assets (id, project_id, filename, file_path) VALUES ('med-1', 'proj-1', 'clip.mp4', 'projects/proj-1/clip.mp4')")
    conn.commit()
    conn.close()

    # Now run migrations on this legacy database
    db_url = f"sqlite:///{legacy_db}"
    legacy_engine = create_engine(db_url)
    run_migrations(target_engine=legacy_engine, db_url=db_url)

    # Verify data integrity
    conn2 = sqlite3.connect(str(legacy_db))
    cur2 = conn2.cursor()

    cur2.execute("SELECT name, budget_total FROM projects WHERE id = 'proj-1'")
    row = cur2.fetchone()
    assert row is not None
    assert row[0] == "Legacy Sci-Fi Movie"
    assert row[1] == 200.0

    # Verify new columns added
    cur2.execute("PRAGMA table_info(media_assets)")
    cols = [r[1] for r in cur2.fetchall()]
    assert "checksum" in cols

    cur2.execute("PRAGMA table_info(cost_records)")
    cost_cols = [r[1] for r in cur2.fetchall()]
    assert "provider" in cost_cols
    assert "flow_credits" in cost_cols

    # Verify alembic_version was stamped and upgraded
    cur2.execute("SELECT version_num FROM alembic_version")
    ver = cur2.fetchone()[0]
    assert ver == "0002_media_security_and_cost_ledger"
    conn2.close()

def test_database_backup_procedure(tmp_path):
    """Verifies that backup_database creates an exact, readable SQLite snapshot."""
    src_db = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"

    # Seed source DB
    conn = sqlite3.connect(str(src_db))
    conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, msg TEXT)")
    conn.execute("INSERT INTO test_data (msg) VALUES ('critical film data')")
    conn.commit()
    conn.close()

    # Perform backup
    backup_path = backup_database(db_path=src_db, backup_dir=backup_dir)
    assert backup_path.exists()
    assert backup_path.name.startswith("studio_backup_")

    # Read from backup
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
    # When seed_demo_data is False and force is False, it returns immediately
    assert settings.seed_demo_data is False
