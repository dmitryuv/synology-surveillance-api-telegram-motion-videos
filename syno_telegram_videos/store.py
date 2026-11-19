"""Per-camera checkpoint in SQLite.

The schema is the one the previous version used, so an existing
processed_events.db keeps working without a migration: one row per camera
holding the id of the last event that reached a terminal outcome.
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events (
    id integer PRIMARY KEY,
    camera_id text NOT NULL,
    last_event_id int NOT NULL,
    processed_date timestamp NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_events_camera
    ON processed_events (camera_id);
"""


class EventStore:
    def __init__(self, data_folder):
        Path(data_folder).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(Path(data_folder) / "processed_events.db"),
            check_same_thread=False,
        )
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def checkpoint(self, camera_id):
        """Last processed event id for a camera, or None on the first run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_event_id FROM processed_events WHERE camera_id=?",
                (str(camera_id),),
            ).fetchone()
        return int(row[0]) if row else None

    def set_checkpoint(self, camera_id, event_id):
        with self._lock:
            self._conn.execute(
                """INSERT INTO processed_events(camera_id, last_event_id, processed_date)
                   VALUES(?,?,?)
                   ON CONFLICT(camera_id) DO UPDATE SET
                     last_event_id=excluded.last_event_id,
                     processed_date=excluded.processed_date""",
                (str(camera_id), event_id, datetime.datetime.now()),
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()