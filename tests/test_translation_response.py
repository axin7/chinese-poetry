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


def test_client_normalizes_stringified_mapping_translation():
    config = TranslationConfig(api_key="dummy")
    client = TranslationClient(config)

    outcome = client._parse_response(
        json.dumps(
            {
                "translation": [
                    "{'甲句': '第一句现代文', '乙句': '第二句现代文'}"
                ],
                "interpretation": "整体解读",
            },
            ensure_ascii=False,
        ),
        source_content=["甲句", "乙句"],
    )

    assert outcome.status == "done"
    assert outcome.translation == ["第一句现代文", "第二句现代文"]
    assert outcome.interpretation == "整体解读"


def test_client_rejects_translation_line_count_mismatch():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {"translation": ["只翻译第一句"], "interpretation": "整体解读"},
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="译文条数"):
        client._parse_response(payload, source_content=["第一句", "第二句"])


def test_client_orders_numeric_mapping_translation_by_source_index():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {
            "translation": {"2": "第二句现代文", "1": "第一句现代文"},
            "interpretation": "整体解读",
        },
        ensure_ascii=False,
    )

    outcome = client._parse_response(payload, source_content=["甲句", "乙句"])

    assert outcome.translation == ["第一句现代文", "第二句现代文"]


def test_client_rejects_empty_translation_for_retry():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {"translation": [], "interpretation": "整体解读"},
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="有效译文"):
        client._parse_response(payload, source_content=["关关雎鸠"])


def test_client_rejects_translation_identical_to_source():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {"translation": ["关关雎鸠"], "interpretation": "整体解读"},
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="完全相同"):
        client._parse_response(payload, source_content=["关关雎鸠"])


def test_client_accepts_identical_punctuation_during_line_fallback():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {"translation": {"1": "。"}, "interpretation": "这是独立标点。"},
        ensure_ascii=False,
    )

    outcome = client._parse_response(payload, source_content=["。"])

    assert outcome.status == "done"
    assert outcome.translation == ["。"]


def test_client_rejects_hallucinated_placeholder_text():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {
            "translation": {"1": "空山新雨后，天气晚来秋。"},
            "interpretation": "整体解读",
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="缺字符号"):
        client._parse_response(payload, source_content=["□□□□□，□□□□□。"])


def test_client_accepts_explained_placeholder_text():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {
            "translation": {"1": "此处原文缺失，具体内容无法辨认。"},
            "interpretation": "原文仅存缺字符号。",
        },
        ensure_ascii=False,
    )

    outcome = client._parse_response(
        payload,
        source_content=["□□□□□，□□□□□。"],
    )

    assert outcome.translation == ["此处原文缺失，具体内容无法辨认。"]


def test_client_keeps_translation_of_editorial_missing_text_note():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {
            "translation": {"1": "原卷残缺的地方，原先依据其他四卷补全。"},
            "interpretation": "这是一条版本校勘说明。",
        },
        ensure_ascii=False,
    )

    outcome = client._parse_response(
        payload,
        source_content=["原卷殘缺處，元用甲乙丙丁四卷補之。"],
    )

    assert outcome.status == "done"


def test_client_accepts_literal_missing_text_marker():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {
            "translation": {"1": "原文残缺"},
            "interpretation": "这是校勘标记。",
        },
        ensure_ascii=False,
    )

    outcome = client._parse_response(payload, source_content=["原文殘缺"])

    assert outcome.status == "done"
    assert outcome.translation == ["原文残缺"]


def test_client_rejects_model_translation_refusal_for_retry():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {
            "translation": {"1": "无法翻译"},
            "interpretation": "原文无法解读。",
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="拒绝翻译"):
        client._parse_response(payload, source_content=["古文"])


def test_client_rejects_severely_truncated_translation():
    client = TranslationClient(TranslationConfig(api_key="dummy"))
    payload = json.dumps(
        {"translation": ["太短"], "interpretation": "整体解读"},
        ensure_ascii=False,
    )
    source = [
        "这是一段明显更长的古文原文，"
        "用来验证模型没有只返回极短的残缺译文。"
    ]

    with pytest.raises(ValueError, match="过短"):
        client._parse_response(payload, source_content=source)
