# 翻译链路安全与稳定性重构设计

## 目标

在保留 `translate_poetry.py` 入口兼容性的前提下，重构现有古诗词翻译链路，使其满足以下目标：

- API 凭据与模型配置迁移到 `.env`
- 支持上游接口并发到 500
- 翻译失败不再删除原始数据
- 引入可持久化的翻译状态与错误信息
- 将超大单文件拆为职责清晰的小模块

## 已确认范围

- 入口脚本仍为 `translate_poetry.py`
- 上游接口最大并发支持 500
- 凭据放入 `.env`
- 继续使用 SQLite 作为翻译结果落盘介质
- 本次只改翻译链路，不改导入脚本和原始 JSON 数据

## 方案选择

采用“保留入口 + 拆分实现模块”的方式，而不是完全重写。

这样做的原因：

1. 保留现有使用方式，降低切换成本。
2. 解决安全、数据安全、恢复能力和维护性问题。
3. 将重构范围控制在翻译脚本内部，符合 KISS 与 YAGNI。

## 模块划分

新增 `translation_pipeline/` 包，职责如下：

1. `translation_pipeline/config.py`
   - 负责加载 `.env`
   - 提供 API Key、模型、Base URL、并发、超时等配置

2. `translation_pipeline/database.py`
   - 负责表校验
   - 负责补齐翻译状态字段
   - 负责查询待翻译记录
   - 负责批量保存翻译结果与失败状态

3. `translation_pipeline/client.py`
   - 负责调用上游接口
   - 负责响应解析、重试和基础限流

4. `translation_pipeline/runner.py`
   - 负责任务调度、并发池维持、进度统计和断点恢复

5. `translate_poetry.py`
   - 只保留 CLI 和主入口

## 数据库字段策略

不再使用“删除原始记录”处理异常数据。

为每个参与翻译的表补齐以下字段：

- `translation TEXT`
- `interpretation TEXT`
- `translation_status TEXT`
- `translation_updated_at TEXT`
- `translation_error TEXT`
- `translation_retry_count INTEGER DEFAULT 0`

状态约定：

- `pending`：待翻译
- `processing`：处理中
- `done`：翻译完成
- `failed`：翻译失败，可后续重试
- `skipped`：内容不适合翻译，但保留原始记录

## 待翻译判定

不再使用 `length(interpretation) < 50` 作为是否未翻译的判断标准。

改为基于状态字段：

- `translation_status IS NULL`
- `translation_status IN ('pending', 'failed')`

这样可以把“业务状态”和“文本质量”分离，避免短解释被重复处理。

## 并发与写入策略

上游接口并发能力按 500 设计，但以显式配置控制：

- `.env` 默认值可设置为 500
- CLI 可继续通过 `--workers` 覆盖
- 运行时使用 `asyncio.Semaphore` 控制请求并发
- HTTP 连接池上限与并发配置保持一致

数据库写入不再逐条 `commit()`，改为批量提交：

- 成功结果累积到一定数量后统一提交
- 状态更新也按批次提交
- 退出前强制 flush 一次

## 错误处理

1. 上游请求异常：
   - 做有限次数重试
   - 失败后写入 `translation_status='failed'`
   - 保存 `translation_error`
   - 增加 `translation_retry_count`

2. 内容异常或疑似残缺：
   - 不删除记录
   - 写入 `translation_status='skipped'`
   - 记录原因

3. 中断恢复：
   - `processing` 状态在新一轮启动时回收为 `pending`
   - 已 `done` 的记录不会重复处理

## 配置策略

新增 `.env.example`，至少包含：

- `POETRY_API_KEY`
- `POETRY_MODEL`
- `POETRY_BASE_URL`
- `POETRY_MAX_CONCURRENCY`
- `POETRY_REQUEST_TIMEOUT`
- `POETRY_DB_WRITE_BATCH_SIZE`

运行时使用 `python-dotenv` 加载 `.env`。

## 测试策略

本次至少覆盖以下行为：

1. 配置从 `.env` 正常加载
2. 数据库会补齐状态字段
3. 空内容或异常内容不会触发删除
4. 失败记录会被正确标记为 `failed` 或 `skipped`
5. `done` 状态记录不会被重复选出
6. CLI 能正常解析新旧参数

## 风险与边界

- 上游接口虽然支持 500 并发，但本地网络与 SQLite 写入仍可能成为瓶颈，因此保留 CLI 覆盖参数。
- 本次不引入分布式队列，不做多进程扩展。
- 本次不做更复杂的质量评估模型，只修正状态机和执行稳定性。

## 验收标准

满足以下条件才算完成：

1. `translate_poetry.py --help` 可正常输出且参数语义清晰。
2. API 凭据不再硬编码在代码中。
3. 翻译失败或残缺数据不再删除原始记录。
4. 存在明确状态字段，支持中断后继续跑。
5. 验证测试通过。
