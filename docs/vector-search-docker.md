# 诗词向量检索运行说明

实现对应 `docs/plans/2026-02-04-vector-search-design.md`。在线 API、SQLite
回表、SiliconFlow、缓存、Qdrant 客户端和离线导入器全部使用 Go。Qdrant 接入使用
官方 `github.com/qdrant/go-client/qdrant`，通过 gRPC 端口 `6334` 通信。

查询成功时只返回一个结果。同一个 `/search` 接口支持文本或 `BAAI/bge-m3`
的 1024 维向量。

## 配置

```bash
cp .env.vector.example .env.vector
```

文本查询和离线导入需要在 `.env.vector` 设置：

```bash
SILICONFLOW_API_KEY=你的密钥
```

纯向量查询不调用 SiliconFlow，服务可以在没有密钥时启动。生产发布时应给
`CORPUS_GENERATION` 使用不可变版本，例如 `poetry-20260918-v1`；索引与 SQLite
快照必须属于同一版本。

## 宿主机持久化

向量、索引、快照和导入断点使用宿主机 bind mount，不存放在容器可写层。
Qdrant 存储是一整个目录，不能只保留其中某个文件。默认路径兼容现有数据：

| 配置 | 默认宿主机路径 | 容器路径 |
|---|---|---|
| `POETRY_DATA_DIR` 下的 `qdrant/` | `./data/qdrant` | `/qdrant/storage` |
| `POETRY_DATA_DIR` 下的 `snapshots/` | `./data/snapshots` | `/qdrant/snapshots` |
| `POETRY_DATA_DIR` 下的 `importer/` | `./data/importer` | `/state` |
| `POETRY_SQLITE_PATH` | `./chinese_poetry.db` | `/data/chinese_poetry.db`，只读 |
| `POETRY_DATASETS_PATH` | `./loader/datas.json` | `/data/datas.json`，只读 |

生产部署建议将数据放在代码目录之外，在 `.env.vector` 中设置：

```dotenv
POETRY_DATA_DIR=/srv/chinese-poetry
POETRY_SQLITE_PATH=/srv/chinese-poetry/corpus/chinese_poetry.db
POETRY_DATASETS_PATH=/srv/chinese-poetry/corpus/datas.json
```

启动前准备独立、不可变的 SQLite 备份快照和数据集配置文件，不直接复制仍在
写入的数据库主文件（未合并的 WAL 可能包含新数据）。文件不存在时挂载会失败，
不会自动创建同名目录。已有数据需继续挂载原路径，或停机完整迁移后再改路径；
仅修改环境变量不会搬迁数据。运行中的 Qdrant 数据应通过快照备份，
不要直接复制正在写入的存储目录。映射 `snapshots/` 本身不会自动生成快照。

下方命令的 `--env-file .env.vector` 同时为宿主机路径插值提供配置。
删除、重建或升级容器后，挂载同一数据目录并使用原来的
`COLLECTION_NAME`、`CORPUS_GENERATION` 和语料快照即可继续查询，
不需要重新生成 Embedding。启动 API 不会自动启动导入器。

## 启动 Qdrant

```bash
docker compose --env-file .env.vector -f docker-compose.vector.yml up -d qdrant
```

首次构建先在全部配置的数据集范围内导入最多 100 句，验证持久化和查询：

```bash
docker compose --env-file .env.vector -f docker-compose.vector.yml --profile tools run --rm \
  vector-importer \
  --max-sentences 100
```

确认查询结果后，保持语料、`COLLECTION_NAME`、`CORPUS_GENERATION` 和数据集
范围不变，去掉数量限制，沿用断点继续导入其余可索引语料：

```bash
docker compose --env-file .env.vector -f docker-compose.vector.yml --profile tools run --rm \
  vector-importer
```

