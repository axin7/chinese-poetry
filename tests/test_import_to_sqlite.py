import json
import sqlite3
from pathlib import Path

import pytest

import import_to_sqlite
from import_to_sqlite import get_dataset_files, import_dataset, load_json_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_songci_config_excludes_author_metadata():
    config = json.loads(
        (PROJECT_ROOT / "loader/datas.json").read_text(encoding="utf-8")
    )

    selected = get_dataset_files(
        config["datasets"]["songci"],
        str(PROJECT_ROOT),
    )

    assert "author.song.json" not in {Path(path).name for path in selected}


def test_import_rejects_records_without_required_content_field(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "authors.json").write_text(
        json.dumps([{"name": "苏轼", "description": "作者资料"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (source_dir / "poetry.json").write_text(
        json.dumps(
            [{"author": "苏轼", "paragraphs": ["明月几时有。"]}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")

    stats = import_dataset(
        connection,
        "songci",
        {"name": "宋词", "path": str(source_dir), "tag": "paragraphs"},
        str(tmp_path),
        force=True,
        show_progress=False,
    )

    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='songci'"
    ).fetchone()
    connection.close()

    assert stats["success"] is False
    assert "缺少正文字段 paragraphs" in stats["errors"][0]
    assert table_exists is None


def test_tangsong_config_excludes_duplicate_anthology():
    config = json.loads(
        (PROJECT_ROOT / "loader/datas.json").read_text(encoding="utf-8")
    )
    selected = get_dataset_files(
        config["datasets"]["tangsong"],
        str(PROJECT_ROOT),
    )
    original_ids = set()

    assert "唐诗三百首.json" not in {Path(path).name for path in selected}
    for file_path in selected:
        records = json.loads(Path(file_path).read_text(encoding="utf-8"))
        for record in records:
            original_id = record.get("id")
            if original_id is None:
                continue
            assert original_id not in original_ids
            original_ids.add(original_id)


def test_load_json_file_rejects_malformed_json(tmp_path):
    source = tmp_path / "broken.json"
    source.write_text("[{", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_json_file(str(source))


def test_load_json_file_recovers_content_embedded_in_title(tmp_path):
    source = tmp_path / "yuanqu.json"
    source.write_text(
        json.dumps(
            [
                {"title": "尾・孤灯照夜，客梦难成。", "paragraphs": []},
                {"title": "仙吕・点绛唇", "paragraphs": ["半世为人。"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dataset = {
        "tag": "paragraphs",
        "prepend_punctuated_title": True,
    }

    records = load_json_file(str(source), dataset)

    assert records[0]["paragraphs"] == ["尾・孤灯照夜，客梦难成。"]
    assert records[1]["paragraphs"] == ["半世为人。"]


def test_import_without_force_does_not_duplicate_existing_table(tmp_path):
    source = tmp_path / "poetry.json"
    source.write_text(
        json.dumps([{"content": ["关关雎鸠。"]}], ensure_ascii=False),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    dataset = {"name": "诗经", "path": str(source), "tag": "content"}

    first = import_dataset(
        connection,
        "shijing",
        dataset,
        str(tmp_path),
        force=True,
        show_progress=False,
    )
    second = import_dataset(
        connection,
        "shijing",
        dataset,
        str(tmp_path),
        show_progress=False,
    )
    row_count = connection.execute("SELECT COUNT(*) FROM shijing").fetchone()[0]
    connection.close()

    assert first["success"] is True
    assert second["success"] is False
    assert "已存在" in second["errors"][0]
    assert row_count == 1


def test_force_import_rolls_back_whole_table_on_late_failure(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name, content in (("a.json", "新记录一"), ("b.json", "新记录二")):
        (source_dir / name).write_text(
            json.dumps([{"content": [content]}], ensure_ascii=False),
            encoding="utf-8",
        )
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE shijing (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT)"
    )
    connection.execute("INSERT INTO shijing (content) VALUES ('旧记录')")
    connection.commit()
    original_insert = import_to_sqlite.insert_data_to_table
    call_count = 0

    def fail_second_insert(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("模拟第二个文件写入失败")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(import_to_sqlite, "insert_data_to_table", fail_second_insert)

    stats = import_dataset(
        connection,
        "shijing",
        {"name": "诗经", "path": str(source_dir), "tag": "content"},
        str(tmp_path),
        force=True,
        show_progress=False,
    )
    rows = connection.execute("SELECT content FROM shijing ORDER BY id").fetchall()
    connection.close()

    assert stats["success"] is False
    assert rows == [("旧记录",)]


def test_import_normalizes_nested_object_records(tmp_path):
    source = tmp_path / "anthology.json"
    source.write_text(
        json.dumps(
            {
                "title": "选集",
                "content": [
                    {
                        "type": "五言绝句",
                        "content": [
                            {
                                "chapter": "春晓",
                                "author": "孟浩然",
                                "paragraphs": ["春眠不觉晓", "处处闻啼鸟"],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    dataset = {
        "name": "选集",
        "path": str(source),
        "tag": "paragraphs",
        "object_records": {
            "path": ["content", "content"],
            "inherit": [
                {"title": "book_title"},
                {"type": "section"},
            ],
            "max_paragraphs": 80,
            "max_chars": 3000,
        },
    }

    stats = import_dataset(
        connection,
        "anthology",
        dataset,
        str(tmp_path),
        force=True,
        show_progress=False,
    )
    row = connection.execute(
        "SELECT book_title, section, chapter, author, paragraphs FROM anthology"
    ).fetchone()
    connection.close()

    assert stats["success"] is True
    assert stats["source_records"] == 1
    assert row == (
        "选集",
        "五言绝句",
        "春晓",
        "孟浩然",
        '["春眠不觉晓", "处处闻啼鸟"]',
    )


def test_import_splits_long_object_content_and_aligned_fields(tmp_path):
    source = tmp_path / "primer.json"
    source.write_text(
        json.dumps(
            {
                "title": "千字文",
                "paragraphs": ["天地玄黄", "宇宙洪荒", "日月盈昃"],
                "spells": ["tiandi", "yuzhou", "riyue"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    dataset = {
        "name": "千字文",
        "path": str(source),
        "tag": "paragraphs",
        "object_records": {
            "path": [],
            "max_paragraphs": 2,
            "max_chars": 3000,
            "aligned_fields": ["spells"],
        },
    }

    stats = import_dataset(
        connection,
        "primer",
        dataset,
        str(tmp_path),
        force=True,
        show_progress=False,
    )
    rows = connection.execute(
        "SELECT paragraphs, spells, content_part, content_part_count, "
        "translation_status, translation_retry_count "
        "FROM primer ORDER BY id"
    ).fetchall()
    connection.close()

    assert stats["success"] is True
    assert stats["source_records"] == 2
    assert rows == [
        ('["天地玄黄", "宇宙洪荒"]', '["tiandi", "yuzhou"]', 1, 2, "pending", 0),
        ('["日月盈昃"]', '["riyue"]', 2, 2, "pending", 0),
    ]


def test_all_object_datasets_are_normalized_within_configured_limits():
    config = json.loads(
        (PROJECT_ROOT / "loader/datas.json").read_text(encoding="utf-8")
    )
    object_datasets = {
        key: value
        for key, value in config["datasets"].items()
        if "object_records" in value
    }

    assert len(object_datasets) == 15
    for dataset in object_datasets.values():
        files = get_dataset_files(dataset, str(PROJECT_ROOT))
        records = load_json_file(files[0], dataset)
        tag = dataset["tag"]
        limits = dataset["object_records"]

        assert records
        assert all(0 < len(record[tag]) <= limits["max_paragraphs"] for record in records)
        assert all(
            sum(len(paragraph) for paragraph in record[tag]) <= limits["max_chars"]
            for record in records
        )
