from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from utils.logger import get_logger

logger = get_logger("storage")


class BrokerEvent(BaseModel):
    """
    Structured, validated model representing a non-sensitive trading or account event.
    """
    id: str = Field(..., description="Unique order ID or event identifier from the broker")
    broker: str = Field(default="Dhan", description="Broker name (e.g. Dhan, Zerodha)")
    event_type: str = Field(..., description="Type of event (Order Executed, Order Rejected, etc.)")
    symbol: str = Field(default="N/A", description="Scrip / Ticker symbol (e.g. RELIANCE)")
    order_type: Optional[str] = Field(default="N/A", description="BUY / SELL / INTRADAY / CNC")
    quantity: Optional[str] = Field(default="-", description="Quantity / Lot size")
    price: Optional[str] = Field(default="-", description="Executed or limit price")
    status: str = Field(..., description="Status string (Executed, Rejected, Cancelled, Traded, etc.)")
    time_str: Optional[str] = Field(default_factory=lambda: datetime.now().strftime("%I:%M:%S %p"), description="Time string")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional non-sensitive event details")

    @property
    def fingerprint(self) -> str:
        """
        Generates a deterministic SHA-256 fingerprint for duplicate detection.
        Combines broker, event_type, order ID, status, and symbol.
        If an order's status transitions (e.g. Pending -> Executed), the status change
        produces a new distinct fingerprint, ensuring the status update is delivered.
        """
        key_data = f"{self.broker.strip().lower()}|{self.id.strip()}|{self.event_type.strip().lower()}|{self.status.strip().lower()}|{self.symbol.strip().upper()}"
        return hashlib.sha256(key_data.encode("utf-8")).hexdigest()


class EventStore:
    """
    Thread-safe SQLite storage for tracking and deduplicating processed broker events.
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Enable WAL mode for high concurrency
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_events (
                        fingerprint TEXT PRIMARY KEY,
                        broker TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        order_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        order_type TEXT,
                        quantity TEXT,
                        price TEXT,
                        status TEXT NOT NULL,
                        time_str TEXT,
                        metadata TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notified_at TIMESTAMP,
                        notification_success INTEGER DEFAULT 0
                    );
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_events_created 
                    ON processed_events(created_at);
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_events_order_status 
                    ON processed_events(order_id, status);
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS market_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        price REAL NOT NULL,
                        previous_price REAL,
                        change_amount REAL,
                        change_percent REAL,
                        direction TEXT NOT NULL,
                        volume TEXT,
                        market_status TEXT,
                        source TEXT,
                        metadata TEXT
                    );
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_snapshots_sym_time 
                    ON market_snapshots(symbol, scanned_at);
                    """
                )
                conn.commit()
                logger.debug(f"EventStore initialized at {self.db_path}")

    def is_processed(self, fingerprint: str) -> bool:
        """
        Checks whether an event with the given fingerprint has already been recorded.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM processed_events WHERE fingerprint = ? LIMIT 1;",
                    (fingerprint,),
                )
                return cursor.fetchone() is not None

    def record_event(
        self,
        event: BrokerEvent,
        notified: bool = False,
        notification_success: bool = False,
    ) -> bool:
        """
        Inserts a newly detected event into the database.
        Returns True if inserted, or False if already existed.
        """
        fp = event.fingerprint
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                try:
                    now = datetime.now().isoformat() if notified else None
                    success_flag = 1 if notification_success else 0
                    cursor.execute(
                        """
                        INSERT INTO processed_events (
                            fingerprint, broker, event_type, order_id, symbol,
                            order_type, quantity, price, status, time_str,
                            metadata, notified_at, notification_success
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            fp,
                            event.broker,
                            event.event_type,
                            event.id,
                            event.symbol,
                            event.order_type,
                            str(event.quantity),
                            str(event.price),
                            event.status,
                            event.time_str,
                            json.dumps(event.metadata, ensure_ascii=False),
                            now,
                            success_flag,
                        ),
                    )
                    conn.commit()
                    logger.debug(f"Stored new event: [{event.broker}] {event.event_type} - {event.symbol} (fp: {fp[:8]}...)")
                    return True
                except sqlite3.IntegrityError:
                    # Already exists
                    return False
                except Exception as e:
                    logger.error(f"Error saving event to database: {e}")
                    return False

    def mark_notified(self, fingerprint: str, success: bool = True) -> None:
        """
        Updates the notification timestamp and success status for an event.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                now = datetime.now().isoformat()
                cursor.execute(
                    """
                    UPDATE processed_events
                    SET notified_at = ?, notification_success = ?
                    WHERE fingerprint = ?;
                    """,
                    (now, 1 if success else 0, fingerprint),
                )
                conn.commit()

    def get_recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Retrieves recent events from the database.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM processed_events
                    ORDER BY created_at DESC
                    LIMIT ?;
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]

    def count_events(self) -> int:
        """
        Returns the total number of recorded events.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM processed_events;")
                row = cursor.fetchone()
                return row[0] if row else 0

    def cleanup_old_events(self, days: int = 30) -> int:
        """
        Prunes records older than the specified retention days.
        Returns the number of deleted records.
        """
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    DELETE FROM processed_events
                    WHERE created_at < datetime('now', '-' || ? || ' days');
                    """,
                    (days,),
                )
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old event records (older than {days} days).")
                return deleted

    # --------------------------------------------------------------------------
    # Market Snapshots Persistence
    # --------------------------------------------------------------------------
    def save_market_snapshot(self, snap: Any) -> int:
        """Saves an individual StockSnapshot model into SQLite."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO market_snapshots (
                        scanned_at, symbol, name, price, previous_price,
                        change_amount, change_percent, direction, volume,
                        market_status, source, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        getattr(snap, "timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        snap.symbol,
                        snap.name,
                        snap.price,
                        snap.previous_price,
                        snap.change,
                        snap.change_percent,
                        snap.direction,
                        snap.volume,
                        snap.market_status,
                        snap.source,
                        json.dumps(getattr(snap, "metadata", {})),
                    ),
                )
                conn.commit()
                return cursor.lastrowid

    def save_market_snapshots_batch(self, snapshots: List[Any]) -> int:
        """Saves a collection of StockSnapshot models in a single transaction."""
        if not snapshots:
            return 0
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                rows = [
                    (
                        getattr(s, "timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        s.symbol,
                        s.name,
                        s.price,
                        s.previous_price,
                        s.change,
                        s.change_percent,
                        s.direction,
                        s.volume,
                        s.market_status,
                        s.source,
                        json.dumps(getattr(s, "metadata", {})),
                    )
                    for s in snapshots
                ]
                cursor.executemany(
                    """
                    INSERT INTO market_snapshots (
                        scanned_at, symbol, name, price, previous_price,
                        change_amount, change_percent, direction, volume,
                        market_status, source, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows,
                )
                conn.commit()
                return len(rows)

    def get_latest_market_snapshots(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves the most recent market snapshots."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM market_snapshots
                    ORDER BY id DESC
                    LIMIT ?;
                    """,
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]

    def get_symbol_history(self, symbol: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieves historical snapshots for a specific stock ticker."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM market_snapshots
                    WHERE symbol = ?
                    ORDER BY id DESC
                    LIMIT ?;
                    """,
                    (symbol.upper(), limit),
                )
                return [dict(row) for row in cursor.fetchall()]

