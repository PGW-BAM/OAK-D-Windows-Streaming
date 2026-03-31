"""SQLite-backed connectivity and alert history (24-hour rolling window)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "connectivity.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connectivity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    state TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    duration_s REAL
);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    component TEXT NOT NULL,
    message TEXT,
    email_sent BOOLEAN DEFAULT FALSE,
    timestamp DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conn_ts ON connectivity_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_alert_ts ON alert_log(timestamp);
"""


class HistoryDB:
    """Async SQLite wrapper for connectivity and alert history."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _DB_PATH
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("History database opened: %s", self._path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def log_connectivity(
        self, component: str, state: str, duration_s: float | None = None
    ) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO connectivity_log (component, state, timestamp, duration_s) "
            "VALUES (?, ?, ?, ?)",
            (component, state, now, duration_s),
        )
        await self._db.commit()

    async def log_alert(
        self, alert_type: str, component: str, message: str, email_sent: bool = False
    ) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO alert_log (alert_type, component, message, email_sent, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (alert_type, component, message, email_sent, now),
        )
        await self._db.commit()

    async def cleanup_old(self, hours: int = 24) -> int:
        """Delete records older than `hours`. Returns count deleted."""
        if not self._db:
            return 0
        cutoff = datetime.now(timezone.utc).isoformat()
        # SQLite datetime comparison works on ISO strings
        cursor = await self._db.execute(
            "DELETE FROM connectivity_log WHERE timestamp < datetime(?, '-' || ? || ' hours')",
            (cutoff, hours),
        )
        count1 = cursor.rowcount
        cursor = await self._db.execute(
            "DELETE FROM alert_log WHERE timestamp < datetime(?, '-' || ? || ' hours')",
            (cutoff, hours),
        )
        count2 = cursor.rowcount
        await self._db.commit()
        return count1 + count2

    async def get_recent_connectivity(
        self, component: str | None = None, limit: int = 100
    ) -> list[dict]:
        if not self._db:
            return []
        if component:
            cursor = await self._db.execute(
                "SELECT component, state, timestamp, duration_s "
                "FROM connectivity_log WHERE component = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (component, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT component, state, timestamp, duration_s "
                "FROM connectivity_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [
            {"component": r[0], "state": r[1], "timestamp": r[2], "duration_s": r[3]}
            for r in rows
        ]

    async def get_recent_alerts(self, limit: int = 50) -> list[dict]:
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT alert_type, component, message, email_sent, timestamp "
            "FROM alert_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "alert_type": r[0],
                "component": r[1],
                "message": r[2],
                "email_sent": bool(r[3]),
                "timestamp": r[4],
            }
            for r in rows
        ]
