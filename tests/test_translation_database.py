import asyncio
import json
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from translate_poetry import build_parser
from translation_pipeline.client import (
    TranslationClient,
    _build_user_prompt,
    _split_content,
)
from translation_pipeline.config import TranslationConfig
from translation_pipeline.database import TranslationDatabase
from translation_pipeline.runner import (
    AdaptiveConcurrencyController,
    ProgressReporter,
    TranslationRunner,
    _is_load_failure,
)


def create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE shijing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO shijing (content) VALUES (?)",
        [
            ('["关关雎鸠，在河之洲。"]',),
            ('["蒹葭苍苍，白露为霜。"]',),
            (None,),
        ],
    )
    conn.commit()
    conn.close()


def create_multi_table_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE shijing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE lunyu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paragraphs TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO shijing (content) VALUES (?)",
        [
            ('["关关雎鸠，在河之洲。"]',),
            ('["蒹葭苍苍，白露为霜。"]',),
        ],
    )
    conn.executemany(
        "INSERT INTO lunyu (paragraphs) VALUES (?)",
        [
            ('["学而时习之，不亦说乎。"]',),
            ('["有朋自远方来，不亦乐乎。"]',),
        ],
    )
    conn.commit()
    conn.close()


def create_streaming_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE shijing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO shijing (content) VALUES (?)",
        [
            ('["甲"]',),
            ('["乙"]',),
            ('["丙"]',),
            ('["丁"]',),
        ],
    )
    conn.commit()
    conn.close()


def test_config_reads_values_from_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "POETRY_API_KEY=test-key",
                "POETRY_MODEL=test-model",
                "POETRY_BASE_URL=https://example.com/v1",
                "POETRY_MAX_CONCURRENCY=500",
                "POETRY_REQUEST_TIMEOUT=42",
                "POETRY_MAX_RECORD_FAILURES=9",
                "POETRY_DB_WRITE_BATCH_SIZE=25",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("POETRY_API_KEY", raising=False)

    config = TranslationConfig.load(dotenv_path=env_path)

    assert config.api_key == "test-key"
    assert config.model == "test-model"
    assert config.base_url == "https://example.com/v1"
    assert config.max_concurrency == 500
    assert config.request_timeout == 42
    assert config.max_record_failures == 9
    assert config.db_write_batch_size == 25


def test_config_requires_api_key(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.delenv("POETRY_API_KEY", raising=False)

    with pytest.raises(ValueError):
        TranslationConfig.load(dotenv_path=env_path)


def test_ensure_translation_fields_adds_status_columns(tmp_path):
    db_path = tmp_path / "poetry.db"
    create_db(db_path)

    database = TranslationDatabase(db_path)
    database.connect()
    database.ensure_translation_fields("shijing")

    columns = database.get_table_columns("shijing")

    assert "translation" in columns
    assert "interpretation" in columns
    assert "translation_status" in columns
    assert "translation_updated_at" in columns
    assert "translation_error" in columns
    assert "translation_retry_count" in columns
    database.close()


def test_mark_skipped_keeps_original_record(tmp_path):
    db_path = tmp_path / "poetry.db"
    create_db(db_path)

    database = TranslationDatabase(db_path)
    database.connect()
    database.ensure_translation_fields("shijing")
    database.mark_skipped("shijing", 3, "内容为空")

    row = database.fetch_record("shijing", 3)

    assert row is not None
    assert row["translation_status"] == "skipped"
    assert row["translation_error"] == "内容为空"
    database.close()


def test_pending_records_skip_done_and_processing(tmp_path):
    db_path = tmp_path / "poetry.db"
    create_db(db_path)

    database = TranslationDatabase(db_path)
    database.connect()
    database.ensure_translation_fields("shijing")
    database.mark_done("shijing", 1, ["现代解释"], "整首诗的解释")
    database.mark_processing("shijing", 2)
    database.mark_failed("shijing", 3, "请求失败")

    pending_before_reset = database.get_pending_records("shijing", "content", limit=10)
    pending_ids_before_reset = {record.record_id for record in pending_before_reset}
    assert 1 not in pending_ids_before_reset
    assert 2 not in pending_ids_before_reset
    assert 3 in pending_ids_before_reset

    database.reset_processing_records(["shijing"])
    pending_after_reset = database.get_pending_records("shijing", "content", limit=10)
    pending_ids_after_reset = {record.record_id for record in pending_after_reset}

    assert pending_ids_after_reset == {2, 3}
    database.close()


def test_flush_retries_when_database_is_temporarily_locked():
    class FakeConnection:
        def __init__(self):
            self.commit_calls = 0

        def commit(self):
            self.commit_calls += 1
            if self.commit_calls == 1:
                raise sqlite3.OperationalError("database is locked")

    database = TranslationDatabase("dummy.db")
    database.conn = FakeConnection()
    database._pending_write_count = 1

    database.flush()

    assert database.conn.commit_calls == 2
    assert database._pending_write_count == 0


def test_runner_recovers_processing_record_after_commit_lock(tmp_path, monkeypatch):
    db_path = tmp_path / "poetry.db"
    config_path = tmp_path / "datas.json"
    create_db(db_path)
    config_path.write_text(
        json.dumps(
            {"datasets": {"shijing": {"name": "诗经", "tag": "content"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runner = TranslationRunner(
        db_path=db_path,
        config_path=config_path,
        config=TranslationConfig(api_key="dummy", db_write_batch_size=100),
        batch_size=1,
    )
    original_commit = runner.database._commit_with_retry
    attempts = 0

    def fail_second_commit():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise sqlite3.OperationalError("database is locked")
        original_commit()

    async def noop():
        return None

    monkeypatch.setattr(runner.database, "_commit_with_retry", fail_second_commit)
    runner.client.open = noop
    runner.client.close = noop

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        asyncio.run(runner.run())

    database = TranslationDatabase(db_path)
    database.connect()
    pending = database.get_pending_records("shijing", "content", limit=10)
    database.close()

    assert {record.record_id for record in pending} == {1, 2, 3}


def test_close_releases_transaction_when_final_flush_is_locked(tmp_path, monkeypatch):
    db_path = tmp_path / "poetry.db"
    create_db(db_path)
    database = TranslationDatabase(db_path, write_batch_size=100)
    database.connect()
    database.ensure_translation_fields("shijing")
    database.mark_processing("shijing", 1)
    monkeypatch.setattr(
        database,
        "_commit_with_retry",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )

    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            database.close()
        assert database.conn is None
    finally:
        if database.conn is not None:
            database.conn.rollback()
            database.conn.close()
            database.conn = None


def test_mark_failed_stops_record_after_reaching_failure_limit(tmp_path):
    db_path = tmp_path / "poetry.db"
    create_db(db_path)

    database = TranslationDatabase(db_path)
    database.connect()
    database.ensure_translation_fields("shijing")

    database.mark_failed("shijing", 1, "请求失败", max_record_failures=2)
    first = database.fetch_record("shijing", 1)
    assert first["translation_status"] == "failed"
    assert first["translation_retry_count"] == 1

    database.mark_failed("shijing", 1, "再次失败", max_record_failures=2)
    second = database.fetch_record("shijing", 1)
    assert second["translation_status"] == "failed"
    assert second["translation_retry_count"] == 2
    assert "达到重试上限" in second["translation_error"]

    pending = database.get_pending_records(
        "shijing",
        "content",
        limit=10,
        max_record_failures=2,
    )

    assert 1 not in {record.record_id for record in pending}
    database.close()
