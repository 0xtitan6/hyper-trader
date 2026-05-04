import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock


class State:
    """Sqlite-backed persistent state.

    All writes serialized via RLock. Schema initialized lazily; safe to
    construct multiple instances against the same path.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS seen_tids (
        tid INTEGER PRIMARY KEY,
        leader TEXT NOT NULL,
        seen_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS own_fills (
        tid INTEGER PRIMARY KEY,
        coin TEXT NOT NULL,
        side TEXT NOT NULL,
        sz REAL NOT NULL,
        px REAL NOT NULL,
        closed_pnl REAL NOT NULL,
        fee REAL NOT NULL,
        ts INTEGER NOT NULL,
        date_utc TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_own_fills_date ON own_fills(date_utc);
    CREATE TABLE IF NOT EXISTS positions (
        coin TEXT PRIMARY KEY,
        sz REAL NOT NULL,
        avg_px REAL NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    def mark_tid_seen(self, tid: int, leader: str) -> bool:
        """Atomically insert a tid. Returns True if newly inserted, False if already seen."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO seen_tids(tid, leader, seen_at) VALUES (?, ?, ?)",
                    (tid, leader.lower(), now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def is_tid_seen(self, tid: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM seen_tids WHERE tid = ?", (tid,)).fetchone()
            return row is not None

    def unmark_tid_seen(self, tid: int) -> bool:
        """Remove a tid from the seen set so it can be retried via backfill.
        Returns True if a row was deleted, False if it wasn't present.
        Used after a pipeline error: the leader fill was received but never
        successfully mirrored, so backfill should pick it up again.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM seen_tids WHERE tid = ?", (tid,))
            return cur.rowcount > 0

    def record_own_fill(
        self,
        tid: int,
        coin: str,
        side: str,
        sz: float,
        px: float,
        closed_pnl: float,
        fee: float,
        ts: int,
        date_utc: str,
    ) -> bool:
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO own_fills(tid, coin, side, sz, px, closed_pnl, fee, ts, date_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tid, coin, side, sz, px, closed_pnl, fee, ts, date_utc),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def daily_pnl(self, date_utc: str) -> tuple[float, float]:
        """Return (realized_pnl, total_fees) for the given UTC date string YYYY-MM-DD."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(closed_pnl), 0.0) AS pnl, "
                "COALESCE(SUM(fee), 0.0) AS fee FROM own_fills WHERE date_utc = ?",
                (date_utc,),
            ).fetchone()
            return float(row["pnl"]), float(row["fee"])

    def get_position(self, coin: str) -> tuple[float, float]:
        """Return (sz, avg_px). Zero if unknown."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT sz, avg_px FROM positions WHERE coin = ?", (coin,)
            ).fetchone()
            if row is None:
                return 0.0, 0.0
            return float(row["sz"]), float(row["avg_px"])

    def get_positions(self) -> dict[str, tuple[float, float]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT coin, sz, avg_px FROM positions").fetchall()
            return {r["coin"]: (float(r["sz"]), float(r["avg_px"])) for r in rows}

    def update_position(self, coin: str, sz: float, avg_px: float) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO positions(coin, sz, avg_px, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(coin) DO UPDATE SET sz=excluded.sz, avg_px=excluded.avg_px, "
                "updated_at=excluded.updated_at",
                (coin, sz, avg_px, now),
            )
