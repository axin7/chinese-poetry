import sqlite3
import re
import os

DB_PATH = './chinese_poetry.db'

# 注册REGEXP函数到sqlite

def regexp(pattern, string):
    if string is None:
        return False
    return re.search(pattern, string) is not None

def get_all_tables(conn):
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [row[0] for row in cur.fetchall()]

def has_column(conn, table, column):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())

def delete_non_chinese_paragraphs(conn, table):
    # 统计要删除的行数
    count_sql = f"SELECT COUNT(*) FROM {table} WHERE paragraphs IS NOT NULL AND paragraphs NOT REGEXP '[\u4e00-\u9fff]'"
    cur = conn.execute(count_sql)
    count = cur.fetchone()[0]
    if count == 0:
        return 0
    # 执行删除
    del_sql = f"DELETE FROM {table} WHERE paragraphs IS NOT NULL AND paragraphs NOT REGEXP '[\u4e00-\u9fff]'"
    conn.execute(del_sql)
    conn.commit()
    return count

def main():
    db_path = DB_PATH
    if not os.path.exists(db_path):
        print(f"数据库文件不存在: {db_path}")
        return
    conn = sqlite3.connect(db_path)
    conn.create_function("REGEXP", 2, regexp)
    tables = get_all_tables(conn)
    total_deleted = 0
    for table in tables:
        if has_column(conn, table, 'paragraphs'):
            deleted = delete_non_chinese_paragraphs(conn, table)
            print(f"表 {table}: 删除了 {deleted} 行不含汉字的记录")
            total_deleted += deleted
    print(f"总共删除了 {total_deleted} 行")
    conn.close()

if __name__ == '__main__':
    main()
