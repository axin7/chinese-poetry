#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
脚本名称: import_to_sqlite.py
功能: 将中国古典诗词导入到SQLite数据库
作者: AI助手
创建日期: 2023-06-24

导入说明:
- 每个诗词集合将被导入到独立的表中
- 每个表自动包含原始字段结构
- 每个表都添加translation和interpretation字段
- 允许用户选择性导入特定诗词集合

使用方法:
1. 基本用法: python import_to_sqlite.py
2. 导入特定数据集: python import_to_sqlite.py --datasets shijing lunyu
3. 列出所有可用数据集: python import_to_sqlite.py --list
4. 强制重建表: python import_to_sqlite.py --force
5. 指定配置文件: python import_to_sqlite.py --config /path/to/config.json
6. 指定数据库文件: python import_to_sqlite.py --db /path/to/database.db
7. 指定字段信息文件: python import_to_sqlite.py --fields-file /path/to/fields.json

注意事项:
- translation和interpretation字段会自动添加到每个表中，用于后续翻译和注释
- 如果数据集中的数据结构不一致，可能会导致导入失败
- 可以使用--force选项强制重建所有表，但会导致原有数据丢失
- 字段信息会保存到字段信息文件中，用于记录每个表的字段结构
"""

import json
import os
import sqlite3
import argparse
import logging
import sys
import time
import datetime
import traceback
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional, Set

from dataset_normalizer import (
    normalize_object_records,
    prepend_punctuated_title_content,
)

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False
    print("提示: 安装tqdm包可以获得进度条显示功能 (pip install tqdm)")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("import_to_sqlite.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# 默认配置文件路径
DEFAULT_CONFIG_PATH = "./loader/datas.json"
# 默认数据库文件路径
DEFAULT_DB_PATH = "./chinese_poetry.db"
# 默认每批插入的数据量
DEFAULT_BATCH_SIZE = 1000
# 默认字段信息文件路径
DEFAULT_FIELDS_FILE = "./table_fields.json"
TRANSLATION_FIELD_TYPES = {
    "translation": "TEXT",
    "interpretation": "TEXT",
    "translation_status": "TEXT DEFAULT 'pending'",
    "translation_updated_at": "TEXT",
    "translation_error": "TEXT",
    "translation_retry_count": "INTEGER DEFAULT 0",
}


def normalize_table_name(table_name: str) -> str:
    """规范化表名，确保可安全用于 SQLite。"""
    if table_name.isalnum() or table_name.replace("_", "").isalnum():
        return table_name

    safe_table_name = "".join(c if c.isalnum() or c == "_" else "_" for c in table_name)
    logger.warning(f"表名 '{table_name}' 含有非法字符，已更改为 '{safe_table_name}'")
    return safe_table_name


def normalize_field_name(field_name: str, rename_id: bool = True,
                         log_level: int = logging.DEBUG) -> str:
    """统一字段名规范化逻辑，避免建表和插入阶段出现不一致。"""
    normalized_name = field_name

    if rename_id and normalized_name == 'id':
        normalized_name = 'original_id'
        logger.log(log_level, "将原始id字段重命名为original_id")

    if not normalized_name.isalnum() and not normalized_name.replace("_", "").isalnum():
        safe_field_name = "".join(
            c if c.isalnum() or c == "_" else "_" for c in normalized_name
        )
        logger.log(
            log_level,
            f"字段名 '{normalized_name}' 含有非法字符，已更改为 '{safe_field_name}'"
        )
        normalized_name = safe_field_name

    if normalized_name and normalized_name[0].isdigit():
        safe_field_name = "f_" + normalized_name
        logger.log(
            log_level,
            f"字段名 '{normalized_name}' 以数字开头，已更改为 '{safe_field_name}'"
        )
        normalized_name = safe_field_name

    return normalized_name


def infer_sqlite_field_type(field_value: Any) -> str:
    """根据值推断 SQLite 字段类型。"""
    if isinstance(field_value, str):
        return "TEXT"
    if isinstance(field_value, bool):
        return "INTEGER"
    if isinstance(field_value, (int, float)):
        return "NUMERIC"
    if isinstance(field_value, (list, dict)):
        return "TEXT"
    return "TEXT"


def collect_normalized_fields(
    items: List[Dict],
    rename_id: bool,
) -> Tuple[List[str], Dict[str, str]]:
    """按出现顺序收集规范化字段，返回字段列表和原始字段映射。"""
    ordered_fields: List[str] = []
    field_mapping: Dict[str, str] = {}
    seen_fields = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        for original_name in item.keys():
            normalized_name = normalize_field_name(
                original_name,
                rename_id=rename_id,
                log_level=logging.DEBUG
            )
            if normalized_name != original_name:
                field_mapping[original_name] = normalized_name
            if normalized_name not in seen_fields:
                seen_fields.add(normalized_name)
                ordered_fields.append(normalized_name)

    return ordered_fields, field_mapping

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='将中国古典诗词导入到SQLite数据库')
    parser.add_argument(
        '--config',
        type=str,
        default=DEFAULT_CONFIG_PATH,
        help=f'配置文件路径，默认为{DEFAULT_CONFIG_PATH}'
    )
    parser.add_argument(
        '--db',
        type=str,
        default=DEFAULT_DB_PATH,
        help=f'SQLite数据库文件路径，默认为{DEFAULT_DB_PATH}'
    )
    parser.add_argument(
        '--datasets',
        type=str,
        nargs='*',
        help='要导入的数据集名称，不提供则导入所有数据集'
    )
    parser.add_argument(
        '--list',
        action='store_true',
        help='列出所有可用的数据集，不执行导入'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='如果表已存在，则覆盖它们'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f'每批插入的数据量，默认为{DEFAULT_BATCH_SIZE}'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='显示详细日志信息'
    )
    parser.add_argument(
        '--fields-file',
        type=str,
        default=DEFAULT_FIELDS_FILE,
        help=f'字段信息文件路径，默认为{DEFAULT_FIELDS_FILE}'
    )
    parser.add_argument(
        '--no-progress',
        action='store_true',
        help='禁用进度条显示'
    )
    parser.add_argument(
        '--create-indexes',
        action='store_true',
        help='为主要字段创建索引以提高查询性能'
    )
    parser.add_argument(
        '--stats',
        action='store_true',
        help='显示导入完成后的统计信息'
    )
    parser.add_argument(
        '--fast-import',
        action='store_true',
        help='启用快速导入模式，仅适用于本地全量重建'
    )
    parser.add_argument(
        '--verify-counts',
        action='store_true',
        help='导入完成后校验源 JSON 条数与 SQLite 表记录数是否一致'
    )
    return parser.parse_args()

def load_config(config_path: str) -> Dict:
    """加载配置文件"""
    if not os.path.exists(config_path):
        logger.error(f"配置文件不存在: {config_path}")
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # 验证配置文件格式
        if "datasets" not in config:
            logger.error("配置文件中缺少'datasets'字段")
            raise ValueError("配置文件格式不正确: 缺少'datasets'字段")

        return config
    except json.JSONDecodeError as e:
        logger.error(f"配置文件格式不正确: {e}")
        raise
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        raise

def load_table_fields_file(file_path: str) -> Dict:
    """
    加载表字段信息文件，如果不存在则创建新的

    Args:
        file_path: 文件路径

    Returns:
        表字段信息字典
    """
    # 确保文件目录存在
    file_dir = os.path.dirname(os.path.abspath(file_path))
    if file_dir and not os.path.exists(file_dir):
        os.makedirs(file_dir, exist_ok=True)
        logger.debug(f"创建目录: {file_dir}")

    # 如果文件存在则加载，否则返回空字典
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                fields_data = json.load(f)
            logger.debug(f"已加载字段信息文件: {file_path}")
            return fields_data
        except json.JSONDecodeError as e:
            logger.warning(f"字段信息文件格式不正确: {e}，将创建新文件")
            return {}
        except Exception as e:
            logger.warning(f"加载字段信息文件失败: {e}，将创建新文件")
            return {}
    else:
        logger.debug(f"字段信息文件不存在，将创建新文件: {file_path}")
        return {}

def save_table_fields(file_path: str, table_name: str, field_types: Dict[str, str],
                     added_fields: List[str], has_original_id: bool = False):
    """
    保存表字段信息到文件

    Args:
        file_path: 字段信息文件路径
        table_name: 表名
        field_types: 字段类型字典 {字段名: 字段类型}
        added_fields: 添加的字段列表
        has_original_id: 是否包含原始id字段
    """
    # 加载现有字段信息
    fields_data = load_table_fields_file(file_path)

    # 更新表字段信息
    fields_data[table_name] = {
        "fields": field_types,
        "added_fields": added_fields,
        "has_original_id": has_original_id,
        "creation_time": datetime.datetime.now().isoformat(),
        "last_updated": datetime.datetime.now().isoformat()
    }

    # 保存到文件
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(fields_data, f, ensure_ascii=False, indent=2)
        logger.info(f"已保存表 {table_name} 的字段信息到文件: {file_path}")
    except Exception as e:
        logger.error(f"保存表字段信息失败: {e}")
        logger.error(traceback.format_exc())

def create_connection(db_path: str, fast_import: bool = False) -> sqlite3.Connection:
    """创建SQLite数据库连接"""
    try:
        # 确保数据库目录存在
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        conn = sqlite3.connect(db_path)
        # 启用外键约束
        conn.execute("PRAGMA foreign_keys = ON")
        if fast_import:
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.execute("PRAGMA cache_size = -65536")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        # 设置连接以支持中文
        conn.text_factory = str
        return conn
    except sqlite3.Error as e:
        logger.error(f"创建数据库连接失败: {e}")
        raise

def close_connection(conn: sqlite3.Connection):
    """关闭SQLite数据库连接"""
    if conn:
        try:
            conn.close()
            logger.debug("数据库连接已关闭")
        except sqlite3.Error as e:
            logger.error(f"关闭数据库连接失败: {e}")

def list_available_datasets(config: Dict):
    """列出所有可用的数据集"""
    datasets = config.get("datasets", {})
    if not datasets:
        logger.warning("配置文件中没有找到数据集")
        return

    print("\n可用的数据集列表:")
    print("-" * 80)
    print(f"{'数据集ID':<8} {'数据集键名':<20} {'数据集名称':<30} {'路径':<30}")
    print("-" * 80)

    for key, dataset in datasets.items():
        dataset_id = dataset.get("id", "N/A")
        dataset_name = dataset.get("name", "未命名")
        dataset_path = dataset.get("path", "未知")
        print(f"{dataset_id:<8} {key:<20} {dataset_name:<30} {dataset_path:<30}")

    print("-" * 80)
    print(f"总计: {len(datasets)} 个数据集")
    print("\n使用示例:")
    print(f"  导入所有数据集: python {sys.argv[0]}")
    print(
        f"  导入特定数据集: python {sys.argv[0]} "
        "--datasets 数据集键名1 数据集键名2"
    )
    print(f"  强制重建表: python {sys.argv[0]} --force")
    print()

def load_json_file(
    file_path: str,
    dataset_config: Optional[Dict[str, Any]] = None,
) -> List[Dict]:
    """加载JSON文件内容"""
    if not os.path.exists(file_path):
        logger.error(f"文件不存在: {file_path}")
        raise FileNotFoundError(f"文件不存在: {file_path}")

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        object_config = (dataset_config or {}).get("object_records")
        if isinstance(data, dict) and object_config is not None:
            return normalize_object_records(
                data,
                object_config,
                (dataset_config or {}).get("tag", ""),
            )

        if not isinstance(data, list):
            logger.error(f"文件内容不是列表格式: {file_path}")
            raise ValueError(f"文件内容不是列表格式: {file_path}")

        if (dataset_config or {}).get("prepend_punctuated_title"):
            return prepend_punctuated_title_content(
                data,
                (dataset_config or {}).get("tag", ""),
            )

        return data
    except json.JSONDecodeError as e:
        logger.error(f"解析JSON文件失败 {file_path}: {e}")
        raise
    except Exception as e:
        logger.error(f"加载JSON文件失败 {file_path}: {e}")
        raise

def analyze_json_structure(data: List[Dict]) -> Tuple[Dict[str, str], bool]:
    """
    分析JSON数据结构并返回字段结构和是否包含原始id字段

    Returns:
        Tuple[Dict[str, str], bool]: (字段类型字典, 是否包含原始id字段)
    """
    if not data or not isinstance(data, list) or not data[0] or not isinstance(data[0], dict):
        logger.error("无法分析JSON结构: 数据为空或格式不正确")
        return {}, False

    # 收集所有条目的字段
    field_types: Dict[str, str] = {}
    has_id_field = False

    # 遍历所有数据条目，确保捕获所有可能的字段
    for item in data:
        if not isinstance(item, dict):
            continue

        # 检查是否有id字段
        if 'id' in item and not has_id_field:
            has_id_field = True

        # 遍历当前条目的所有字段
        for original_field_name, field_value in item.items():
            field_name = normalize_field_name(
                original_field_name,
                rename_id=True,
                log_level=logging.DEBUG
            )

            # 如果字段已经处理过，跳过
            if field_name in field_types:
                continue

            # 确定字段类型
            field_types[field_name] = infer_sqlite_field_type(field_value)

    # 如果数据量太大，只分析前100条记录
    max_items_to_check = min(100, len(data))
    logger.debug(f"分析了 {max_items_to_check} 条记录，发现 {len(field_types)} 个字段")

    return field_types, has_id_field


def analyze_dataset_structure(
    file_paths: List[str],
    dataset_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, str], bool]:
    """分析整个数据集的字段并集，避免跨文件字段不一致导致导入失败"""
    merged_field_types: Dict[str, str] = {}
    has_original_id = False

    for file_path in file_paths:
        data = load_json_file(file_path, dataset_config)
        if not data:
            continue

        field_types, file_has_original_id = analyze_json_structure(data)
        if file_has_original_id:
            has_original_id = True

        for field_name, field_type in field_types.items():
            existing_type = merged_field_types.get(field_name)
            if existing_type is None:
                merged_field_types[field_name] = field_type
            elif existing_type != field_type:
                merged_field_types[field_name] = "TEXT"

    return merged_field_types, has_original_id

def create_table_if_not_exists(conn: sqlite3.Connection, table_name: str,
                              field_types: Dict[str, str], force: bool = False,
                              fields_file_path: str = None, create_indexes: bool = False,
                              has_original_id: bool = False,
                              commit: bool = True):
    """
    创建表（如果不存在）

    Args:
        conn: SQLite连接
        table_name: 表名
        field_types: 字段类型字典 {字段名: 字段类型}
        force: 如果表已存在，是否强制删除并重新创建
        fields_file_path: 字段信息文件路径，如果提供则保存字段信息
        create_indexes: 是否为主要字段创建索引
        has_original_id: 数据是否已包含id字段(已重命名为original_id)
    """
    cursor = conn.cursor()

    table_name = normalize_table_name(table_name)

    # 如果需要强制重建表
    if force:
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
        logger.info(f"已删除表 {table_name}")

    # 检查表是否存在
    cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
    table_exists = cursor.fetchone() is not None

    if table_exists and not force:
        logger.info(f"表 {table_name} 已存在，跳过创建")
        return

    # 构建字段定义
    field_definitions = []
    field_definitions.append("id INTEGER PRIMARY KEY AUTOINCREMENT")

    # 保存原始字段名和类型，用于后续保存字段信息
    original_field_types = field_types.copy()
    # 需要创建索引的字段
    index_fields = []

    # 创建一个新的字段类型字典，包含处理过的字段名
    processed_field_types = {}

    for field_name, field_type in field_types.items():
        original_field_name = field_name
        field_name = normalize_field_name(
            field_name,
            rename_id=False,
            log_level=logging.WARNING,
        )

        # 将处理过的字段名添加到新字典中
        processed_field_types[field_name] = field_type

        # 如果字段名发生了变化，更新original_field_types
        if field_name != original_field_name:
            original_field_types[field_name] = original_field_types.pop(original_field_name)

        field_definitions.append(f"{field_name} {field_type}")

        # 为特定字段标记创建索引
        indexed_fields = {'title', 'author', 'chapter', 'section', 'original_id'}
        if create_indexes and field_name in indexed_fields:
            index_fields.append(field_name)

    added_fields = list(TRANSLATION_FIELD_TYPES)
    field_definitions.extend(
        f"{field_name} {field_type}"
        for field_name, field_type in TRANSLATION_FIELD_TYPES.items()
    )

    # 构建CREATE TABLE语句
    create_table_sql = f"CREATE TABLE {table_name} ({', '.join(field_definitions)})"

    # 执行创建表操作
    try:
        cursor.execute(create_table_sql)

        # 创建索引
        if create_indexes and index_fields:
            for field_name in index_fields:
                index_name = f"idx_{table_name}_{field_name}"
                create_index_sql = (
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name}({field_name})"
                )
                try:
                    cursor.execute(create_index_sql)
                    logger.info(f"为表 {table_name} 的 {field_name} 字段创建索引")
                except sqlite3.Error as e:
                    logger.warning(
                        f"为表 {table_name} 的 {field_name} 字段创建索引失败: {e}"
                    )

        if commit:
            conn.commit()
        logger.info(f"成功创建表 {table_name}")

        # 如果提供了字段信息文件路径，则保存字段信息
        if fields_file_path:
            save_table_fields(
                fields_file_path,
                table_name,
                original_field_types,
                added_fields,
                has_original_id,
            )
    except sqlite3.Error as e:
        logger.error(f"创建表 {table_name} 失败: {e}")
        conn.rollback()
        raise

def get_dataset_files(dataset_config: Dict, base_path: str) -> List[str]:
    """
    获取数据集中的所有文件路径

    Args:
        dataset_config: 数据集配置
        base_path: 基础路径

    Returns:
        文件路径列表
    """
    file_paths = []
    path = dataset_config.get("path", "")
    if not path:
        logger.warning("数据集配置中缺少'path'字段")
        return []

    full_path = os.path.join(base_path, path)
    excludes = dataset_config.get("excludes", [])

    # 检查路径是否存在
    if not os.path.exists(full_path):
        logger.warning(f"路径不存在: {full_path}")
        return []

    if os.path.isfile(full_path):
        # 如果是单个文件
        if full_path.endswith('.json'):
            file_paths.append(full_path)
        else:
            logger.warning(f"非JSON文件: {full_path}，已跳过")
    elif os.path.isdir(full_path):
        # 如果是目录，获取目录下所有文件
        for file_name in os.listdir(full_path):
            if file_name in excludes:
                logger.debug(f"排除文件: {file_name}")
                continue

            file_path = os.path.join(full_path, file_name)
            if os.path.isfile(file_path) and file_path.endswith('.json'):
                file_paths.append(file_path)

    logger.info(f"找到 {len(file_paths)} 个JSON文件")
    return sorted(file_paths)


def validate_dataset_files(
    file_paths: List[str],
    required_field: str,
    dataset_config: Optional[Dict[str, Any]] = None,
) -> None:
    """在建表前确认所有源记录都包含配置声明的正文字段。"""
    if not required_field:
        raise ValueError("数据集配置缺少 tag 字段")

    for file_path in file_paths:
        data = load_json_file(file_path, dataset_config)
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{file_path} 第 {index} 条记录不是对象")
            if required_field not in item:
                raise ValueError(
                    f"{file_path} 第 {index} 条记录缺少正文字段 {required_field}"
                )


def count_source_records(
    file_paths: List[str],
    dataset_config: Optional[Dict[str, Any]] = None,
) -> int:
    """统计源 JSON 文件中的总记录数"""
    total = 0
    for file_path in file_paths:
        total += len(load_json_file(file_path, dataset_config))
    return total


def count_table_records(conn: sqlite3.Connection, table_name: str) -> int:
    """统计表中的总记录数"""
    cursor = conn.cursor()
    cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
    result = cursor.fetchone()
    return int(result[0]) if result else 0


def database_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """检查业务表是否已经存在。"""
    result = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (normalize_table_name(table_name),),
    ).fetchone()
    return result is not None


def verify_dataset_counts(conn: sqlite3.Connection, datasets: Dict[str, Dict],
                          base_path: str,
                          selected_datasets: Optional[List[str]] = None
                          ) -> Tuple[List[Dict[str, Any]], bool]:
    """严格校验源 JSON 条数与 SQLite 表记录数是否一致"""
    dataset_keys = selected_datasets or list(datasets.keys())
    verification_results = []
    all_matched = True

    print("\n严格计数校验:")
    print("-" * 100)
    print(
        f"{'数据集键名':<20} {'数据集名称':<20} {'源记录数':<12} "
        f"{'表记录数':<12} {'结果':<8}"
    )
    print("-" * 100)

    for dataset_key in dataset_keys:
        dataset_config = datasets.get(dataset_key)
        if not dataset_config:
            result = {
                "dataset_key": dataset_key,
                "dataset_name": "",
                "source_records": 0,
                "table_records": 0,
                "matched": False,
                "error": "数据集不存在"
            }
            verification_results.append(result)
            all_matched = False
            print(f"{dataset_key:<20} {'未知':<20} {0:<12} {0:<12} {'失败':<8}")
            continue

        table_name = dataset_key.replace("-", "_")
        file_paths = get_dataset_files(dataset_config, base_path)
        source_records = count_source_records(file_paths, dataset_config)
        table_records = count_table_records(conn, table_name)
        matched = source_records == table_records
        if not matched:
            all_matched = False

        result = {
            "dataset_key": dataset_key,
            "dataset_name": dataset_config.get("name", ""),
            "source_records": source_records,
            "table_records": table_records,
            "matched": matched,
            "error": ""
        }
        verification_results.append(result)
        status = "通过" if matched else "失败"
        print(f"{dataset_key:<20} {result['dataset_name']:<20} "
              f"{source_records:<12} {table_records:<12} {status:<8}")

    print("-" * 100)
    print(f"严格计数校验结果: {'全部通过' if all_matched else '存在不一致'}")
    print("-" * 100)
    return verification_results, all_matched

def prepare_value_for_db(value: Any) -> Any:
    """
    准备值以存入数据库

    Args:
        value: 原始值

    Returns:
        处理后的值
    """
    if value is None:
        return None

    if isinstance(value, (list, dict)):
        # 将列表和字典序列化为JSON字符串
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception as e:
            logger.error(f"序列化值失败: {e}, 值: {value}")
            return str(value)

    return value

def insert_data_to_table(conn: sqlite3.Connection, table_name: str, data: List[Dict],
                        batch_size: int = DEFAULT_BATCH_SIZE,
                        has_original_id: bool = False,
                        show_progress: bool = True,
                        fast_import: bool = False,
                        manage_transaction: bool = True):
    """
    将数据插入到指定表中

    Args:
        conn: SQLite连接
        table_name: 表名
        data: 要插入的数据
        batch_size: 每批插入的数据量
        has_original_id: 是否包含原始id字段(已重命名)
        show_progress: 是否显示进度条
    """
    if not data:
        logger.warning(f"没有数据可插入到表 {table_name}")
        return

    cursor = conn.cursor()
    table_name = normalize_table_name(table_name)
    fields, field_mapping = collect_normalized_fields(data, rename_id=has_original_id)

    # 应用字段名映射到数据中
    if field_mapping:
        for item in data:
            for original_name, new_name in field_mapping.items():
                if original_name in item:
                    item[new_name] = item.pop(original_name)

    placeholders = ", ".join(["?" for _ in fields])
    fields_str = ", ".join(fields)

    # 插入语句
    insert_sql = f"INSERT INTO {table_name} ({fields_str}) VALUES ({placeholders})"

    # 计算批次数
    total_items = len(data)
    num_batches = (total_items + batch_size - 1) // batch_size

    logger.info(
        f"开始向表 {table_name} 插入 {total_items} 条数据，"
        f"分 {num_batches} 批进行"
    )

    total_inserted = 0
    start_time = time.time()

    # 使用tqdm创建进度条
    if show_progress and TQDM_AVAILABLE:
        progress_bar = tqdm(total=total_items, desc=f"导入 {table_name}", unit="条")

    try:
        # 开始事务
        if manage_transaction:
            conn.execute("BEGIN TRANSACTION")

        # 分批插入数据
        for batch_index in range(num_batches):
            start_idx = batch_index * batch_size
            end_idx = min(start_idx + batch_size, total_items)
            batch_data = data[start_idx:end_idx]

            # 准备批量插入的数据
            values_to_insert = []
            for item in batch_data:
                row_values = []
                for field in fields:
                    row_values.append(prepare_value_for_db(item.get(field)))
                values_to_insert.append(tuple(row_values))

            # 执行批量插入
            try:
                cursor.executemany(insert_sql, values_to_insert)
                batch_inserted = len(values_to_insert)
                total_inserted += batch_inserted

                # 更新进度条
                if show_progress and TQDM_AVAILABLE:
                    progress_bar.update(batch_inserted)

                # 默认模式每10批提交一次；快速模式整表一次提交
                should_commit = (
                    manage_transaction and not fast_import and (
                        (batch_index + 1) % 10 == 0 or
                        (batch_index + 1) == num_batches
                    )
                )
                if should_commit:
                    conn.commit()
                    if (batch_index + 1) != num_batches:
                        conn.execute("BEGIN TRANSACTION")

                # 显示进度（如果没有进度条）
                if not (show_progress and TQDM_AVAILABLE):
                    progress = (batch_index + 1) / num_batches * 100
                    elapsed_time = time.time() - start_time
                    eta = 0
                    if batch_index > 0:
                        eta = (
                            elapsed_time / (batch_index + 1)
                        ) * (num_batches - batch_index - 1)
                    logger.info(
                        f"批次 {batch_index + 1}/{num_batches} 完成 "
                        f"({progress:.1f}%)，已插入 "
                        f"{total_inserted}/{total_items} 条数据，"
                        f"耗时 {elapsed_time:.2f} 秒，预计剩余时间: {eta:.2f} 秒"
                    )

            except sqlite3.Error as e:
                logger.error(
                    f"向表 {table_name} 插入数据失败 "
                    f"(批次 {batch_index + 1}/{num_batches}): {e}"
                )
                raise

        # 提交最终事务
        if manage_transaction:
            conn.commit()

        # 关闭进度条
        if show_progress and TQDM_AVAILABLE:
            progress_bar.close()

        total_time = time.time() - start_time
        logger.info(
            f"向表 {table_name} 插入数据完成，共插入 {total_inserted} 条数据，"
            f"总耗时 {total_time:.2f} 秒"
        )

        return total_inserted, total_time

    except Exception as e:
        # 如果发生错误，回滚事务
        if manage_transaction:
            conn.rollback()
        logger.error(f"插入数据时发生错误: {e}")
        if show_progress and TQDM_AVAILABLE:
            progress_bar.close()
        raise
def replace_dataset_table(
    conn: sqlite3.Connection,
    table_name: str,
    file_paths: List[str],
    field_types: Dict[str, str],
    has_original_id: bool,
    batch_size: int,
    fields_file_path: str,
    create_indexes: bool,
    show_progress: bool,
    fast_import: bool,
    dataset_config: Dict[str, Any],
) -> int:
    """在单个事务中完整替换一个数据集表。"""
    total_records = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        create_table_if_not_exists(
            conn, table_name, field_types, True, None, create_indexes,
            has_original_id, commit=False,
        )
        for file_path in file_paths:
            logger.info(f"导入文件: {file_path}")
            result = insert_data_to_table(
                conn, table_name, load_json_file(file_path, dataset_config), batch_size,
                has_original_id, show_progress, fast_import,
                manage_transaction=False,
            )
            if result:
                total_records += result[0]
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if fields_file_path:
        save_table_fields(
            fields_file_path, table_name, field_types,
            list(TRANSLATION_FIELD_TYPES), has_original_id,
        )
    return total_records


def import_dataset(
    conn: sqlite3.Connection,
    dataset_key: str,
    dataset_config: Dict,
    base_path: str,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    fields_file_path: str = None,
    create_indexes: bool = False,
    show_progress: bool = True,
    fast_import: bool = False,
) -> Dict[str, Any]:
    """校验并原子导入单个数据集。"""
    logger.info(f"开始导入数据集: {dataset_key}")
    start_time = time.time()
    stats = {
        "dataset_key": dataset_key, "dataset_name": dataset_config.get("name", ""),
        "files_count": 0, "records_count": 0, "source_records": 0,
        "total_time": 0, "success": False, "errors": [],
    }
    try:
        file_paths = get_dataset_files(dataset_config, base_path)
        if not file_paths:
            raise ValueError("没有找到任何文件")
        table_name = dataset_key.replace("-", "_")
        if database_table_exists(conn, table_name) and not force:
            raise ValueError(f"表 {table_name} 已存在，请使用 --force 重建")
        validate_dataset_files(
            file_paths,
            dataset_config.get("tag", ""),
            dataset_config,
        )
        stats["files_count"] = len(file_paths)
        stats["source_records"] = count_source_records(file_paths, dataset_config)
        field_types, has_original_id = analyze_dataset_structure(
            file_paths,
            dataset_config,
        )
        if not field_types:
            raise ValueError("无法分析数据结构")
        stats["records_count"] = replace_dataset_table(
            conn, table_name, file_paths, field_types, has_original_id,
            batch_size, fields_file_path, create_indexes, show_progress,
            fast_import, dataset_config,
        )
        stats["success"] = True
    except Exception as exc:
        error_msg = f"导入数据集 {dataset_key} 失败: {exc}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        stats["errors"].append(error_msg)
    stats["total_time"] = time.time() - start_time
    logger.info(f"数据集 {dataset_key} 导入完成，共 {stats['records_count']} 条记录")
    return stats

def display_import_statistics(import_stats: List[Dict[str, Any]]):
    """显示导入统计信息"""
    if not import_stats:
        return

    print("\n导入统计信息:")
    print("-" * 100)
    print(
        f"{'数据集名称':<20} {'文件数':<8} {'记录数':<10} "
        f"{'耗时(秒)':<12} {'状态':<8} {'错误数':<8}"
    )
    print("-" * 100)

    total_files = 0
    total_records = 0
    total_time = 0
    success_count = 0

    for stats in import_stats:
        dataset_name = stats.get("dataset_name", stats.get("dataset_key", "未知"))
        files_count = stats.get("files_count", 0)
        records_count = stats.get("records_count", 0)
        total_time_sec = stats.get("total_time", 0)
        success = stats.get("success", False)
        errors = stats.get("errors", [])

        status = "成功" if success else "失败"

        print(
            f"{dataset_name:<20} {files_count:<8} {records_count:<10} "
            f"{total_time_sec:.2f}s{'':<8} {status:<8} {len(errors):<8}"
        )

        total_files += files_count
        total_records += records_count
        total_time += total_time_sec
        if success:
            success_count += 1

    print("-" * 100)
    print(
        f"总计: {len(import_stats)} 个数据集, {total_files} 个文件, "
        f"{total_records} 条记录, 耗时 {total_time:.2f} 秒"
    )
    print(f"成功: {success_count} 个, 失败: {len(import_stats) - success_count} 个")
    print("-" * 100)

def main():
    """主函数"""
    # 解析命令行参数
    args = parse_args()

    # 设置日志级别
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        logger.debug("已启用详细日志模式")

    try:
        # 加载配置
        logger.info(f"加载配置文件: {args.config}")
        config = load_config(args.config)

        # 如果只是列出可用的数据集
        if args.list:
            list_available_datasets(config)
            return

        # 创建数据库连接
        logger.info(f"连接数据库: {args.db}")
        conn = create_connection(args.db, fast_import=args.fast_import)

        # 是否显示进度条
        show_progress = TQDM_AVAILABLE and not args.no_progress

        # 导入统计信息
        import_stats = []
        exit_code = 0

        try:
            # 获取要导入的数据集
            datasets = config.get("datasets", {})
            if not datasets:
                logger.warning("配置文件中没有找到数据集")
                return

            logger.info(f"找到 {len(datasets)} 个数据集")

            # 如果指定了特定的数据集
            if args.datasets:
                logger.info(f"将导入以下数据集: {', '.join(args.datasets)}")

                # 验证所有指定的数据集是否存在
                for dataset_key in args.datasets:
                    if dataset_key not in datasets:
                        logger.warning(f"数据集 '{dataset_key}' 不存在")
                        continue

                    # 导入指定数据集
                    try:
                        stats = import_dataset(
                            conn, dataset_key, datasets[dataset_key],
                            config.get("cp_path", "./"), args.force, args.batch_size,
                            args.fields_file, args.create_indexes, show_progress,
                            args.fast_import
                        )
                        import_stats.append(stats)
                    except Exception as e:
                        logger.error(f"导入数据集 {dataset_key} 时发生错误: {e}")
                        logger.error(traceback.format_exc())
                        continue
            else:
                # 导入所有数据集
                logger.info("将导入所有数据集")

                for dataset_key, dataset_config in datasets.items():
                    try:
                        stats = import_dataset(
                            conn, dataset_key, dataset_config,
                            config.get("cp_path", "./"), args.force, args.batch_size,
                            args.fields_file, args.create_indexes, show_progress,
                            args.fast_import
                        )
                        import_stats.append(stats)
                    except Exception as e:
                        logger.error(f"导入数据集 {dataset_key} 时发生错误: {e}")
                        logger.error(traceback.format_exc())
                        continue

            # 显示统计信息
            if args.stats:
                display_import_statistics(import_stats)

            failed_stats = [stats for stats in import_stats if not stats.get("success", False)]
            if failed_stats:
                logger.error(f"有 {len(failed_stats)} 个数据集导入失败")
                exit_code = 1

            if args.verify_counts:
                _, all_matched = verify_dataset_counts(
                    conn,
                    datasets,
                    config.get("cp_path", "./"),
                    args.datasets
                )
                if not all_matched:
                    logger.error("严格计数校验失败")
                    exit_code = 1

            logger.info("所有导入操作已完成")

        finally:
            # 关闭数据库连接
            close_connection(conn)

        if exit_code != 0:
            sys.exit(exit_code)

    except Exception as e:
        logger.error(f"程序执行失败: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
