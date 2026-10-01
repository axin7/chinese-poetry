from __future__ import annotations

import ast
import json
import unicodedata
from dataclasses import dataclass
from typing import Any, List, Optional

TRANSLATION_REFUSALS = {
    "无法翻译",
    "无法译出",
    "原文残缺，无法翻译",
}
INTERPRETATION_REFUSAL_KEYWORDS = ("无法进行", "无法解读", "无法提供")
PLACEHOLDER_CHARACTERS = frozenset("□■�")
PLACEHOLDER_EXPLANATION_KEYWORDS = (
    "原文",
    "此处",
    "空白",
    "空缺",
    "缺失",
    "无法辨认",
    "省略",
    "缺字符号",
    "空格",
    "提供",
)


@dataclass(frozen=True)
class TranslationOutcome:
    status: str
    translation: List[str]
    interpretation: str
    reason: Optional[str] = None


def is_punctuation_only(text: str) -> bool:
    characters = [character for character in text if not character.isspace()]
    return bool(characters) and all(
        unicodedata.category(character)[0] in {"P", "S"}
        for character in characters
    )


def is_placeholder_only(text: str) -> bool:
    characters = [character for character in text if not character.isspace()]
    return any(character in PLACEHOLDER_CHARACTERS for character in characters) and all(
        character in PLACEHOLDER_CHARACTERS
        or unicodedata.category(character)[0] in {"P", "S"}
        for character in characters
    )


def _explains_placeholder(text: str) -> bool:
    return any(keyword in text for keyword in PLACEHOLDER_EXPLANATION_KEYWORDS)


def _is_translation_refusal(translation: List[str]) -> bool:
    return any(
        line.strip(" 。；;，,：（）()") in TRANSLATION_REFUSALS
        for line in translation
    )


def _is_interpretation_refusal(interpretation: str) -> bool:
    if len(interpretation) > 40:
        return False
    return any(
        keyword in interpretation
        for keyword in INTERPRETATION_REFUSAL_KEYWORDS
    )


def _extract_json(payload: str) -> dict:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        start_index = payload.find("{")
        end_index = payload.rfind("}")
        if start_index != -1 and end_index != -1 and start_index < end_index:
            return json.loads(payload[start_index : end_index + 1])
    raise ValueError("响应不是有效 JSON")


def _normalize_mapping_translation(translation: dict[Any, Any]) -> List[str]:
    items = list(translation.items())
    if items and all(str(key).isdigit() for key, _ in items):
        items.sort(key=lambda item: int(str(item[0])))
    return [
        str(value).strip()
        for _, value in items
        if value is not None and str(value).strip()
    ]


def _parse_mapping_string(item_text: str) -> dict[str, Any] | None:
    if not (item_text.startswith("{") and item_text.endswith("}")):
        return None
    try:
        parsed = json.loads(item_text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(item_text)
        except (ValueError, SyntaxError):
            return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_translation_list(translation: List[Any]) -> List[str]:
    normalized: List[str] = []
    for item in translation:
        if item is None:
            continue
        if isinstance(item, dict):
            normalized.extend(_normalize_mapping_translation(item))
            continue
        item_text = str(item).strip()
        if not item_text:
            continue
        mapping_value = _parse_mapping_string(item_text)
        if mapping_value is not None:
            normalized.extend(_normalize_mapping_translation(mapping_value))
            continue
        normalized.append(item_text)
    return normalized


def _normalize_translation(
    translation: Any,
    source_content: Optional[List[str]],
) -> List[str]:
    if isinstance(translation, str):
        translation = [translation]
    if isinstance(translation, dict):
        if source_content is not None:
            expected_keys = {
                str(index) for index in range(1, len(source_content) + 1)
            }
            actual_keys = {str(key) for key in translation}
            if actual_keys != expected_keys:
                raise ValueError("translation 对象的序号与原文不一致")
        translation = _normalize_mapping_translation(translation)
    if not isinstance(translation, list):
        raise ValueError("translation 字段必须为字符串数组")
    normalized = _normalize_translation_list(translation)
    if not normalized:
        raise ValueError("模型未返回有效译文")
    return normalized


def _validate_translation_quality(
    source_content: List[str],
    translation: List[str],
) -> None:
    if len(translation) != len(source_content):
        raise ValueError(
            f"译文条数与原文不一致: 原文 {len(source_content)} 条，"
            f"译文 {len(translation)} 条"
        )
    normalized_source = [line.strip() for line in source_content]
    for source_line, translated_line in zip(normalized_source, translation):
        if (
            is_placeholder_only(source_line)
            and source_line != translated_line
            and not _explains_placeholder(translated_line)
        ):
            raise ValueError("缺字符号行的译文未说明原文缺失")
    if normalized_source == translation and not all(
        is_punctuation_only(line) for line in normalized_source
    ):
        raise ValueError("译文与原文完全相同")
    source_length = sum(len("".join(line.split())) for line in normalized_source)
    translation_length = sum(len("".join(line.split())) for line in translation)
    if source_length and translation_length * 10 < source_length * 3:
        raise ValueError("译文相对原文过短，疑似内容缺失")


def parse_response(
    payload: str,
    source_content: Optional[List[str]] = None,
) -> TranslationOutcome:
    result = _extract_json(payload)
    translation = _normalize_translation(result.get("translation"), source_content)
    interpretation = str(result.get("interpretation", "")).strip()
    if _is_translation_refusal(translation):
        raise ValueError("模型返回拒绝翻译")
    if source_content is not None:
        _validate_translation_quality(source_content, translation)
    if not interpretation:
        raise ValueError("interpretation 字段为空")
    if _is_interpretation_refusal(interpretation):
        raise ValueError("模型返回拒绝解读")
    return TranslationOutcome(
        status="done",
        translation=translation,
        interpretation=interpretation,
    )
