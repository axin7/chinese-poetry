from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from dotenv import find_dotenv, load_dotenv
except ModuleNotFoundError:  # pragma: no cover - 兼容未安装 python-dotenv 的环境
    def find_dotenv(filename: str, usecwd: bool = False) -> str:
        start = Path.cwd() if usecwd else Path(__file__).resolve().parent
        for directory in (start, *start.parents):
            candidate = directory / filename
            if candidate.exists():
                return str(candidate)
        return ""

    def load_dotenv(dotenv_path: str | Path, override: bool = False) -> None:
        path = Path(dotenv_path)
        if not path.exists():
            return
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if override or key not in os.environ:
                os.environ[key] = value

DEFAULT_SYSTEM_PROMPT = """请你扮演一名精通古典文学和现代汉语的语言专家，
将给定的中国古诗词按行逐句翻译为现代汉语，并对整首诗进行简要解读。

输入是一个古诗词句子列表，请你：

对带序号的每一句古诗，分别用现代汉语准确且流畅地翻译成一句话。
输出"translation"字段为以原文序号为键、现代汉语译文为值的 JSON 对象。
对整首诗作出简要的文化解读，涵盖意境、情感、历史背景或文化内涵，
输出"interpretation"字段。

translation 对象必须包含全部原文序号，且不得增加其他键。
不得拆分、合并或遗漏句子。
若原文仅有□、■或�等缺字符号，不得猜测或补写内容，必须明确说明原文缺失。

请严格返回以下 JSON 结构：
{
  "translation": {"1": "第一条原文的现代汉语译文"},
  "interpretation": "这里填写对整首诗的简要解读"
}
"""

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-4-flash"
DEFAULT_MAX_CONCURRENCY = 500
DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RECORD_FAILURES = 8
DEFAULT_DB_WRITE_BATCH_SIZE = 100

ENV_DEFAULTS = {
    "api_key": ("POETRY_API_KEY", ""),
    "model": ("POETRY_MODEL", DEFAULT_MODEL),
    "base_url": ("POETRY_BASE_URL", DEFAULT_BASE_URL),
    "max_concurrency": ("POETRY_MAX_CONCURRENCY", str(DEFAULT_MAX_CONCURRENCY)),
    "request_timeout": ("POETRY_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)),
    "max_retries": ("POETRY_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)),
    "max_record_failures": (
        "POETRY_MAX_RECORD_FAILURES",
        str(DEFAULT_MAX_RECORD_FAILURES),
    ),
    "db_write_batch_size": (
        "POETRY_DB_WRITE_BATCH_SIZE",
        str(DEFAULT_DB_WRITE_BATCH_SIZE),
    ),
}


def _load_environment(dotenv_path: Optional[Path]) -> None:
    if dotenv_path is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)
        return

    dotenv_file = find_dotenv(".env", usecwd=True)
    if dotenv_file:
        load_dotenv(dotenv_file, override=False)


def _config_values(overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    values = {
        name: os.getenv(environment_name, default)
        for name, (environment_name, default) in ENV_DEFAULTS.items()
    }
    values.update(
        {key: value for key, value in (overrides or {}).items() if value is not None}
    )
    return values


def _required_api_key(values: Dict[str, Any]) -> str:
    api_key = str(values["api_key"]).strip()
    if not api_key:
        raise ValueError("缺少 POETRY_API_KEY，请在 .env 或环境变量中配置")
    return api_key


def _concurrency_limit(value: Any) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > DEFAULT_MAX_CONCURRENCY:
        raise ValueError("POETRY_MAX_CONCURRENCY 必须在 1 到 500 之间")
    return parsed


def _positive_int(value: Any, environment_name: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{environment_name} 必须大于 0")
    return parsed


def _nonnegative_int(value: Any, environment_name: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{environment_name} 不能小于 0")
    return parsed


def _optional_text(value: Any, default: str) -> str:
    return str(value).strip() or default


@dataclass(frozen=True)
class TranslationConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    max_record_failures: int = DEFAULT_MAX_RECORD_FAILURES
    db_write_batch_size: int = DEFAULT_DB_WRITE_BATCH_SIZE
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @classmethod
    def load(
        cls,
        dotenv_path: Optional[Path] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> "TranslationConfig":
        _load_environment(dotenv_path)
        values = _config_values(overrides)
        return cls(
            api_key=_required_api_key(values),
            model=_optional_text(values["model"], DEFAULT_MODEL),
            base_url=_optional_text(values["base_url"], DEFAULT_BASE_URL),
            max_concurrency=_concurrency_limit(values["max_concurrency"]),
            request_timeout=_positive_int(
                values["request_timeout"], "POETRY_REQUEST_TIMEOUT"
            ),
            max_retries=_nonnegative_int(values["max_retries"], "POETRY_MAX_RETRIES"),
            max_record_failures=_positive_int(
                values["max_record_failures"], "POETRY_MAX_RECORD_FAILURES"
            ),
            db_write_batch_size=_positive_int(
                values["db_write_batch_size"], "POETRY_DB_WRITE_BATCH_SIZE"
            ),
        )
