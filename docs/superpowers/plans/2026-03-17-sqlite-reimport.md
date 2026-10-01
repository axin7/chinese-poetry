# SQLite Reimport Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `import_to_sqlite.py` 增加可显式开启的快速导入模式与严格计数校验，并完成对 `./chinese_poetry.db` 的全量重导入验证。

**Architecture:** 保持现有单脚本结构，只在 `import_to_sqlite.py` 内增加少量参数、连接配置和校验逻辑。默认行为保持兼容，`--fast-import` 负责启用高性能 SQLite PRAGMA 与单表单事务，`--verify-counts` 负责在导入结束后对每个 dataset 做源 JSON 条数与表行数一致性校验。

**Tech Stack:** Python 3、sqlite3、argparse、json、uv、pytest

---

## File Structure

- Modify: `import_to_sqlite.py`
  - 负责 CLI 参数解析、SQLite 连接、建表、批量导入、统计输出
  - 本次新增快速导入开关、校验函数、失败退出逻辑
- Create: `docs/superpowers/plans/2026-03-17-sqlite-reimport.md`
  - 当前实现计划文档

## Chunk 1: 实现快速导入与严格校验

**Chunk 1 验收约束：** 只有在验证命令显式包含 `--verify-counts` 且校验通过时，本 chunk 才算完成。未带 `--verify-counts` 的运行只能用于兼容性检查，不能作为验收依据。

### Task 1: 扩展 CLI 和数据库连接

**Files:**
- Modify: `import_to_sqlite.py:70-133`
- Modify: `import_to_sqlite.py:224-240`

- [ ] **Step 1: 写一个最小失败检查脚本，确认当前参数不存在**

```bash
uv run python import_to_sqlite.py --help
```

预期：输出中还没有 `--fast-import` 和 `--verify-counts`

- [ ] **Step 2: 添加新参数**

在 `parse_args()` 中新增：

```python
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
```

- [ ] **Step 3: 修改连接函数签名并添加快速导入 PRAGMA**

将：

```python
def create_connection(db_path: str) -> sqlite3.Connection:
```

改为：

```python
def create_connection(db_path: str, fast_import: bool = False) -> sqlite3.Connection:
```

在 `fast_import` 为真时执行：

```python
conn.execute("PRAGMA journal_mode = OFF")
conn.execute("PRAGMA synchronous = OFF")
conn.execute("PRAGMA cache_size = -65536")
conn.execute("PRAGMA temp_store = MEMORY")
conn.execute("PRAGMA locking_mode = EXCLUSIVE")
```

- [ ] **Step 4: 在 `main()` 中传递新参数**

将：

```python
conn = create_connection(args.db)
```

改为：

```python
conn = create_connection(args.db, fast_import=args.fast_import)
```

- [ ] **Step 5: 运行 help 验证参数已生效**

Run:

```bash
uv run python import_to_sqlite.py --help
```

Expected: 帮助输出中包含 `--fast-import` 与 `--verify-counts`

### Task 2: 按模式调整事务策略

**Files:**
- Modify: `import_to_sqlite.py:551-699`

- [ ] **Step 1: 先写一个最小行为约束注释/常量，避免把默认模式改坏**

新增函数参数：

```python
def insert_data_to_table(
    conn: sqlite3.Connection,
    table_name: str,
    data: List[Dict],
    batch_size: int = DEFAULT_BATCH_SIZE,
    has_original_id: bool = False,
    show_progress: bool = True,
    fast_import: bool = False,
):
```

- [ ] **Step 2: 保留默认行为，新增 fast_import 分支**

逻辑要求：

```python
if fast_import:
    conn.execute("BEGIN TRANSACTION")
    # 全批次执行完后统一 commit
else:
    conn.execute("BEGIN TRANSACTION")
    # 维持现有每10批 commit 一次
```

失败时都 `rollback()`，但 `fast_import=True` 时必须是整表回滚。

- [ ] **Step 3: 在调用处传递 `fast_import`**

修改 `import_dataset()` 中调用：

```python
result = insert_data_to_table(
    conn,
    table_name,
    data,
    batch_size,
    has_original_id,
    show_progress,
    fast_import,
)
```

并为 `import_dataset()` 增加参数：

```python
fast_import: bool = False
```

- [ ] **Step 4: 运行一次小范围命令，确保脚本可启动**

Run:

```bash
uv run python import_to_sqlite.py --datasets shijing --fast-import --stats --no-progress
```

Expected: 脚本进入导入流程，不因参数或函数签名错误直接崩溃

### Task 3: 添加严格计数校验与失败退出

**Files:**
- Modify: `import_to_sqlite.py:483-526`
- Modify: `import_to_sqlite.py:806-935`

- [ ] **Step 1: 新增源记录计数函数**

在 `get_dataset_files()` 后新增：

