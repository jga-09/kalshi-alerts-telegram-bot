"""
Subscription Manager - Handles user subscriptions to Kalshi markets
"""
import sqlite3
import logging
from typing import Set, Dict, List
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class SubscriptionManager:
    """Manages user subscriptions to market tickers"""

    def __init__(self, db_path: str = "subscriptions.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize the database with required tables"""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    chat_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, ticker)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ticker
                ON subscriptions(ticker)
            """)
            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def subscribe(self, chat_id: int, ticker: str) -> bool:
        """
        Subscribe a user to a ticker
        Returns True if newly subscribed, False if already subscribed
        """
        ticker = ticker.upper()
        with self._get_connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO subscriptions (chat_id, ticker) VALUES (?, ?)",
                    (chat_id, ticker)
                )
                conn.commit()
                logger.info(f"User {chat_id} subscribed to {ticker}")
                return True
            except sqlite3.IntegrityError:
                logger.info(f"User {chat_id} already subscribed to {ticker}")
                return False

    def unsubscribe(self, chat_id: int, ticker: str) -> bool:
        """
        Unsubscribe a user from a ticker
        Returns True if unsubscribed, False if not subscribed
        """
        ticker = ticker.upper()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM subscriptions WHERE chat_id = ? AND ticker = ?",
                (chat_id, ticker)
            )
            conn.commit()
            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"User {chat_id} unsubscribed from {ticker}")
            return deleted

    def get_user_subscriptions(self, chat_id: int) -> List[str]:
        """Get all tickers a user is subscribed to"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT ticker FROM subscriptions WHERE chat_id = ? ORDER BY ticker",
                (chat_id,)
            )
            return [row[0] for row in cursor.fetchall()]

    def get_subscribers(self, ticker: str) -> Set[int]:
        """Get all chat_ids subscribed to a ticker"""
        ticker = ticker.upper()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT chat_id FROM subscriptions WHERE ticker = ?",
                (ticker,)
            )
            return {row[0] for row in cursor.fetchall()}

    def get_all_tickers(self) -> Set[str]:
        """Get all tickers that have at least one subscriber"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT DISTINCT ticker FROM subscriptions"
            )
            return {row[0] for row in cursor.fetchall()}

    def get_subscription_count(self, chat_id: int) -> int:
        """Get the number of subscriptions for a user"""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ?",
                (chat_id,)
            )
            return cursor.fetchone()[0]
