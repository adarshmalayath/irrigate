"""Small SQLite account and garden-profile store for the Irrigate web app."""

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional


class AccountStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    plant_type TEXT,
                    soil_type TEXT,
                    onboarding_complete INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS gardens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    location_label TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    postcode TEXT NOT NULL DEFAULT '',
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS gardens_user_id_idx ON gardens(user_id);
            """)

    def create_user(self, email: str, display_name: str, password_hash: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO users (email, display_name, password_hash) VALUES (?, ?, ?)",
                (email, display_name, password_hash),
            )
            return int(cursor.lastrowid)

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, email, display_name, plant_type, soil_type, onboarding_complete "
                "FROM users WHERE id = ?", (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_onboarding(
        self,
        user_id: int,
        plant_type: str,
        soil_type: str,
        gardens: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET plant_type = ?, soil_type = ?, onboarding_complete = 0 WHERE id = ?",
                (plant_type, soil_type, user_id),
            )
            conn.execute("DELETE FROM gardens WHERE user_id = ?", (user_id,))
            for index, garden in enumerate(gardens):
                conn.execute(
                    """INSERT INTO gardens
                       (user_id, name, location_label, latitude, longitude, postcode, is_primary)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        garden["name"],
                        garden["location_label"],
                        garden["latitude"],
                        garden["longitude"],
                        garden.get("postcode", ""),
                        1 if index == 0 else 0,
                    ),
                )
            conn.execute("UPDATE users SET onboarding_complete = 1 WHERE id = ?", (user_id,))
            rows = conn.execute(
                "SELECT id, name, location_label, latitude, longitude, postcode, is_primary "
                "FROM gardens WHERE user_id = ? ORDER BY id", (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_gardens(self, user_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, location_label, latitude, longitude, postcode, is_primary "
                "FROM gardens WHERE user_id = ? ORDER BY is_primary DESC, id", (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_garden(self, user_id: int, garden_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, name, location_label, latitude, longitude, postcode, is_primary "
                "FROM gardens WHERE user_id = ? AND id = ?", (user_id, garden_id),
            ).fetchone()
        return dict(row) if row else None
