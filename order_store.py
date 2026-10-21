"""SQLite persistence for AKOSH VIDEO EDIT orders."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrderStore:
    """Small, connection-per-operation SQLite store for the Telegram bot."""

    _ORDER_FIELDS = {
        "username",
        "customer_name",
        "price_uzs",
        "custom_description",
        "receipt_file_id",
        "receipt_chat_id",
        "receipt_message_id",
        "source_type",
        "source_file_id",
        "source_chat_id",
        "source_message_id",
        "source_text",
        "final_file_id",
        "final_type",
        "final_chat_id",
        "final_message_id",
    }

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 20000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    customer_name TEXT NOT NULL,
                    service_code TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    price_uzs INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    custom_description TEXT,
                    receipt_file_id TEXT,
                    receipt_chat_id INTEGER,
                    receipt_message_id INTEGER,
                    source_type TEXT,
                    source_file_id TEXT,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    source_text TEXT,
                    final_file_id TEXT,
                    final_type TEXT,
                    final_chat_id INTEGER,
                    final_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            order_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(orders)").fetchall()
            }
            if "custom_description" not in order_columns:
                connection.execute(
                    "ALTER TABLE orders ADD COLUMN custom_description TEXT"
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS orders_user_created_idx
                ON orders(user_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS orders_status_created_idx
                ON orders(status, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_source_selection (
                    user_id INTEGER PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS custom_order_files (
                    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
                    telegram_file_id TEXT NOT NULL,
                    file_kind TEXT NOT NULL,
                    file_name TEXT,
                    mime_type TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_order(
        self,
        *,
        order_id: str,
        user_id: int,
        username: str | None,
        customer_name: str,
        service_code: str,
        service_name: str,
        price_uzs: int,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, username, customer_name, service_code,
                    service_name, price_uzs, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?)
                """,
                (
                    order_id,
                    user_id,
                    username,
                    customer_name,
                    service_code,
                    service_name,
                    price_uzs,
                    now,
                    now,
                ),
            )

    def create_custom_draft(
        self,
        *,
        order_id: str,
        user_id: int,
        username: str | None,
        customer_name: str,
    ) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, user_id, username, customer_name, service_code,
                    service_name, price_uzs, status, custom_description,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'custom', 'Boshqa xizmat', 0,
                          'custom_draft', '', ?, ?)
                """,
                (order_id, user_id, username, customer_name, now, now),
            )

    def append_custom_description(
        self, order_id: str, user_id: int, description: str
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET custom_description = CASE
                        WHEN COALESCE(custom_description, '') = '' THEN ?
                        ELSE custom_description || char(10) || ?
                    END,
                    updated_at = ?
                WHERE order_id = ? AND user_id = ? AND status = 'custom_draft'
                """,
                (
                    description,
                    description,
                    _now(),
                    order_id,
                    user_id,
                ),
            )
        return cursor.rowcount == 1

    def update_custom_draft_user(
        self,
        order_id: str,
        user_id: int,
        *,
        username: str | None,
        customer_name: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE orders
                SET username = ?, customer_name = ?, updated_at = ?
                WHERE order_id = ? AND user_id = ? AND status = 'custom_draft'
                """,
                (username, customer_name, _now(), order_id, user_id),
            )

    def add_custom_file(
        self,
        order_id: str,
        *,
        telegram_file_id: str,
        file_kind: str,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO custom_order_files (
                    order_id, telegram_file_id, file_kind, file_name, mime_type,
                    created_at
                )
                SELECT ?, ?, ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM orders
                    WHERE order_id = ? AND status = 'custom_draft'
                )
                """,
                (
                    order_id,
                    telegram_file_id,
                    file_kind,
                    file_name,
                    mime_type,
                    _now(),
                    order_id,
                ),
            )
        return cursor.rowcount == 1

    def custom_files(self, order_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM custom_order_files
                WHERE order_id = ?
                ORDER BY file_id
                """,
                (order_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return self._as_dict(row)

    def transition(
        self,
        order_id: str,
        *,
        expected_statuses: Sequence[str],
        new_status: str,
        updates: Mapping[str, Any] | None = None,
    ) -> bool:
        """Change state only when the order is still in an expected state."""
        if not expected_statuses:
            return False

        update_values = dict(updates or {})
        unknown_fields = set(update_values) - self._ORDER_FIELDS
        if unknown_fields:
            raise ValueError(f"Unsupported order fields: {sorted(unknown_fields)}")

        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [new_status, _now()]
        for field, value in update_values.items():
            assignments.append(f"{field} = ?")
            values.append(value)

        placeholders = ", ".join("?" for _ in expected_statuses)
        values.extend([order_id, *expected_statuses])
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE orders
                SET {", ".join(assignments)}
                WHERE order_id = ? AND status IN ({placeholders})
                """,
                values,
            )
        return cursor.rowcount == 1

    def latest_user_order(
        self, user_id: int, statuses: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM orders WHERE user_id = ?"
        values: list[Any] = [user_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            values.extend(statuses)
        sql += " ORDER BY created_at DESC LIMIT 1"
        with self._connection() as connection:
            row = connection.execute(sql, values).fetchone()
        return self._as_dict(row)

    def user_orders_with_status(
        self, user_id: int, status: str
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE user_id = ? AND status = ?
                ORDER BY created_at DESC
                """,
                (user_id, status),
            ).fetchall()
        return [dict(row) for row in rows]

    def user_orders(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def admin_orders(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE status IN (
                    'payment_review', 'source_received', 'awaiting_final_video',
                    'custom_review', 'awaiting_custom_acceptance', 'custom_paid'
                )
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_active_final_order(self, order_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_settings (setting_key, setting_value)
                VALUES ('active_final_order_id', ?)
                ON CONFLICT(setting_key) DO UPDATE
                SET setting_value = excluded.setting_value
                """,
                (order_id,),
            )

    def active_final_order(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT orders.*
                FROM bot_settings
                JOIN orders
                  ON orders.order_id = bot_settings.setting_value
                WHERE bot_settings.setting_key = 'active_final_order_id'
                  AND orders.status = 'awaiting_final_video'
                """
            ).fetchone()
        return self._as_dict(row)

    def clear_active_final_order(self, order_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM bot_settings
                WHERE setting_key = 'active_final_order_id'
                  AND setting_value = ?
                """,
                (order_id,),
            )

    def set_active_custom_quote_order(self, order_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO bot_settings (setting_key, setting_value)
                VALUES ('active_custom_quote_order_id', ?)
                ON CONFLICT(setting_key) DO UPDATE
                SET setting_value = excluded.setting_value
                """,
                (order_id,),
            )

    def active_custom_quote_order(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT orders.*
                FROM bot_settings
                JOIN orders ON orders.order_id = bot_settings.setting_value
                WHERE bot_settings.setting_key = 'active_custom_quote_order_id'
                  AND orders.status = 'custom_review'
                """
            ).fetchone()
        return self._as_dict(row)

    def clear_active_custom_quote_order(self, order_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM bot_settings
                WHERE setting_key = 'active_custom_quote_order_id'
                  AND setting_value = ?
                """,
                (order_id,),
            )

    def set_active_source_order(self, user_id: int, order_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_source_selection (user_id, order_id)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE
                SET order_id = excluded.order_id
                """,
                (user_id, order_id),
            )

    def active_source_order(self, user_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT orders.*
                FROM user_source_selection
                JOIN orders ON orders.order_id = user_source_selection.order_id
                WHERE user_source_selection.user_id = ?
                  AND orders.user_id = ?
                  AND orders.status = 'awaiting_source'
                """,
                (user_id, user_id),
            ).fetchone()
        return self._as_dict(row)

    def clear_active_source_order(
        self, user_id: int, order_id: str | None = None
    ) -> None:
        with self._connection() as connection:
            if order_id is None:
                connection.execute(
                    "DELETE FROM user_source_selection WHERE user_id = ?",
                    (user_id,),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM user_source_selection
                    WHERE user_id = ? AND order_id = ?
                    """,
                    (user_id, order_id),
                )
