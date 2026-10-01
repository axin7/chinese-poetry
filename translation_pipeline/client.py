from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional

from .config import TranslationConfig
from .response import TranslationOutcome, is_punctuation_only, parse_response

logger = logging.getLogger(__name__)
MIN_HARD_TIMEOUT_SLACK_SECONDS = 0.05
MAX_HARD_TIMEOUT_SLACK_SECONDS = 1.0
TRANSLATION_TEMPERATURE = 0.1
MAX_CONTENT_LINES_PER_REQUEST = 8
MAX_CONTENT_CHARS_PER_REQUEST = 600

PERMANENT_SKIP_ERROR_PATTERNS = (
    "contentfilter",
    '"code": "1301"',
    "'code': '1301'",
    "不安全或敏感内容",
)
PERMANENT_SKIP_REASON_PREFIX = "上游接口因内容安全策略拒绝处理"
SINGLE_LINE_FALLBACK_ERROR_PATTERNS = (
    "translation 对象的序号与原文不一致",
    "译文条数与原文不一致",
    "模型未返回有效译文",
    "译文与原文完全相同",
    "译文相对原文过短",
    "模型返回拒绝翻译",
    "模型返回拒绝解读",
    "缺字符号行的译文未说明原文缺失",
)


def _split_content(
    content: List[str],
    max_lines: int = MAX_CONTENT_LINES_PER_REQUEST,
    max_chars: int = MAX_CONTENT_CHARS_PER_REQUEST,
) -> List[List[str]]:
    chunks: List[List[str]] = []
    current_chunk: List[str] = []
    current_chars = 0
    for line in content:
        line_chars = len(line)
        exceeds_limit = current_chunk and (
            len(current_chunk) >= max_lines
            or current_chars + line_chars > max_chars
        )
        if exceeds_limit:
            chunks.append(current_chunk)
            current_chunk = []
            current_chars = 0
        current_chunk.append(line)
        current_chars += line_chars
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _is_permanent_outcome_skip(outcome: TranslationOutcome) -> bool:
    reason = outcome.reason or ""
    return reason.startswith(PERMANENT_SKIP_REASON_PREFIX)


def _can_fallback_to_single_lines(error: Exception) -> bool:
    error_text = str(error)
    return any(
        pattern in error_text
        for pattern in SINGLE_LINE_FALLBACK_ERROR_PATTERNS
    )


def _build_user_prompt(content: List[str]) -> str:
    numbered_lines = "\n".join(
        f"{index}. {json.dumps(line, ensure_ascii=False)}"
        for index, line in enumerate(content, start=1)
    )
    required_keys = ", ".join(
        json.dumps(str(index), ensure_ascii=False)
        for index in range(1, len(content) + 1)
    )
    response_template = {
        "translation": {
            str(index): f"第{index}条的现代汉语译文"
            for index in range(1, len(content) + 1)
        },
        "interpretation": "整篇内容的简要解读",
    }
    return (
        f"原文共 {len(content)} 条，序号只用于对齐：\n"
        f"{numbered_lines}\n\n"
        "请逐条翻译，不得拆分、合并或遗漏。"
        "每条译文必须改写为现代汉语，不得原样照抄原文。"
        "即使原文接近现代汉语，也要替换古语、补全省略成分，"
        "并解释典故。"
        "标题、单字和符号行也必须单独返回对应译文。"
        "校勘说明、诗题、首句和残缺标记等编辑信息也要完整翻译，"
        "不得省略。"
        "罕见字、异体字和●等符号是原文内容，不得判定为残缺。"
        "若某行仅有□、■或�等缺字符号和标点，必须明确说明原文缺失，"
        "禁止猜测或补写内容。"
        f"translation 必须是 JSON 对象，且只包含键 {required_keys}。"
        "格式示例："
        f"{json.dumps(response_template, ensure_ascii=False)}"
    )


def _merge_outcomes(outcomes: List[TranslationOutcome]) -> TranslationOutcome:
    if len(outcomes) == 1:
        return outcomes[0]
    return TranslationOutcome(
        status="done",
        translation=[
            line
            for outcome in outcomes
            for line in outcome.translation
        ],
        interpretation="\n".join(
            outcome.interpretation for outcome in outcomes
        ),
    )


