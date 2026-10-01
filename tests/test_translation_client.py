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


def test_client_skips_permanent_content_filter_error():
    config = TranslationConfig(api_key="dummy", max_retries=3)
    client = TranslationClient(config)

    class FakeAPIError(Exception):
        pass

    class FakeCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            raise FakeAPIError(
                "Error code: 400 - {'contentFilter': [{'level': 1, 'role': 'user'}], "
                "'error': {'code': '1301', "
                "'message': '系统检测到输入或生成内容可能包含"
                "不安全或敏感内容'"
                "}}"
            )

    completions = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client._openai = SimpleNamespace(
        APIError=FakeAPIError,
        APIConnectionError=RuntimeError,
        APITimeoutError=TimeoutError,
    )

    async def fake_open():
        return None

    client.open = fake_open

    outcome = asyncio.run(client.translate(["测试内容"], "tangsong", 1))

    assert outcome.status == "skipped"
    assert "内容安全" in outcome.reason
    assert completions.calls == 1


def test_client_limits_single_attempt_wall_time():
    config = TranslationConfig(
        api_key="dummy",
        request_timeout=0.01,
        max_retries=0,
    )
    client = TranslationClient(config)

    class FakeCompletions:
        async def create(self, **kwargs):
            await asyncio.sleep(60)

    client._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    client._openai = SimpleNamespace(
        APIError=RuntimeError,
        APIConnectionError=RuntimeError,
        APITimeoutError=TimeoutError,
    )

    async def fake_open():
        return None

    client.open = fake_open

    started = time.perf_counter()
    with pytest.raises(RuntimeError, match="翻译请求失败"):
        asyncio.run(
            asyncio.wait_for(
                client.translate(["测试内容"], "tangsong", 1),
                timeout=0.2,
            )
        )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2


def test_client_disables_sdk_builtin_retries(monkeypatch):
    config = TranslationConfig(api_key="dummy", max_concurrency=12, max_retries=3)
    client = TranslationClient(config)
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "httpx",
        SimpleNamespace(
            AsyncClient=FakeAsyncClient,
            Limits=lambda **kwargs: kwargs,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI),
    )

    asyncio.run(client.open())

    assert captured["max_retries"] == 0


def test_user_prompt_numbers_each_source_line():
    prompt = _build_user_prompt(["甲句", "乙句"])

    assert "原文共 2 条" in prompt
    assert '1. "甲句"' in prompt
    assert '2. "乙句"' in prompt
    assert "每条译文必须改写为现代汉语，不得原样照抄原文" in prompt
    expected_detail_prompt = (
        "即使原文接近现代汉语，也要替换古语、补全省略成分，"
        "并解释典故"
    )
    assert expected_detail_prompt in prompt
    assert "标题、单字和符号行也必须单独返回对应译文" in prompt
    assert (
        "校勘说明、诗题、首句和残缺标记等编辑信息也要完整翻译"
        in prompt
    )
    assert "罕见字、异体字和●等符号是原文内容，不得判定为残缺" in prompt
    assert 'translation 必须是 JSON 对象，且只包含键 "1", "2"' in prompt
    assert '"interpretation": "整篇内容的简要解读"' in prompt


def test_split_content_preserves_lines_and_limits_chunk_size():
    content = [f"第{index}句" for index in range(1, 26)]

    chunks = _split_content(content, max_lines=20, max_chars=30)

    assert [line for chunk in chunks for line in chunk] == content
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert all(sum(len(line) for line in chunk) <= 30 for chunk in chunks)


def test_client_translates_long_content_in_chunks_and_merges_results():
    config = TranslationConfig(api_key="dummy", max_retries=0)
    client = TranslationClient(config)
    content = [f"古文第{index}句" for index in range(1, 22)]

    class FakeCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            line_count = (8, 8, 5)[self.calls - 1]
            payload = {
                "translation": {
                    str(index): f"第{self.calls}块第{index}句现代文"
                    for index in range(1, line_count + 1)
                },
                "interpretation": f"第{self.calls}部分解读",
            }
            message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client._openai = SimpleNamespace(
        APIError=RuntimeError,
        APIConnectionError=ConnectionError,
        APITimeoutError=TimeoutError,
    )

    async def fake_open():
        return None

    client.open = fake_open
    outcome = asyncio.run(client.translate(content, "tangsong", 1))

    assert completions.calls == 3
    assert len(outcome.translation) == len(content)
    assert outcome.translation[0] == "第1块第1句现代文"
    assert outcome.translation[-1] == "第3块第5句现代文"
    assert outcome.interpretation == "第1部分解读\n第2部分解读\n第3部分解读"