导入器使用确定性 UUIDv5 point ID，并在 Qdrant 确认写入后更新元素级断点。
相同版本可安全续跑。`--reset-checkpoint` 只重置本地进度，不删除已有 point；
需要保证语料和版本仍相同。全量导入会调用 SiliconFlow 并产生费用。
`--rm` 仅删除导入器容器，已写入的向量和 `/state` 中的导入进度保留在宿主机。
中断后使用相同配置和数据集范围重新执行命令即可续传；正常重建 API 容器
不需要再次运行导入命令。
已保存断点之前的句子不会重新调用 Embedding；中断时尚未保存断点的批次
可能再次调用模型，但确定性 point ID 可避免产生重复记录。

`IMPORT_EMBEDDING_ENCODING=base64` 可减少离线导入的向量传输量，不改变模型、
维度或在线查询配置。瞬时网络错误、HTTP 408/429/5xx 最多重试三次；
每次确认写入并保存断点后，日志会输出累计数量与当前数据位置。
限流优先遵循 `Retry-After`；未提供时，三次等待分别为 30、60、120 秒。

当前导入范围为配置数据集中 `translation_status=done` 且原文、译文有效对齐的
句子。缺少译文的原始记录不会自动生成向量；全量语料发布前需核对覆盖率。

## 启动接口

```bash
docker compose --env-file .env.vector -f docker-compose.vector.yml up -d vector-api
curl -sS http://127.0.0.1:8000/health
```

文本查询：

```bash
curl -sS http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"一个人在外面，晚上特别想家"}'
```

带数据集过滤：

```bash
curl -sS http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query":"离别后想念朋友",
    "filters":{"tables":["tangsong","songci"]}
  }'
```

向量查询需要完整的 1024 维数组和匹配的 profile：

```json
{
  "vector": [/* 1024 个 float32 兼容数值 */],
  "embedding_profile": "sf-bge-m3-1024-v1"
}
```

同时传入 `query` 和 `vector` 时，接口直接使用传入向量召回。只有后台开启
rerank 时才使用 `query` 做候选重排。接口没有 `limit` 和分页参数。

搜索成功只返回作品 ID 和命中的诗句，不包含译文、作者、标题或详情地址：

```json
{"id":"tangshisanbaishou:1","original":"床前明月光，疑是地上霜。"}
```

示例用于说明字段结构，实际 ID 和诗句以数据库为准。`id` 是
`<数据集>:<作品首段行号>`，同一作品中的不同诗句共享作品 ID。
平台后端可读取 `X-Poetry-Generation` 和 `X-Poetry-Score` 响应头，分别用于
锁定语料版本和相关性过滤；这两个字段不放入公开搜索 JSON。

用户点击诗句后，详情页面通过 `GET /poems/<id>` 获取当前语料版本的完整作品：

```bash
curl -sS http://127.0.0.1:8000/poems/tangshisanbaishou:1
```

详情返回 `id`、`dataset`、`title`、`author`、`original`、`translation` 和
`interpretations`。原文、译文和赏析为数组，无内容时返回 `[]`；未知作者返回
`null`。分段作品会合并所有分段。找不到作品或 ID 无效时返回 404。

平台后端可追加搜索响应头中的 `?generation=<X-Poetry-Generation>`，避免语料
切换后把旧搜索结果关联到新快照。省略时使用当前版本；指定旧版本、空版本或多个
版本值时返回 404，不会静默回退到当前版本。升级前使用 `match`、`poem` 或
`poem.detail_url` 的调用方需要同步迁移到新的搜索响应。

## 本地开发

先启动 Qdrant，再运行：

```bash
set -a
source .env.vector
set +a
go run ./cmd/vector-api
```

小规模导入：

```bash
go run ./cmd/vector-importer \
  --datasets tangsong,songci \
  --max-sentences 100
```

每个 `CORPUS_GENERATION` 使用独立的 `COLLECTION_NAME`。语料或 embedding
配置变化时，同时更换这两个值并重新导入；当前实现会拒绝在同一 collection
中混入多个 generation。新 collection 验证完成后再修改 API 配置。

运行测试：

```bash
go test ./...
```

## 停止

```bash
docker compose --env-file .env.vector -f docker-compose.vector.yml down
```

该命令删除容器和 Compose 网络，宿主机 bind mount 中的数据仍然保留。
下次使用相同配置执行 `up -d qdrant vector-api` 即可重新挂载。
