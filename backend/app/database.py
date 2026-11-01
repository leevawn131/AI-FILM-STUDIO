import os
import sqlite3
import datetime
from pathlib import Path
from typing import Optional
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from alembic.config import Config
from alembic import command

from backend.app.config import settings

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

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
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

def run_migrations(target_engine=None, db_url: Optional[str] = None) -> None:
    """
    Applies Alembic migrations up to head.
    Detects existing legacy unversioned tables and stamps revision 0001 before upgrading,
    preventing data loss and duplicate table errors.
    """
    import backend.app.models.entities  # ensure all models are registered

    active_engine = target_engine or engine
    url = db_url or settings.database_url
    
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    alembic_ini_path = settings.root_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("script_location", str(settings.root_dir / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.attributes["configure_logger"] = False

    # Check if legacy database (has tables like projects but no alembic_version)
    with active_engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        needs_stamp = ("projects" in tables and "alembic_version" not in tables)

    if needs_stamp:
        with active_engine.connect() as conn:
            alembic_cfg.attributes["connection"] = conn
            command.stamp(alembic_cfg, "0001_initial_schema")
            conn.commit()

    with active_engine.connect() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "head")
        conn.commit()

def init_db():
    """
    Initialize database schema via Alembic migrations.
    Replaces create_all() to ensure proper schema migration tracking.
    """
    run_migrations()
