import asyncio
import json
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from translate_poetry import build_parser
from translation_pipeline.config import TranslationConfig
from translation_pipeline.database import TranslationDatabase
from translation_pipeline.runner import TranslationRunner


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


async def async_noop():
    return None


def write_datasets_config(path, datasets):
    path.write_text(
        json.dumps({"datasets": datasets}, ensure_ascii=False),
        encoding="utf-8",
    )


def assert_selected_table_results(db_path):
    database = TranslationDatabase(db_path)
    database.connect()
    try:
        shijing_done = database.fetch_record("shijing", 1)
        shijing_pending = database.fetch_record("shijing", 2)
        lunyu_row = database.fetch_record("lunyu", 1)
        lunyu_columns = database.get_table_columns("lunyu")
        assert shijing_done["translation_status"] == "done"
        assert shijing_pending["translation_status"] is None
        assert "translation_status" not in lunyu_columns
        assert lunyu_row["paragraphs"] == '["学而时习之，不亦说乎。"]'
    finally:
        database.close()


async def run_streaming_scenario(runner, started_ids, events):
    events["first_record_started"] = asyncio.Event()
    events["release_first_record"] = asyncio.Event()
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(events["first_record_started"].wait(), timeout=1)

    await asyncio.wait_for(wait_for_third_record(started_ids), timeout=1)

    events["release_first_record"].set()
    await asyncio.wait_for(task, timeout=1)


async def wait_for_third_record(started_ids):
    while 3 not in started_ids:
        await asyncio.sleep(0.01)


def test_runner_cancels_pending_tasks_before_shutdown(tmp_path):
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
        config=TranslationConfig(api_key="dummy"),
        batch_size=1,
    )

    async def verify():
        task = asyncio.create_task(asyncio.sleep(60))
        started = time.perf_counter()
        await runner._cancel_in_flight_tasks({task})
        elapsed = time.perf_counter() - started
        assert task.cancelled()
        assert elapsed < 1

    asyncio.run(verify())


def test_runner_attempts_failed_record_only_once_per_run(tmp_path):
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
        config=TranslationConfig(api_key="dummy", max_concurrency=1),
        batch_size=1,
    )
    calls = []

    async def noop():
        return None

    async def fail(content, table_name, record_id):
        calls.append(record_id)
        raise RuntimeError("模拟请求失败")

    runner.client.open = noop
    runner.client.close = noop
    runner.client.translate = fail

    asyncio.run(runner.run())

    database = TranslationDatabase(db_path)
    database.connect()
    first = database.fetch_record("shijing", 1)
    second = database.fetch_record("shijing", 2)
    database.close()

    assert calls == [1, 2]
    assert first["translation_retry_count"] == 1
    assert second["translation_retry_count"] == 1


def test_runner_closes_resources_when_client_open_fails(tmp_path):
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
        config=TranslationConfig(api_key="dummy"),
        batch_size=1,
    )
    close_calls = 0

    async def fail_open():
        raise RuntimeError("客户端初始化失败")

    async def track_close():
        nonlocal close_calls
        close_calls += 1

    runner.client.open = fail_open
    runner.client.close = track_close

    with pytest.raises(RuntimeError, match="客户端初始化失败"):
        asyncio.run(runner.run())

    assert runner.database.conn is None
    assert close_calls == 1


def test_runner_rejects_selected_table_missing_from_config(tmp_path):
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
        config=TranslationConfig(api_key="dummy"),
        batch_size=1,
        selected_tables=["lunyu"],
    )

    async def noop():
        return None

    runner.client.open = noop
    runner.client.close = noop

    with pytest.raises(ValueError, match="未配置的表: lunyu"):
        asyncio.run(runner.run())

    assert runner.database.conn is None


def test_runner_rejects_selected_table_missing_from_database(tmp_path):
    db_path = tmp_path / "poetry.db"
    config_path = tmp_path / "datas.json"
    create_db(db_path)
    config_path.write_text(
        json.dumps(
            {"datasets": {"lunyu": {"name": "论语", "tag": "paragraphs"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runner = TranslationRunner(
        db_path=db_path,
        config_path=config_path,
        config=TranslationConfig(api_key="dummy"),
        batch_size=1,
        selected_tables=["lunyu"],
    )

    async def noop():
        return None

    runner.client.open = noop
    runner.client.close = noop

    with pytest.raises(ValueError, match="不存在于数据库: lunyu"):
        asyncio.run(runner.run())

    assert runner.database.conn is None


def test_runner_resets_processing_records_when_cancelled(tmp_path):
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
        config=TranslationConfig(api_key="dummy", max_concurrency=1),
        batch_size=1,
        max_records=1,
    )
    started = asyncio.Event()

    async def noop():
        return None

    async def wait_forever(content, table_name, record_id):
        started.set()
        await asyncio.Event().wait()

    runner.client.open = noop
    runner.client.close = noop
    runner.client.translate = wait_forever

    async def scenario():
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    database = TranslationDatabase(db_path)
    database.connect()
    row = database.fetch_record("shijing", 1)
    database.close()

    assert row["translation_status"] == "pending"


def test_cli_parser_accepts_tables_and_limit():
    args = build_parser().parse_args(["--tables", "shijing", "lunyu", "--limit", "1"])

    assert args.tables == ["shijing", "lunyu"]
    assert args.limit == 1


def test_runner_respects_selected_tables_and_limit(tmp_path):
    db_path = tmp_path / "poetry.db"
    config_path = tmp_path / "datas.json"
    create_multi_table_db(db_path)
    write_datasets_config(
        config_path,
        {
            "shijing": {"name": "诗经", "tag": "content"},
            "lunyu": {"name": "论语", "tag": "paragraphs"},
        },
    )

    config = TranslationConfig(
        api_key="dummy",
        max_concurrency=1,
        db_write_batch_size=1,
    )
    runner = TranslationRunner(
        db_path=db_path,
        config_path=config_path,
        config=config,
        batch_size=1,
        selected_tables=["shijing"],
        max_records=1,
    )

    async def fake_translate(content, table_name, record_id):
        class Outcome:
            status = "done"
            translation = [f"{table_name}-{record_id}-现代文"]
            interpretation = f"{table_name}-{record_id}-解读"

        return Outcome()

    runner.client.open = async_noop
    runner.client.close = async_noop
    runner.client.translate = fake_translate

    asyncio.run(runner.run())
    assert_selected_table_results(db_path)


def test_runner_refills_work_without_waiting_for_slowest_record(tmp_path):
    db_path = tmp_path / "poetry.db"
    config_path = tmp_path / "datas.json"
    create_streaming_db(db_path)
    write_datasets_config(
        config_path,
        {"shijing": {"name": "诗经", "tag": "content"}},
    )

    config = TranslationConfig(
        api_key="dummy",
        max_concurrency=2,
        db_write_batch_size=10,
    )
    runner = TranslationRunner(
        db_path=db_path,
        config_path=config_path,
        config=config,
        batch_size=2,
        selected_tables=["shijing"],
    )

    started_ids = []
    events = {}

    async def fake_translate(content, table_name, record_id):
        started_ids.append(record_id)
        if record_id == 1:
            events["first_record_started"].set()
            await events["release_first_record"].wait()

        await asyncio.sleep(0)

        class Outcome:
            status = "done"
            translation = [f"{table_name}-{record_id}-现代文"]
            interpretation = f"{table_name}-{record_id}-解读"

        return Outcome()

    runner.client.open = async_noop
    runner.client.close = async_noop
    runner.client.translate = fake_translate
    asyncio.run(run_streaming_scenario(runner, started_ids, events))

    assert 3 in started_ids
