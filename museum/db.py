"""SQLite 持久化层：连接与表结构。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    UNIQUE (batch_id, seq)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    hum_min REAL NOT NULL,
    hum_max REAL NOT NULL,
    sample_interval INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, version_no)
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    ts TEXT NOT NULL,
    temperature REAL NOT NULL,
    humidity REAL NOT NULL,
    corrected_from INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    metric TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    reading_count INTEGER NOT NULL,
    min_value REAL NOT NULL,
    max_value REAL NOT NULL,
    limit_lo REAL NOT NULL,
    limit_hi REAL NOT NULL,
    nodes TEXT NOT NULL,
    max_deviation REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    close_note TEXT,
    close_evidence TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    expected_interval INTEGER NOT NULL,
    actual_interval REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_version ON readings(version_id, artifact_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_version ON events(version_id, status);
CREATE INDEX IF NOT EXISTS idx_gaps_version ON gaps(version_id);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
