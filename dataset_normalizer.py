from __future__ import annotations

from typing import Any, Dict, List, Tuple


SENTENCE_PUNCTUATION = frozenset("，。！？；")


def prepend_punctuated_title_content(
    records: List[Dict[str, Any]],
    content_field: str,
) -> List[Dict[str, Any]]:
    """Recover source text embedded in a title field."""
    normalized = []
    for record in records:
        title = record.get("title")
        content = record.get(content_field)
        if not isinstance(content, list):
            raise ValueError(f"正文字段 {content_field} 必须是列表")
        if not isinstance(title, str) or not any(
            mark in title for mark in SENTENCE_PUNCTUATION
        ):
            normalized.append(record)
            continue
        updated = dict(record)
        updated[content_field] = [title, *content]
        normalized.append(updated)
    return normalized


def normalize_object_records(
    data: Dict[str, Any],
    config: Dict[str, Any],
    content_field: str,
) -> List[Dict[str, Any]]:
    """Expand a nested object into records and split oversized content."""
    path = config.get("path", [])
    inherit = config.get("inherit", [])
    if not isinstance(path, list) or not all(isinstance(key, str) for key in path):
        raise ValueError("object_records.path 必须是字符串列表")
    if not isinstance(inherit, list):
        raise ValueError("object_records.inherit 必须是列表")

    contexts: List[Tuple[Dict[str, Any], Dict[str, Any]]] = [(data, {})]
    for depth, child_field in enumerate(path):
        field_mapping = inherit[depth] if depth < len(inherit) else {}
        if not isinstance(field_mapping, dict):
            raise ValueError("object_records.inherit 的每一项必须是对象")
        next_contexts = []
        for node, inherited in contexts:
            inherited_fields = _inherit_fields(node, inherited, field_mapping)
            children = node.get(child_field)
            if not isinstance(children, list):
                raise ValueError(f"对象字段 {child_field} 必须是列表")
            for child in children:
                if not isinstance(child, dict):
                    raise ValueError(f"对象字段 {child_field} 的元素必须是对象")
                next_contexts.append((child, inherited_fields))
        contexts = next_contexts

    records = []
    for leaf, inherited in contexts:
        record = dict(inherited)
        record.update(leaf)
        for expanded_record in _expand_content_records(record, content_field, config):
            records.extend(_split_record(expanded_record, content_field, config))
    return records


def _inherit_fields(
    node: Dict[str, Any],
    inherited: Dict[str, Any],
    field_mapping: Dict[str, str],
) -> Dict[str, Any]:
    result = dict(inherited)
    for source_field, target_field in field_mapping.items():
        if not isinstance(source_field, str) or not isinstance(target_field, str):
            raise ValueError("object_records.inherit 的字段名必须是字符串")
        if source_field in node:
            result[target_field] = node[source_field]
    return result


def _expand_content_records(
    record: Dict[str, Any],
    content_field: str,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    content = record.get(content_field)
    if not config.get("expand_content_records") or not isinstance(content, list):
        return [record]
    if not content or all(isinstance(item, str) for item in content):
        return [record]
    if not all(isinstance(item, dict) for item in content):
        raise ValueError(f"正文字段 {content_field} 的嵌套格式不一致")

    parent = {key: value for key, value in record.items() if key != content_field}
    expanded = []
    for item in content:
        child = dict(parent)
        child.update(item)
        expanded.append(child)
    return expanded


def _split_record(
    record: Dict[str, Any],
    content_field: str,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    content = record.get(content_field)
    if not isinstance(content, list) or not all(isinstance(item, str) for item in content):
        raise ValueError(f"正文字段 {content_field} 必须是字符串列表")

    max_paragraphs = _positive_int(config, "max_paragraphs")
    max_chars = _positive_int(config, "max_chars")
    aligned_fields = config.get("aligned_fields", [])
    if not isinstance(aligned_fields, list):
        raise ValueError("object_records.aligned_fields 必须是列表")
    _validate_aligned_fields(record, aligned_fields, len(content))

    ranges = _content_ranges(content, max_paragraphs, max_chars)
    part_count = len(ranges)
    parts = []
    for part_number, (start, end) in enumerate(ranges, start=1):
        part = dict(record)
        part[content_field] = content[start:end]
        for field in aligned_fields:
            part[field] = record[field][start:end]
        part["content_part"] = part_number
        part["content_part_count"] = part_count
        parts.append(part)
    return parts


def _positive_int(config: Dict[str, Any], field: str) -> int:
    value = config.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"object_records.{field} 必须是正整数")
    return value


def _validate_aligned_fields(
    record: Dict[str, Any],
    aligned_fields: List[str],
    content_length: int,
) -> None:
    for field in aligned_fields:
        value = record.get(field)
        if not isinstance(field, str) or not isinstance(value, list):
            raise ValueError("object_records.aligned_fields 必须引用列表字段")
        if len(value) != content_length:
            raise ValueError(f"对齐字段 {field} 与正文字段长度不一致")


def _content_ranges(
    content: List[str],
    max_paragraphs: int,
    max_chars: int,
) -> List[Tuple[int, int]]:
    if not content:
        return [(0, 0)]
    ranges = []
    start = 0
    chars = 0
    for index, paragraph in enumerate(content):
        would_overflow = index > start and (
            index - start >= max_paragraphs or chars + len(paragraph) > max_chars
        )
        if would_overflow:
            ranges.append((start, index))
            start = index
            chars = 0
        chars += len(paragraph)
    ranges.append((start, len(content)))
    return ranges