```python
def count_source_records(file_paths: List[str]) -> int:
    total = 0
    for file_path in file_paths:
        data = load_json_file(file_path)
        total += len(data)
    return total
```

- [ ] **Step 2: 新增表记录计数函数**

```python
def count_table_records(conn: sqlite3.Connection, table_name: str) -> int:
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    return cursor.fetchone()[0]
```

- [ ] **Step 3: 新增数据集校验函数**

```python
def verify_dataset_counts(conn, datasets, base_path, selected_datasets=None):
    ...
```

输出每个 dataset 的：
- `dataset_key`
- `dataset_name`
- `source_records`
- `table_records`
- `matched`

返回值至少应包含：
- 校验结果列表
- 是否全部通过

- [ ] **Step 4: 在 `main()` 中接入 `--verify-counts`**

要求：
- 导入完成后若 `args.verify_counts` 为真，则执行严格校验
- 任一表不一致或校验过程报错时，最终 `sys.exit(1)`
- 全部一致时保持成功退出

- [ ] **Step 5: 跑一个单 dataset 校验验证路径**

Run:

```bash
uv run python import_to_sqlite.py --datasets shijing --force --fast-import --verify-counts --stats --no-progress
```

Expected: 命令退出码为 0，且输出里包含校验结果字段与一致性结论

## Chunk 2: 执行全量重导入并验证结果

### Task 4: 做基线测试与针对性回归检查

**Files:**
- Test: `test_poetry.py`
- Modify: `import_to_sqlite.py`（仅如前面任务所需）

- [ ] **Step 1: 运行 JSON 基线测试（绕开旧版 pytest assertion rewrite 问题）**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain test_poetry.py -q
```

Expected: `14 passed`

- [ ] **Step 2: 运行导入脚本 help 回归检查**

Run:

```bash
uv run python import_to_sqlite.py --help
```

Expected: 参数说明完整，无 traceback

### Task 5: 执行全量重导入

**Files:**
- Modify: `./chinese_poetry.db`（运行产物）

- [ ] **Step 1: 如旧库存在，先删除旧库文件**

Run:

```bash
rm -f chinese_poetry.db
```

Expected: 工作目录中不再有旧数据库文件

- [ ] **Step 2: 执行全量重导入**

Run:

```bash
uv run python import_to_sqlite.py --force --fast-import --verify-counts --stats --no-progress
```

Expected:
- 脚本退出码为 0
- 输出所有数据集导入完成
- 输出严格计数校验全部通过

- [ ] **Step 3: 显式检查数据库文件已生成**

Run:

```bash
test -f chinese_poetry.db && test -r chinese_poetry.db
```

Expected: 命令退出码为 0，证明 `./chinese_poetry.db` 已生成且可读

- [ ] **Step 4: 记录关键结果**

记录：
- 总数据集数
- 总文件数
- 总记录数
- 总耗时
- 是否全部校验通过

### Task 6: 做最终数据库抽样与总结

**Files:**
- Modify: `import_to_sqlite.py`（如需极小修正）

- [ ] **Step 1: 校验 SQLite 表集合与 `loader/datas.json` 完全一致**

Run:

```bash
python3 - <<'PY'
import json
import sqlite3

with open('loader/datas.json', 'r', encoding='utf-8') as f:
    datasets = json.load(f)['datasets']
expected = sorted(key.replace('-', '_') for key in datasets.keys())

conn = sqlite3.connect('chinese_poetry.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
actual = sorted(row[0] for row in cur.fetchall())
conn.close()

print('expected_tables', expected)
print('actual_tables', actual)
print('missing', sorted(set(expected) - set(actual)))
print('extra', sorted(set(actual) - set(expected)))
raise SystemExit(0 if expected == actual else 1)
PY
```

Expected: 退出码为 0，且 `missing`、`extra` 都为空

- [ ] **Step 2: 运行 SQLite 抽样检查**

Run:

```bash
python3 - <<'PY'
import sqlite3
conn = sqlite3.connect('chinese_poetry.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
tables = [row[0] for row in cur.fetchall()]
print('tables', len(tables))
for name in tables:
    cur.execute(f'SELECT COUNT(*) FROM {name}')
    print(name, cur.fetchone()[0])
conn.close()
PY
```

Expected: 能列出所有导入表及对应记录数

- [ ] **Step 3: 总结脚本优化空间**

输出结论时必须区分：
- 已落地优化：`--fast-import`、单表单事务、严格计数校验
- 仍可优化但本次未做：跨多文件并集字段分析、流式 JSON 读取、导入过程更细粒度 profiling

- [ ] **Step 4: 完成后再决定是否进入收尾流程**

只有在：
- 基线测试通过
- `./chinese_poetry.db` 已生成且可读
- 全量导入退出码为 0
- 严格计数校验通过
- SQLite 实际表集合与 `loader/datas.json` 期望表集合完全一致

才可以进入后续开发分支收尾。
