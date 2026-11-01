from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
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

def init_db():
    import backend.app.models.entities  # ensure all models are registered
    Base.metadata.create_all(bind=engine)
