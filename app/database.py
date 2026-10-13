import logging
import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

os.makedirs(Path(settings.data_dir) / "db", exist_ok=True)

engine = create_engine(
    settings.database_url,
    # check_same_thread: FastAPI's background tasks and request handlers use
    # different threads than the one that opened the connection.
    # timeout: how long sqlite3 waits on a locked database before raising
    # "database is locked", instead of Python's 5s default -- a background
    # reanalyze/import holding a write lock for a moment used to make any
    # concurrent page load fail outright rather than just wait briefly.
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _record):
    """WAL lets readers (loading a page) proceed while a writer (an import
    or reanalyze running in the background) is mid-transaction, instead of
    SQLite's default journal mode where a writer blocks every reader. This
    is what "database is locked" on a concurrent page load was missing."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401 - ensures models are registered
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _add_missing_indexes()

    # Fingerprinting the imports made before deduplication existed
    # (app/dedup.py) takes a while on a large database -- run in the
    # background so the server starts listening right away instead of a TCP
    # liveness probe killing it mid-backfill. Until it's done, those imports
    # just don't show up in the platform views yet.
    threading.Thread(target=_backfill_fingerprints, name="dedup-backfill", daemon=True).start()


def _backfill_fingerprints() -> None:
    from app import dedup
    try:
        with SessionLocal() as db:
            dedup.backfill(db)
    except Exception:
        logger.exception("Dedup backfill failed -- it runs again at the next start")


def _add_missing_columns() -> None:
    """No migration framework at this stage of the project: the model keeps
    gaining columns while the app is under active development, and
    Base.metadata.create_all() only creates tables that don't exist yet --
    it never alters an existing one. Without this, every schema change would
    require wiping the database, discarding whatever has already been
    imported. This only ever ADDS nullable columns, never renames/drops."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                logger.info("Migration: added column %s.%s", table.name, column.name)


def _add_missing_indexes() -> None:
    """Same rationale as _add_missing_columns(): create_all() only creates
    indexes for brand-new tables, it never adds one to a table that already
    exists. Composite indexes added to a model after the table was first
    created (e.g. for the conversations page) would otherwise silently never
    apply to an existing database -- CREATE INDEX IF NOT EXISTS backfills
    them without needing a migration framework."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue
            existing = {ix["name"] for ix in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name in existing:
                    continue
                columns = ", ".join(f'"{col.name}"' for col in index.columns)
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{index.name}" ON "{table.name}" ({columns})'))
                logger.info("Migration: added index %s on %s", index.name, table.name)


def uploads_dir() -> Path:
    path = Path(settings.data_dir) / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def imports_dir() -> Path:
    path = Path(settings.data_dir) / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def anonymized_dir() -> Path:
    path = Path(settings.data_dir) / "anonymized"
    path.mkdir(parents=True, exist_ok=True)
    return path
