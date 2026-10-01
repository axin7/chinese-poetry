# Translation Pipeline Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重构 `translate_poetry.py` 的翻译链路，使凭据从 `.env` 加载、并发可支撑 500、失败不删原文，并引入可持久化翻译状态。

**Architecture:** 保留 `translate_poetry.py` 作为 CLI 入口，把配置、数据库、API 调用和任务调度拆进 `translation_pipeline/` 包。数据库基于显式状态字段驱动，不再依赖 `interpretation` 长度判断是否未翻译，并通过批量提交降低 SQLite 写入开销。

**Tech Stack:** Python 3、sqlite3、asyncio、openai、httpx、python-dotenv、pytest

---

## File Structure

- Modify: `translate_poetry.py`
  - 收缩为 CLI 和主入口
- Create: `translation_pipeline/__init__.py`
  - 包导出
- Create: `translation_pipeline/config.py`
  - `.env` 加载与配置对象
- Create: `translation_pipeline/database.py`
  - 表结构补齐、待翻译查询、状态更新、批量写入
- Create: `translation_pipeline/client.py`
  - 上游接口调用、重试、结果解析
- Create: `translation_pipeline/runner.py`
  - 并发调度与进度统计
- Create: `tests/test_translation_pipeline.py`
  - 新增翻译链路回归测试
- Create: `.env.example`
  - 环境变量示例
- Modify: `.gitignore`
  - 忽略 `.env`
- Modify: `README.md`
  - 更新翻译脚本用法与配置说明

## Chunk 1: 建立测试与配置基础

### Task 1: 为配置加载和数据库状态字段写失败测试

**Files:**
- Create: `tests/test_translation_pipeline.py`

- [ ] **Step 1: 写配置加载失败测试**

写一个测试，验证没有显式传值时会从 `.env` 读取 `POETRY_API_KEY` 和并发配置。

- [ ] **Step 2: 运行单测并确认失败**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain tests/test_translation_pipeline.py -q
```

Expected: 因目标模块不存在或行为未实现而失败

- [ ] **Step 3: 写数据库状态字段失败测试**

写一个测试，验证 `ensure_translation_fields()` 会补齐：

- `translation_status`
- `translation_updated_at`
- `translation_error`
- `translation_retry_count`

- [ ] **Step 4: 再次运行单测并确认按预期失败**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain tests/test_translation_pipeline.py -q
```

Expected: 失败信息指向缺少实现，而不是测试语法错误

## Chunk 2: 实现配置层与数据库层

### Task 2: 实现 `.env` 配置和数据库状态管理

**Files:**
- Create: `translation_pipeline/__init__.py`
- Create: `translation_pipeline/config.py`
- Create: `translation_pipeline/database.py`
- Modify: `.gitignore`
- Create: `.env.example`

- [ ] **Step 1: 实现配置对象**

要求：

- 使用 `python-dotenv` 加载 `.env`
- 提供默认值
- 并发上限默认支持到 500
- 缺少 `POETRY_API_KEY` 时快速失败

- [ ] **Step 2: 实现数据库字段补齐**

要求：

- 保留 `translation` 和 `interpretation`
- 新增状态相关字段
- 不删除原始记录

- [ ] **Step 3: 运行测试确认转绿**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain tests/test_translation_pipeline.py -q
```

Expected: 当前已实现测试通过

## Chunk 3: 为失败不删原文和状态流转写失败测试

### Task 3: 先写状态流转测试

**Files:**
- Modify: `tests/test_translation_pipeline.py`

- [ ] **Step 1: 写空内容不会删除的测试**

验证空内容记录会被标记为 `skipped`，而不是从表中删除。

- [ ] **Step 2: 写失败状态更新测试**

验证请求失败后会写入：

- `translation_status='failed'`
- `translation_error`
- `translation_retry_count + 1`

- [ ] **Step 3: 写完成状态过滤测试**

验证 `done` 状态记录不会再次被 `get_pending_records()` 取出。

- [ ] **Step 4: 运行测试并确认失败**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain tests/test_translation_pipeline.py -q
```

Expected: 新增测试失败，且失败原因对应缺失行为

## Chunk 4: 实现 API 客户端和运行器

### Task 4: 实现请求、批量写入和任务调度

**Files:**
- Create: `translation_pipeline/client.py`
- Create: `translation_pipeline/runner.py`
- Modify: `translation_pipeline/database.py`

- [ ] **Step 1: 实现客户端调用与解析**

要求：

- 支持 `response_format={"type": "json_object"}`
- 保留有限重试
- 对残缺/不适合翻译内容返回 `skipped`

- [ ] **Step 2: 实现批量状态写入**

要求：

- 不再逐条 `commit()`
- 支持批量 flush

- [ ] **Step 3: 实现运行器**

要求：

- 使用 `asyncio.Semaphore`
- 并发可配置到 500
- 启动时把 `processing` 回收为 `pending`
- 只处理 `pending` / `failed`

- [ ] **Step 4: 运行测试确认转绿**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain tests/test_translation_pipeline.py -q
```

Expected: 测试通过

## Chunk 5: 收缩 CLI 入口并更新文档

### Task 5: 保持入口兼容并更新说明

**Files:**
- Modify: `translate_poetry.py`
- Modify: `README.md`

- [ ] **Step 1: 将入口脚本改为薄封装**

要求：

- CLI 继续支持 `--db`、`--config`、`--batch`、`--workers`、`--model`
- 实际配置优先级清晰：CLI > `.env` 默认值

- [ ] **Step 2: 更新 README**

要求：

- 删除与当前实现不符的断点文件描述
- 增加 `.env` 使用方法
- 说明新的状态字段

- [ ] **Step 3: 检查 help 输出**

Run:

```bash
uv run python translate_poetry.py --help
```

Expected: 正常输出帮助，无 traceback

## Chunk 6: 最终验证

### Task 6: 执行验证命令

**Files:**
- Verify only

- [ ] **Step 1: 跑翻译链路测试**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain tests/test_translation_pipeline.py -q
```

Expected: 全部通过

- [ ] **Step 2: 跑既有 JSON 基线测试（如果文件存在）**

Run:

```bash
if [ -f test_poetry.py ]; then PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest --assert=plain test_poetry.py -q; else echo "test_poetry.py missing"; fi
```

Expected: 若文件存在则通过；若缺失则明确输出缺失，不伪造通过

- [ ] **Step 3: 运行入口 help**

Run:

```bash
uv run python translate_poetry.py --help
```

Expected: 退出码为 0

- [ ] **Step 4: 用小批量 dry-run 验证入口可启动**

Run:

```bash
POETRY_API_KEY=dummy uv run python translate_poetry.py --batch 1 --workers 1 --db chinese_poetry.db --config loader/datas.json --help
```

Expected: 参数解析正常，不触发运行时异常