def test_client_limits_concurrent_chunk_requests():
    client = TranslationClient(
        TranslationConfig(api_key="dummy", max_concurrency=2, max_retries=0)
    )

    class FakeCompletions:
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def create(self, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                prompt = kwargs["messages"][-1]["content"]
                count = int(prompt.split("原文共 ", 1)[1].split(" 条", 1)[0])
                payload = {
                    "translation": {
                        str(index): f"第{index}句现代文"
                        for index in range(1, count + 1)
                    },
                    "interpretation": "分块解读",
                }
                message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])
            finally:
                self.active -= 1

    completions = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client._openai = SimpleNamespace(
        APIError=RuntimeError,
        APIConnectionError=ConnectionError,
        APITimeoutError=TimeoutError,
    )

    async def fake_open():
        return None

    client.open = fake_open
    outcome = asyncio.run(client.translate(["古文"] * 17, "tangsong", 1))

    assert len(outcome.translation) == 17
    assert completions.max_active == 2


def test_client_retries_model_skip_from_one_chunk_of_long_content():
    client = TranslationClient(TranslationConfig(api_key="dummy", max_retries=0))

    class FakeCompletions:
        async def create(self, **kwargs):
            payload = {
                "translation": {
                    str(index): "无法翻译" if index == 1 else f"第{index}句现代文"
                    for index in range(1, 9)
                },
                "interpretation": "当前片段无法解读",
            }
            message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    client._openai = SimpleNamespace(
        APIError=RuntimeError,
        APIConnectionError=ConnectionError,
        APITimeoutError=TimeoutError,
    )

    async def fake_open():
        return None

    client.open = fake_open

    with pytest.raises(RuntimeError, match="分块 1/2 处理失败"):
        asyncio.run(client.translate(["古文"] * 9, "wenzimengqiu", 1))


def test_client_falls_back_to_single_lines_when_chunk_alignment_fails():
    client = TranslationClient(TranslationConfig(api_key="dummy", max_retries=0))

    class FakeCompletions:
        def __init__(self):
            self.calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                payload = {
                    "translation": {"1": "不完整结果"},
                    "interpretation": "不完整解读",
                }
            else:
                payload = {
                    "translation": {"1": f"第{self.calls - 1}句现代文"},
                    "interpretation": f"第{self.calls - 1}句解读",
                }
            message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client._openai = SimpleNamespace(
        APIError=RuntimeError,
        APIConnectionError=ConnectionError,
        APITimeoutError=TimeoutError,
    )

    async def fake_open():
        return None

    client.open = fake_open
    outcome = asyncio.run(client.translate(["古文"] * 8, "wenzimengqiu", 1))

    assert completions.calls == 9
    assert outcome.translation == [f"第{index}句现代文" for index in range(1, 9)]


def test_client_corrects_identical_translation_on_retry():
    client = TranslationClient(TranslationConfig(api_key="dummy", max_retries=1))

    class FakeCompletions:
        def __init__(self):
            self.messages = []

        async def create(self, **kwargs):
            self.messages.append(kwargs["messages"])
            translation = "关关雎鸠" if len(self.messages) == 1 else "雎鸠鸟相互鸣叫。"
            payload = {
                "translation": {"1": translation},
                "interpretation": "以雎鸠起兴。",
            }
            message = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client._openai = SimpleNamespace(
        APIError=RuntimeError,
        APIConnectionError=ConnectionError,
        APITimeoutError=TimeoutError,
    )

    outcome = asyncio.run(client._translate_chunk(["关关雎鸠"], "shijing", 1, "1", 1))

    assert outcome.translation == ["雎鸠鸟相互鸣叫。"]
    assert "上一次结果照抄了原文" in completions.messages[1][-1]["content"]


def test_client_skips_record_containing_only_punctuation_without_request():
    client = TranslationClient(TranslationConfig(api_key="dummy"))

    outcome = asyncio.run(client.translate(["。"], "tangsong", 1))

    assert outcome.status == "skipped"
    assert outcome.reason == "内容仅包含标点或符号"