class TranslationClient:
    def __init__(self, config: TranslationConfig):
        self.config = config
        self._http_client: Optional[Any] = None
        self._client = None
        self._openai = None
        self._request_semaphore = asyncio.Semaphore(config.max_concurrency)

    async def open(self) -> None:
        if self._client is not None:
            return

        try:
            import httpx
        except ModuleNotFoundError as exc:  # pragma: no cover - 运行时依赖保护
            raise RuntimeError("未安装 httpx 依赖，无法执行翻译") from exc

        try:
            import openai
        except ModuleNotFoundError as exc:  # pragma: no cover - 运行时依赖保护
            raise RuntimeError("未安装 openai 依赖，无法执行翻译") from exc

        self._openai = openai

        self._http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=self.config.max_concurrency,
                max_keepalive_connections=max(1, self.config.max_concurrency // 2),
                keepalive_expiry=30.0,
            ),
            timeout=self.config.request_timeout,
        )
        self._client = openai.AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.request_timeout,
            max_retries=0,
            http_client=self._http_client,
        )

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._client = None

    async def translate(
        self,
        content: List[str],
        table_name: str,
        record_id: int,
    ) -> TranslationOutcome:
        if not content or all(not line.strip() for line in content):
            return TranslationOutcome(
                status="skipped",
                translation=[],
                interpretation="",
                reason="内容为空或仅包含空白字符",
            )
        if all(is_punctuation_only(line) for line in content):
            return TranslationOutcome(
                status="skipped",
                translation=[],
                interpretation="",
                reason="内容仅包含标点或符号",
            )

        await self.open()
        chunks = _split_content(content)
        outcomes = await self._translate_chunks(
            chunks,
            table_name,
            record_id,
        )
        skipped = next(
            (outcome for outcome in outcomes if outcome.status != "done"),
            None,
        )
        if skipped is not None:
            return skipped
        return _merge_outcomes(outcomes)

    async def _translate_chunks(
        self,
        chunks: List[List[str]],
        table_name: str,
        record_id: int,
    ) -> List[TranslationOutcome]:
        tasks = [
            asyncio.create_task(
                self._translate_indexed_chunk(
                    chunk,
                    table_name,
                    record_id,
                    chunk_index,
                    len(chunks),
                )
            )
            for chunk_index, chunk in enumerate(chunks, start=1)
        ]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _translate_indexed_chunk(
        self,
        content: List[str],
        table_name: str,
        record_id: int,
        chunk_index: int,
        chunk_count: int,
    ) -> TranslationOutcome:
        try:
            return await self._translate_chunk_with_fallback(
                content,
                table_name,
                record_id,
                chunk_index,
                chunk_count,
            )
        except RuntimeError as exc:
            if chunk_count == 1:
                raise
            raise RuntimeError(
                f"分块 {chunk_index}/{chunk_count} 处理失败: {exc}"
            ) from exc

    async def _translate_chunk_with_fallback(
        self,
        content: List[str],
        table_name: str,
        record_id: int,
        chunk_index: int,
        chunk_count: int,
    ) -> TranslationOutcome:
        try:
            outcome = await self._translate_chunk(
                content,
                table_name,
                record_id,
                str(chunk_index),
                chunk_count,
            )
        except RuntimeError as exc:
            if len(content) == 1 or not _can_fallback_to_single_lines(exc):
                raise
            return await self._translate_lines_individually(
                content, table_name, record_id, chunk_index, chunk_count
            )

        if outcome.status == "done" or _is_permanent_outcome_skip(outcome):
            return outcome
        if len(content) == 1:
            if chunk_count == 1:
                return outcome
            raise RuntimeError(
                f"分块 {chunk_index}/{chunk_count} 返回不可翻译: "
                f"{outcome.reason or '原因未知'}"
            )
        return await self._translate_lines_individually(
            content, table_name, record_id, chunk_index, chunk_count
        )

    async def _translate_lines_individually(
        self,
        content: List[str],
        table_name: str,
        record_id: int,
        chunk_index: int,
        chunk_count: int,
    ) -> TranslationOutcome:
        outcomes: List[TranslationOutcome] = []
        for line_index, line in enumerate(content, start=1):
            outcome = await self._translate_chunk(
                [line],
                table_name,
                record_id,
                f"{chunk_index}.{line_index}",
                chunk_count,
            )
            if outcome.status != "done":
                if _is_permanent_outcome_skip(outcome):
                    return outcome
                raise RuntimeError(
                    f"分块 {chunk_index}/{chunk_count} 第 {line_index} 行"
                    f"返回不可翻译: {outcome.reason or '原因未知'}"
                )
            outcomes.append(outcome)
        return _merge_outcomes(outcomes)

    async def _translate_chunk(
        self,
        content: List[str],
        table_name: str,
        record_id: int,
        chunk_index: str,
        chunk_count: int,
    ) -> TranslationOutcome:
        messages = self._build_messages(content)
        logger.debug(
            "发送翻译请求: 表=%s, ID=%s, 分块=%s/%s, 行数=%s",
            table_name,
            record_id,
            chunk_index,
            chunk_count,
            len(content),
        )

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                payload = await self._request_payload(messages)
                return self._parse_response(payload, source_content=content)
            except self._openai.APIError as exc:
                skip_reason = self._get_permanent_skip_reason(exc)
                if skip_reason is not None:
                    return TranslationOutcome(
                        status="skipped",
                        translation=[],
                        interpretation="",
                        reason=skip_reason,
                    )
                last_error = exc
            except self._openai.APIConnectionError as exc:
                last_error = exc
            except self._openai.APITimeoutError as exc:
                last_error = exc
            except asyncio.TimeoutError as exc:
                last_error = TimeoutError(
                    f"单次请求超过 {self.config.request_timeout}s 硬超时限制"
                )
            except ValueError as exc:
                last_error = exc
                messages = self._build_retry_messages(messages, exc)

            if attempt >= self.config.max_retries:
                break
            await asyncio.sleep(min(2 ** attempt, 8))

        raise RuntimeError(f"翻译请求失败: {last_error}")

    def _build_messages(self, content: List[str]) -> List[dict[str, str]]:
        return [
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": _build_user_prompt(content)},
        ]

    def _build_retry_messages(
        self,
        messages: List[dict[str, str]],
        error: ValueError,
    ) -> List[dict[str, str]]:
        error_text = str(error)
        if "完全相同" in error_text:
            instruction = (
                "上一次结果照抄了原文。请重新生成，每句都用完整的"
                "现代白话"
                "解释古语、典故和省略成分，译文不得与原文相同。"
            )
        elif "缺字符号" in error_text:
            instruction = (
                "上一次为缺字符号补写了不存在的内容。请明确说明该行"
                "原文缺失或无法辨认，禁止猜测。"
            )
        elif "过短" in error_text:
            instruction = (
                "上一次译文遗漏了信息。请重新生成并保留原文中的诗题、"
                "首句、人物、数字及校勘说明。"
            )
        else:
            instruction = (
                f"上一次输出未通过校验（{error_text}），"
                "请按格式重新生成。"
            )
        return [*messages, {"role": "user", "content": instruction}]

    async def _request_payload(self, messages: List[dict[str, str]]) -> str:
        async with self._request_semaphore:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self.config.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=TRANSLATION_TEMPERATURE,
                ),
                timeout=(
                    float(self.config.request_timeout)
                    + self._hard_timeout_slack_seconds()
                ),
            )
        return response.choices[0].message.content or ""

    def _parse_response(
        self,
        payload: str,
        source_content: Optional[List[str]] = None,
    ) -> TranslationOutcome:
        return parse_response(payload, source_content)

    def _get_permanent_skip_reason(self, error: Exception) -> str | None:
        error_text = str(error)
        lowered_error = error_text.lower()
        if any(pattern in lowered_error for pattern in PERMANENT_SKIP_ERROR_PATTERNS):
            return f"上游接口因内容安全策略拒绝处理: {error_text}"
        return None

    def _hard_timeout_slack_seconds(self) -> float:
        adaptive_slack = float(self.config.request_timeout) * 0.1
        return min(
            MAX_HARD_TIMEOUT_SLACK_SECONDS,
            max(MIN_HARD_TIMEOUT_SLACK_SECONDS, adaptive_slack),
        )
