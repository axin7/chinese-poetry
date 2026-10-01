#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""古诗词翻译脚本入口。"""

import argparse
import asyncio
import logging

from translation_pipeline.config import TranslationConfig
from translation_pipeline.runner import TranslationRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="翻译中国古典诗词")
    parser.add_argument("--db", type=str, default="chinese_poetry.db", help="数据库文件路径")
    parser.add_argument("--config", type=str, default="loader/datas.json", help="配置文件路径")
    parser.add_argument("--batch", type=int, default=50, help="单轮拉取记录数")
    parser.add_argument("--tables", nargs="+", default=None, help="仅处理指定表名")
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少条记录")
    parser.add_argument("--workers", type=int, default=None, help="最大并发数，默认从 .env 读取")
    parser.add_argument("--model", type=str, default=None, help="翻译模型，默认从 .env 读取")
    parser.add_argument("--base-url", type=str, default=None, help="接口基础地址，默认从 .env 读取")
    parser.add_argument("--timeout", type=int, default=None, help="请求超时时间，默认从 .env 读取")
    parser.add_argument(
        "--write-batch-size",
        type=int,
        default=None,
        help="SQLite 批量提交阈值，默认从 .env 读取",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("translation.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(args.log_level)
    config = TranslationConfig.load(
        overrides={
            "model": args.model,
            "base_url": args.base_url,
            "max_concurrency": args.workers,
            "request_timeout": args.timeout,
            "db_write_batch_size": args.write_batch_size,
        }
    )
    runner = TranslationRunner(
        db_path=args.db,
        config_path=args.config,
        config=config,
        batch_size=args.batch,
        selected_tables=args.tables,
        max_records=args.limit,
    )
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
