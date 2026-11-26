"""Small SQLite account and garden-profile store for the Irrigate web app."""

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

# Zones offered to new gardens. The second value marks zones that rain does not
# reach (they may still be watered by Auto mode while it rains).
DEFAULT_ZONES = (
    ("Front Garden", False),
    ("Back Garden", False),
    ("Side Bed", True),
    ("Greenhouse", True),
)


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
                CREATE TABLE IF NOT EXISTS zone_labels (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    zone_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    PRIMARY KEY (user_id, zone_id)
                );
                CREATE TABLE IF NOT EXISTS tank_settings (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    water_supplier TEXT NOT NULL DEFAULT '',
                    catchment_area_m2 REAL NOT NULL DEFAULT 20,
                    collection_efficiency REAL NOT NULL DEFAULT 0.8,
                    water_tariff_gbp_per_m3 REAL
                );
                CREATE TABLE IF NOT EXISTS garden_zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    garden_id INTEGER NOT NULL REFERENCES gardens(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    sheltered INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    plant_type TEXT,
                    soil_type TEXT
                );
                CREATE INDEX IF NOT EXISTS gardens_user_id_idx ON gardens(user_id);
                CREATE INDEX IF NOT EXISTS garden_zones_garden_id_idx ON garden_zones(garden_id);
            """)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(garden_zones)")}
            for column in ("plant_type", "soil_type"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE garden_zones ADD COLUMN {column} TEXT")
            self._seed_missing_zones(conn)

    @staticmethod
    def _seed_missing_zones(conn):
        """Give gardens saved before per-garden zones existed the default zones,
        keeping any names the user had already given the fixed zones."""
        rows = conn.execute(
            "SELECT g.id, g.user_id FROM gardens g "
            "WHERE NOT EXISTS (SELECT 1 FROM garden_zones z WHERE z.garden_id = g.id)"
        ).fetchall()
        for row in rows:
            labels = {
                r["zone_id"]: r["label"]
                for r in conn.execute(
                    "SELECT zone_id, label FROM zone_labels WHERE user_id = ?", (row["user_id"],)
                )
            }
            conn.executemany(
                "INSERT INTO garden_zones (garden_id, name, sheltered, sort_order) VALUES (?, ?, ?, ?)",
                [
                    (row["id"], labels.get(name, name), 1 if sheltered else 0, index)
                    for index, (name, sheltered) in enumerate(DEFAULT_ZONES)
                ],
            )

    @staticmethod
    def _zones_for(conn, garden_id: int) -> List[Dict[str, Any]]:
        # A zone without its own plant/soil type uses the account's main choice.
        rows = conn.execute(
            "SELECT z.id, z.name, z.sheltered, "
            "COALESCE(z.plant_type, u.plant_type) AS plant_type, "
            "COALESCE(z.soil_type, u.soil_type) AS soil_type "
            "FROM garden_zones z JOIN gardens g ON g.id = z.garden_id JOIN users u ON u.id = g.user_id "
            "WHERE z.garden_id = ? ORDER BY z.sort_order, z.id",
            (garden_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "sheltered": bool(r["sheltered"]),
                "plant_type": r["plant_type"],
                "soil_type": r["soil_type"],
            }
            for r in rows
        ]

    @staticmethod
    def _insert_zones(conn, garden_id: int, zones: Iterable[Dict[str, Any]]) -> None:
        conn.executemany(
            "INSERT INTO garden_zones (garden_id, name, sheltered, sort_order, plant_type, soil_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    garden_id,
                    zone["name"],
                    1 if zone.get("sheltered") else 0,
                    index,
                    zone.get("plant_type") or None,
                    zone.get("soil_type") or None,
                )
                for index, zone in enumerate(zones)
            ],
        )

    def _with_zones(self, conn, rows) -> List[Dict[str, Any]]:
        gardens = []
        for row in rows:
            garden = dict(row)
            garden["zones"] = self._zones_for(conn, garden["id"])
            gardens.append(garden)
        return gardens

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
                cursor = conn.execute(
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
                zones = garden.get("zones") or [
                    {"name": name, "sheltered": sheltered} for name, sheltered in DEFAULT_ZONES
                ]
                self._insert_zones(conn, int(cursor.lastrowid), zones)
            conn.execute("UPDATE users SET onboarding_complete = 1 WHERE id = ?", (user_id,))
            rows = conn.execute(
                "SELECT id, name, location_label, latitude, longitude, postcode, is_primary "
                "FROM gardens WHERE user_id = ? ORDER BY id", (user_id,),
            ).fetchall()
            return self._with_zones(conn, rows)

    def list_gardens(self, user_id: int) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, location_label, latitude, longitude, postcode, is_primary "
                "FROM gardens WHERE user_id = ? ORDER BY is_primary DESC, id", (user_id,),
            ).fetchall()
            return self._with_zones(conn, rows)

    def get_garden(self, user_id: int, garden_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, name, location_label, latitude, longitude, postcode, is_primary "
                "FROM gardens WHERE user_id = ? AND id = ?", (user_id, garden_id),
            ).fetchone()
            return self._with_zones(conn, [row])[0] if row else None

    def save_garden_zones(
        self, user_id: int, garden_id: int, zones: Iterable[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Replace a garden's zones. Returns the updated garden, or None if it is not the user's."""
        with self.connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM gardens WHERE id = ? AND user_id = ?", (garden_id, user_id),
            ).fetchone()
            if not owned:
                return None
            conn.execute("DELETE FROM garden_zones WHERE garden_id = ?", (garden_id,))
            self._insert_zones(conn, garden_id, zones)
        return self.get_garden(user_id, garden_id)

    def get_zone_labels(self, user_id: int) -> Dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT zone_id, label FROM zone_labels WHERE user_id = ?", (user_id,),
            ).fetchall()
        return {row["zone_id"]: row["label"] for row in rows}

    def save_zone_labels(self, user_id: int, labels: Dict[str, str]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM zone_labels WHERE user_id = ?", (user_id,))
            conn.executemany(
                "INSERT INTO zone_labels (user_id, zone_id, label) VALUES (?, ?, ?)",
                [(user_id, zone_id, label) for zone_id, label in labels.items()],
            )

    def get_tank_settings(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT water_supplier, catchment_area_m2, collection_efficiency, water_tariff_gbp_per_m3 "
                "FROM tank_settings WHERE user_id = ?", (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_tank_settings(self, user_id: int, settings: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO tank_settings
                   (user_id, water_supplier, catchment_area_m2, collection_efficiency, water_tariff_gbp_per_m3)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     water_supplier = excluded.water_supplier,
                     catchment_area_m2 = excluded.catchment_area_m2,
                     collection_efficiency = excluded.collection_efficiency,
                     water_tariff_gbp_per_m3 = excluded.water_tariff_gbp_per_m3""",
                (
                    user_id,
                    settings["water_supplier"],
                    settings["catchment_area_m2"],
                    settings["collection_efficiency"],
                    settings["water_tariff_gbp_per_m3"],
                ),
            )
