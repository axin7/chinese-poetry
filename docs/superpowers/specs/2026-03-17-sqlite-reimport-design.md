# SQLite 全量重导入与校验设计

## 目标

基于现有 `import_to_sqlite.py`，对本地诗词 JSON 数据执行一次面向 `./chinese_poetry.db` 的全量重导入，并提供速度优先的导入模式与严格的导入结果校验。

## 已确认范围

- 目标数据库：`./chinese_poetry.db`
- 导入范围：`loader/datas.json` 中全部 datasets
- 优先级：速度优先
- 运行场景：本地全量重建，可接受激进 SQLite 性能参数
- 验收标准：脚本退出码为 0，且每个数据集对应表的 SQLite 行数与源 JSON 记录数一致

## 方案选择

采用在现有 `import_to_sqlite.py` 上做最小改造的方案，不新增导入脚本，不改变默认行为，通过新增参数显式开启优化与校验。

## 改动范围

仅修改 `import_to_sqlite.py`：

1. 在数据库连接阶段增加可选的快速导入模式。
2. 在主流程末尾增加严格计数校验。
3. 保持现有入口、参数风格和表结构规则兼容。

不改动以下内容：

- `loader/datas.json`
- 原始数据源 JSON 文件
- 按 dataset 一表的结构原则
- 自动附加 `translation`、`interpretation` 字段的现有逻辑

## 设计细节

### 1. 快速导入模式

为 `create_connection` 增加可选快速导入参数，并在显式传入 `--fast-import` 时设置以下 PRAGMA：

- `PRAGMA journal_mode = OFF`
- `PRAGMA synchronous = OFF`
- `PRAGMA cache_size = -65536`
- `PRAGMA temp_store = MEMORY`
- `PRAGMA locking_mode = EXCLUSIVE`

该模式仅用于本地全量重建场景，不作为默认行为。

### 2. 事务策略优化

事务策略按模式区分：

- 默认模式：保持现有“每 10 批提交一次”的行为不变，确保旧用法兼容。
- `--fast-import` 模式：切换为单表单事务模式。

在 `--fast-import` 下：

- 进入表导入前开始事务
- 全部批量插入成功后统一提交
- 任一批次失败则回滚整表导入

保留现有 `executemany` 批量写入方式。

### 3. 严格计数校验

新增导入后校验流程，并由 `--verify-counts` 显式开启。

校验步骤：

1. 复用 `get_dataset_files` 获取每个数据集下所有 JSON 文件。
2. 逐文件读取，累加源记录总数。
3. 对对应 SQLite 表执行 `SELECT COUNT(*)`。
4. 输出每个数据集的：
   - dataset key
   - 数据集名称
   - 源记录数
   - 表记录数
   - 是否一致
5. 任一表不一致则返回失败状态，并让主程序以非 0 退出。

### 4. CLI 参数

计划新增以下参数：

- `--fast-import`：开启快速导入模式
- `--verify-counts`：开启严格计数校验

默认不启用，保持旧用法兼容。

## 预期执行方式

本次执行将采用类似命令：

```bash
uv run python import_to_sqlite.py --force --fast-import --verify-counts --stats
```

本任务验收必须显式启用 `--verify-counts`。若未开启严格计数校验，即使脚本执行无报错，也不视为通过验收。

如环境中没有 `uv`，需根据实际环境处理，但实现目标不变。

## 验收标准

满足以下全部条件才算成功：

1. `./chinese_poetry.db` 成功生成。
2. 脚本执行结束退出码为 0。
3. 所有数据集对应表均创建成功。
4. 每个表的 SQLite 行数与源 JSON 记录总数一致。

## 风险与说明

- `journal_mode=OFF` 与 `synchronous=OFF` 会牺牲崩溃恢复能力，因此只适用于本地可重跑的全量重建。
- 现有脚本按首个文件分析字段结构；若同一 dataset 后续文件出现新增字段，仍可能导致插入失败。这属于当前脚本固有约束，本次不扩展为跨全量文件动态并集建表，以避免超出最小改造范围。
- 索引创建不是本次速度优先导入的核心路径，不作为必做改动。

## 后续实现要点

1. 修改参数解析与数据库连接逻辑。
2. 调整插入事务策略。
3. 增加计数校验函数与主流程失败退出逻辑。
4. 运行全量导入。
5. 输出优化分析与运行结果。