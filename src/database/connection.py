"""Database connection and session management."""

import logging
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from src.config import settings
from src.database.sync_fence import SyncSession

logger = logging.getLogger(__name__)

# Create database engine with Postgres pool config
engine_kwargs = {"echo": settings.database_echo}
if "postgresql" in settings.database_url:
    engine_kwargs.update(
        {
            "pool_size": 5,
            "max_overflow": 10,
            "pool_pre_ping": True,
        }
    )

engine = create_engine(settings.database_url, **engine_kwargs)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=SyncSession)


def get_db() -> Session:
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_session():
    """Context manager for database sessions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        logger.error("Database session error: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


def init_database():
    """Apply versioned schema migrations and encrypt legacy credentials."""
    project_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(alembic_config, "head")

    from src.security.crypto import rotate_plaintext_credentials

    rotated = rotate_plaintext_credentials()
    if rotated:
        logger.info("Encrypted %s legacy mailbox credentials", rotated)
    logger.info("Database migrations applied successfully")
