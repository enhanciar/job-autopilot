from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from backend.core.config import DB_PATH

from sqlalchemy import event

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 60})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")       # readers never block writers; concurrent collectors are fine
    cur.execute("PRAGMA busy_timeout=60000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    from backend.app import models  # noqa: F401
    Base.metadata.create_all(engine)
    # lightweight migrations for columns added later (SQLite ALTER TABLE ADD COLUMN)
    from sqlalchemy import text, inspect
    cols = {c["name"] for c in inspect(engine).get_columns("jobs")}
    with engine.begin() as conn:
        if "country" not in cols:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN country VARCHAR(60)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_country ON jobs (country)"))
    # Versioned, additive migrations; web startup never resets another process's runs.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
        run_cols = {c["name"] for c in inspect(engine).get_columns("runs")}
        for name, typ in (("spec", "JSON"), ("checkpoints", "JSON"), ("cancel_requested", "BOOLEAN DEFAULT 0")):
            if name not in run_cols: conn.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {typ}"))
        # Existing duplicates must be reconciled manually, never silently deleted.
        duplicate = conn.execute(text("SELECT job_id FROM applications GROUP BY job_id HAVING count(*) > 1 LIMIT 1")).first()
        if duplicate: raise RuntimeError("Duplicate applications require reconciliation before migration")
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_application_job ON applications(job_id)"))
        conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)"))

        for table in ("applications", "outreach"):
            columns = {c["name"] for c in inspect(conn).get_columns(table)}
            if "claim_run_id" not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN claim_run_id INTEGER"))
        conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)"))

        if "provider_message_id" not in {c["name"] for c in inspect(conn).get_columns("outreach")}:
            conn.execute(text("ALTER TABLE outreach ADD COLUMN provider_message_id VARCHAR(200)"))
        conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)"))
        conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)"))

        if "job_ids" not in {c["name"] for c in inspect(conn).get_columns("questions")}:
            conn.execute(text("ALTER TABLE questions ADD COLUMN job_ids JSON"))
        conn.execute(text("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)"))   # questions + chat_turns (created above)
