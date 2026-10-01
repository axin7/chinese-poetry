#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
修复进度文件：根据数据库中已翻译的记录更新进度文件
"""

import json
import os
import sqlite3
import logging
import sys

# 配置文件路径
CONFIG_PATH = "loader/datas.json"
# 数据库配置
DB_PATH = "chinese_poetry.db"
# 进度文件路径
PROGRESS_FILE = "translation_progress.json"

# 日志配置
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.FileHandler("fix_progress.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def load_config(config_path):
    """加载配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"已加载配置文件: {config_path}")
        return config
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        return None

def load_progress(progress_file):
    """从文件加载进度"""
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.info(f"已加载进度文件，共 {sum(len(ids) for ids in data.values())} 条记录")
            return data
        except Exception as e:
            logger.error(f"加载进度文件失败: {e}")
            return {}
    else:
        logger.info("进度文件不存在，将创建新的进度文件")
        return {}

def save_progress(progress_file, data):
    """保存进度到文件"""
    try:
        # 打印数据结构，用于调试
        logger.info(f"保存的数据结构: {type(data)}")
        for table, ids in data.items():
            logger.info(f"表 {table}: {type(ids)}, 长度: {len(ids)}")

        # 确保每个表的ID列表不超过100个，以避免文件过大
        limited_data = {}
        for table, ids in data.items():
            if ids:  # 只添加有数据的表
                # 取前100个ID保存到进度文件中
                limited_data[table] = ids[:100]

        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump(limited_data, f, ensure_ascii=False, indent=2)

        # 记录实际保存的数据量
        total_saved = sum(len(ids) for ids in limited_data.values())
        total_actual = sum(len(ids) for ids in data.values())
        logger.info(f"已保存进度文件，实际保存 {total_saved} 条记录，总共有 {total_actual} 条记录")
    except Exception as e:
        logger.error(f"保存进度失败: {e}")

def get_translated_records(db_path, table_name):
    """获取已翻译的记录ID"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 检查表是否存在
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        if not cursor.fetchone():
            logger.warning(f"表不存在: {table_name}")
            return []

        # 获取已翻译的记录ID
        cursor.execute(f"SELECT id FROM {table_name} WHERE translation IS NOT NULL")
        records = [row['id'] for row in cursor.fetchall()]

        conn.close()
        logger.info(f"表 {table_name} 已翻译记录数: {len(records)}")
        return records
    except Exception as e:
        logger.error(f"获取已翻译记录失败: 表名={table_name}, 错误={e}")
        return []

def main():
    """主函数"""
    # 加载配置
    config = load_config(CONFIG_PATH)
    if not config:
        return

    # 加载进度文件
    old_progress = load_progress(PROGRESS_FILE)
    logger.info(f"原进度文件内容: {old_progress}")

    # 获取所有表
    datasets = config.get("datasets", {})
    if not datasets:
        logger.error("配置文件中没有找到数据集")
        return

    logger.info(f"找到 {len(datasets)} 个数据集")

    # 更新进度文件
    updated_progress = {}
    total_records = 0

    for table_name in datasets.keys():
        # 获取已翻译的记录ID
        translated_ids = get_translated_records(DB_PATH, table_name)
        if translated_ids:
            updated_progress[table_name] = translated_ids
            total_records += len(translated_ids)

    # 打印更新后的进度文件内容
    logger.info(f"更新后的进度文件内容: {updated_progress.keys()}")

    # 保存更新后的进度文件
    save_progress(PROGRESS_FILE, updated_progress)
    logger.info(f"进度文件更新完成，总共 {total_records} 条记录")

if __name__ == "__main__":
    main()
