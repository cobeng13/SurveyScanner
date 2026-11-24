from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    template TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    total INTEGER NOT NULL,
    metadata TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    message TEXT,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS batch_queue ON batches (status, deleted, created);
CREATE INDEX IF NOT EXISTS batch_owner ON batches (owner, deleted, created);
CREATE TABLE IF NOT EXISTS pages (
    batch_id TEXT NOT NULL REFERENCES batches (id) ON DELETE CASCADE,
    page INTEGER NOT NULL,
    id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    machine TEXT NOT NULL,
    warnings TEXT NOT NULL,
    overlay TEXT,
    review TEXT NOT NULL DEFAULT 'Unreviewed',
    reviewed_at TEXT,
    reviewer TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, page)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    changed_at TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    FOREIGN KEY (batch_id, page) REFERENCES pages (batch_id, page) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS changes_page ON changes (batch_id, page, id);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def elapsed(batch):
    if not batch["started"]:
        return 0
    start = datetime.fromisoformat(batch["started"])
    end = datetime.fromisoformat(batch["finished"] or now())
    return max(0, int((end - start).total_seconds()))


def batch_dir(data, batch_id):
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise ValueError("Invalid batch ID")
    path = data / "batches" / batch_id
    if path.resolve() != path:
        raise ValueError("Unsafe batch directory")
    return path


class Database:
    def __init__(self, data: Path):
        self.path = data / "database" / "survey.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        (data / "batches").mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            with conn:
                yield conn
        finally:
            conn.close()

    def submit(self, batch_id, owner, template, filename, size, total, metadata):
        with self.connect(write=True) as conn:
            conn.execute(
                """INSERT INTO batches(id,owner,template,filename,size,total,metadata,status,created)
                   VALUES (?,?,?,?,?,?,?,'Queued',?)""",
                (batch_id, owner, template, filename, size, total, json.dumps(metadata), now()),
            )

    def recover(self):
        with self.connect(write=True) as conn:
            conn.execute(
                """UPDATE batches SET status='Interrupted', finished=?, message=?
                   WHERE status='Processing'""",
                (now(), "The worker stopped during recognition. Retry to process the original scan."),
            )

    def claim(self):
        with self.connect(write=True) as conn:
            batch = conn.execute(
                "SELECT id FROM batches WHERE status='Queued' AND deleted=0 ORDER BY created, id LIMIT 1"
            ).fetchone()
            if not batch:
                return None
            return conn.execute(
                """UPDATE batches SET status='Processing', started=?, finished=NULL, message=NULL,
                   attempt=attempt+1 WHERE id=? RETURNING *""", (now(), batch["id"]),
            ).fetchone()

    def finish(self, batch, status, message=None, pages=()):
        with self.connect(write=True) as conn:
            current = conn.execute(
                "SELECT 1 FROM batches WHERE id=? AND status='Processing' AND attempt=? AND deleted=0",
                (batch["id"], batch["attempt"]),
            ).fetchone()
            if not current:
                return
            conn.execute("DELETE FROM pages WHERE batch_id=?", (batch["id"],))
            conn.executemany(
                """INSERT INTO pages(batch_id,page,id,status,machine,warnings,overlay,review)
                   VALUES (?,?,?,?,?,?,?,?)""", pages,
            )
            conn.execute("UPDATE batches SET status=?, finished=?, message=? WHERE id=?",
                         (status, now(), message, batch["id"]))

    def batch(self, conn, owner, batch_id):
        row = conn.execute("SELECT * FROM batches WHERE id=? AND owner=? AND deleted=0",
                           (batch_id, owner)).fetchone()
        return dict(row, metadata=json.loads(row["metadata"])) if row else None

    def pages(self, conn, batch_id):
        rows = conn.execute("SELECT * FROM pages WHERE batch_id=? ORDER BY page", (batch_id,)).fetchall()
        edits = conn.execute("SELECT * FROM changes WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        changes = {}
        for edit in edits:
            changes.setdefault(edit["page"], {})[edit["field"]] = edit["value"]
        return [
            dict(row, machine=json.loads(row["machine"]), warnings=json.loads(row["warnings"]),
                 corrections=changes.get(row["page"], {}))
            for row in rows
        ], edits
