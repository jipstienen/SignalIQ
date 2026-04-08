import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    pool_recycle=300,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def migrate_article_assessment_columns() -> None:
    """Add columns introduced after first deploy (PostgreSQL). Safe to run multiple times."""
    if "postgresql" not in settings.database_url.lower():
        return
    stmts = [
        "ALTER TABLE article_assessments ADD COLUMN IF NOT EXISTS base_score DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE article_assessments ADD COLUMN IF NOT EXISTS final_score DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE article_assessments ADD COLUMN IF NOT EXISTS semantic_category VARCHAR(40) NOT NULL DEFAULT 'irrelevant'",
        "ALTER TABLE article_assessments ADD COLUMN IF NOT EXISTS semantic_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE article_assessments ADD COLUMN IF NOT EXISTS entity_match DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE article_assessments ADD COLUMN IF NOT EXISTS event_importance DOUBLE PRECISION NOT NULL DEFAULT 0",
    ]
    try:
        with engine.begin() as conn:
            for sql in stmts:
                conn.execute(text(sql))
    except Exception as exc:
        logger.warning("article_assessments migration skipped or failed: %s", exc)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

