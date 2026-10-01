from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Sequence

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

STATUS_COLUMNS = {
    "translation": "TEXT",
    "interpretation": "TEXT",
    "translation_status": "TEXT",
    "translation_updated_at": "TEXT",
    "translation_error": "TEXT",
    "translation_retry_count": "INTEGER DEFAULT 0",
}

SQLITE_BUSY_TIMEOUT_MS = 30_000
COMMIT_RETRY_DELAYS = (0.2, 0.5, 1.0)


@dataclass(frozen=True)
class PendingRecord:
    table_name: str
    record_id: int
    content: List[str]


class TranslationDatabase:
    def __init__(self, db_path: Path | str, write_batch_size: int = 100):
        self.db_path = Path(db_path)
        self.write_batch_size = write_batch_size
        self.conn: sqlite3.Connection | None = None
        self._pending_write_count = 0

    def connect(self) -> None:
        if not self.db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")

        self.conn = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.flush()
        except Exception:
            self.rollback()
            raise
        finally:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            self._pending_write_count = 0

    def rollback(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.rollback()
        finally:
            self._pending_write_count = 0

    def flush(self) -> None:
        if self.conn is not None and self._pending_write_count > 0:
            self._commit_with_retry()
            self._pending_write_count = 0

    def get_all_tables(self) -> List[str]:
        cursor = self._cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return [row["name"] for row in cursor.fetchall()]

    def get_table_columns(self, table_name: str) -> List[str]:
        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        cursor.execute(f"PRAGMA table_info({validated_table})")
        return [row["name"] for row in cursor.fetchall()]

    def ensure_translation_fields(self, table_name: str) -> None:
        validated_table = self._validate_identifier(table_name)
        if validated_table not in self.get_all_tables():
            raise ValueError(f"表不存在: {validated_table}")

        columns = set(self.get_table_columns(validated_table))
        cursor = self._cursor()
        for name, definition in STATUS_COLUMNS.items():
            if name not in columns:
                cursor.execute(
                    f"ALTER TABLE {validated_table} ADD COLUMN {name} {definition}"
                )
        self.conn.commit()
        self._pending_write_count = 0

    def count_all_records(self, table_name: str) -> int:
        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        cursor.execute(f"SELECT COUNT(*) AS count FROM {validated_table}")
        return int(cursor.fetchone()["count"])

    def count_pending_records(
        self,
        table_name: str,
        max_record_failures: int | None = None,
    ) -> int:
        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        if max_record_failures is not None:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM {validated_table}
                WHERE translation_status IS NULL
                   OR translation_status = ?
                   OR (translation_status = ?
                       AND COALESCE(translation_retry_count, 0) < ?)
                """,
                (STATUS_PENDING, STATUS_FAILED, max_record_failures),
            )
            return int(cursor.fetchone()["count"])
        cursor.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM {validated_table}
            WHERE translation_status IS NULL
               OR translation_status IN (?, ?)
            """,
            (STATUS_PENDING, STATUS_FAILED),
        )
        return int(cursor.fetchone()["count"])

    def get_pending_records(
        self,
        table_name: str,
        content_field: str,
        limit: int,
        max_record_failures: int | None = None,
        after_record_id: int = 0,
    ) -> List[PendingRecord]:
        validated_table = self._validate_identifier(table_name)
        validated_field = self._validate_identifier(content_field)
        rows = self._fetch_pending_rows(
            validated_table,
            validated_field,
            limit,
            max_record_failures,
            after_record_id,
        )
        return self._pending_records_from_rows(validated_table, validated_field, rows)

    def _fetch_pending_rows(
        self,
        table_name: str,
        content_field: str,
        limit: int,
        max_record_failures: int | None,
        after_record_id: int,
    ) -> List[sqlite3.Row]:
        cursor = self._cursor()
        query, parameters = self._pending_records_query(
            table_name,
            content_field,
            limit,
            max_record_failures,
            after_record_id,
        )
        cursor.execute(query, parameters)
        return cursor.fetchall()

    def _pending_records_query(
        self,
        table_name: str,
        content_field: str,
        limit: int,
        max_record_failures: int | None,
        after_record_id: int,
    ) -> tuple[str, tuple[object, ...]]:
        if max_record_failures is None:
            return self._unlimited_pending_query(
                table_name,
                content_field,
                limit,
                after_record_id,
            )
        return self._limited_pending_query(
            table_name,
            content_field,
            limit,
            max_record_failures,
            after_record_id,
        )

    def _unlimited_pending_query(
        self,
        table_name: str,
        content_field: str,
        limit: int,
        after_record_id: int,
    ) -> tuple[str, tuple[object, ...]]:
        return (
            f"""
            SELECT id, {content_field}
            FROM {table_name}
            WHERE id > ?
              AND (translation_status IS NULL
                   OR translation_status IN (?, ?))
            ORDER BY id
            LIMIT ?
            """,
            (after_record_id, STATUS_PENDING, STATUS_FAILED, limit),
        )

    def _limited_pending_query(
        self,
        table_name: str,
        content_field: str,
        limit: int,
        max_record_failures: int,
        after_record_id: int,
    ) -> tuple[str, tuple[object, ...]]:
        return (
            f"""
            SELECT id, {content_field}
            FROM {table_name}
            WHERE id > ?
              AND (translation_status IS NULL
                   OR translation_status = ?
                   OR (translation_status = ?
                       AND COALESCE(translation_retry_count, 0) < ?))
            ORDER BY id
            LIMIT ?
            """,
            (
                after_record_id,
                STATUS_PENDING,
                STATUS_FAILED,
                max_record_failures,
                limit,
            ),
        )

    def _pending_records_from_rows(
        self,
        table_name: str,
        content_field: str,
        rows: Sequence[sqlite3.Row],
    ) -> List[PendingRecord]:
        return [
            PendingRecord(
                table_name=table_name,
                record_id=row["id"],
                content=self._parse_content(row[content_field]),
            )
            for row in rows
        ]

    def fetch_record(self, table_name: str, record_id: int) -> sqlite3.Row | None:
        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        cursor.execute(
            f"SELECT * FROM {validated_table} WHERE id = ?",
            (record_id,),
        )
        return cursor.fetchone()

    def reset_processing_records(self, table_names: Sequence[str]) -> None:
        cursor = self._cursor()
        for table_name in table_names:
            validated_table = self._validate_identifier(table_name)
            cursor.execute(
                f"""
                UPDATE {validated_table}
                SET translation_status = ?,
                    translation_error = ?,
                    translation_updated_at = ?
                WHERE translation_status = ?
                """,
                (
                    STATUS_PENDING,
                    "上次任务中断，已回收为待处理",
                    self._now(),
                    STATUS_PROCESSING,
                ),
            )
            self._register_write()
        self.flush()

    def mark_processing(self, table_name: str, record_id: int) -> None:
        self._update_status(
            table_name,
            record_id,
            STATUS_PROCESSING,
            None,
            increment_retry=False,
        )

    def mark_done(
        self,
        table_name: str,
        record_id: int,
        translation: Sequence[str],
        interpretation: str,
    ) -> None:
        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        cursor.execute(
            f"""
            UPDATE {validated_table}
            SET translation = ?,
                interpretation = ?,
                translation_status = ?,
                translation_error = NULL,
                translation_updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(list(translation), ensure_ascii=False),
                interpretation,
                STATUS_DONE,
                self._now(),
                record_id,
            ),
        )
        self._register_write()

    def mark_failed(
        self,
        table_name: str,
        record_id: int,
        error: str,
        max_record_failures: int | None = None,
    ) -> None:
        if max_record_failures is None:
            self._update_status(
                table_name,
                record_id,
                STATUS_FAILED,
                error,
                increment_retry=True,
            )
            return

        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        now = self._now()
        cursor.execute(
            f"""
            UPDATE {validated_table}
            SET translation_status = ?,
                translation_error = CASE
                    WHEN COALESCE(translation_retry_count, 0) + 1 >= ?
                    THEN ?
                    ELSE ?
                END,
                translation_updated_at = ?,
                translation_retry_count = COALESCE(translation_retry_count, 0) + 1
            WHERE id = ?
            """,
            (
                STATUS_FAILED,
                max_record_failures,
                f"达到重试上限({max_record_failures})，停止自动重试: {error}",
                error,
                now,
                record_id,
            ),
        )
        self._register_write()

    def mark_skipped(self, table_name: str, record_id: int, reason: str) -> None:
        self._update_status(
            table_name,
            record_id,
            STATUS_SKIPPED,
            reason,
            increment_retry=False,
        )

    def _update_status(
        self,
        table_name: str,
        record_id: int,
        status: str,
        error: str | None,
        increment_retry: bool,
    ) -> None:
        validated_table = self._validate_identifier(table_name)
        cursor = self._cursor()
        assignments = [
            "translation_status = ?",
            "translation_error = ?",
            "translation_updated_at = ?",
        ]
        if increment_retry:
            assignments.append(
                "translation_retry_count = COALESCE(translation_retry_count, 0) + 1"
            )
        cursor.execute(
            f"""
            UPDATE {validated_table}
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            (status, error, self._now(), record_id),
        )
        self._register_write()

    def _cursor(self) -> sqlite3.Cursor:
        if self.conn is None:
            raise RuntimeError("数据库未连接")
        return self.conn.cursor()

    def _register_write(self) -> None:
        self._pending_write_count += 1
        if self._pending_write_count >= self.write_batch_size:
            self.flush()

    def _commit_with_retry(self) -> None:
        last_error: sqlite3.OperationalError | None = None
        for delay in (0.0, *COMMIT_RETRY_DELAYS):
            try:
                self.conn.commit()
                return
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                last_error = exc
                if delay > 0:
                    time.sleep(delay)
        raise last_error

    def _parse_content(self, content_raw: object) -> List[str]:
        if content_raw is None:
            return []

        if isinstance(content_raw, str):
            stripped = content_raw.strip()
            if not stripped:
                return []
            if stripped.startswith("[") and stripped.endswith("]"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [
                            str(item).strip()
                            for item in parsed
                            if item is not None and str(item).strip()
                        ]
                except json.JSONDecodeError:
                    pass
            return [stripped]

        if isinstance(content_raw, Iterable):
            return [
                str(item).strip()
                for item in content_raw
                if item is not None and str(item).strip()
            ]

        return [str(content_raw).strip()]

    def _validate_identifier(self, value: str) -> str:
        if not IDENTIFIER_PATTERN.match(value):
            raise ValueError(f"非法标识符: {value}")
        return value

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
