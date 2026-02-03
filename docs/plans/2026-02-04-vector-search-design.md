# 诗词向量检索系统设计

## 概述

将古诗词按句子存入向量数据库，使用现代中文翻译生成向量，支持语义搜索。用户输入自然语言描述，返回意境相近的诗句及元信息。

## 技术选型

| 组件 | 选择 | 说明 |
|------|------|------|
| 向量数据库 | Qdrant | 开源，Rust 编写，支持过滤，部署简单 |
| Embedding | SiliconFlow bge-m3 | 中文效果好，1024 维 |
| 数据源 | SQLite (chinese_poetry.db) | 现有已翻译数据 |

## 数据模型

### Qdrant Collection 结构

```python
# Collection: poetry_sentences
# 向量维度: 1024

{
    "id": "tangsong_123_2",      # {table}_{poem_id}_{sentence_index}
    "vector": [0.1, 0.2, ...],   # translation 的 embedding
    "payload": {
        "table": "tangsong",
        "poem_id": 123,
        "sentence_index": 2,
        "original": "水文生舊浦，風色滿新花。",
        "translation": "水波荡漾在古老的河浦，春风吹拂着盛开的新花。",
        "title": "晦日宴高氏林亭",
        "author": "陳嘉言"
    }
}
```

### 存储估算

- 句子总数：~135.8 万
- 向量存储：~5.2 GB
- Payload 存储：~400 MB
- 总计：~5.6 GB

## 导入流程

```
SQLite → 读取诗词 → 拆分句子 → 调用 Embedding API → 写入 Qdrant
```

### 核心逻辑

```python
for row in db.execute("SELECT id, title, author, paragraphs, translation FROM {table} WHERE length(translation) > 10"):
    paragraphs = json.loads(row["paragraphs"])
    translations = json.loads(row["translation"])

    for idx, (original, trans) in enumerate(zip(paragraphs, translations)):
        point_id = f"{table}_{row['id']}_{idx}"
        vector = embed(trans)

        points.append({
            "id": point_id,
            "vector": vector,
            "payload": {
                "table": table,
                "poem_id": row["id"],
                "sentence_index": idx,
                "original": original,
                "translation": trans,
                "title": row["title"],
                "author": row["author"]
            }
        })

client.upsert(collection_name="poetry_sentences", points=points)
```

### 优化策略

- Embedding 批量调用：每次 100-500 句
- Qdrant 批量写入：每次 1000 个 Point
- 断点续传：记录已处理的 poem_id

## 检索接口

### 接口定义

```python
def search_poetry(
    query: str,                    # 自然语言描述
    tables: list[str] = None,      # 指定表，None 表示全部
    limit: int = 10                # 返回数量
) -> list[dict]:
    """
    返回:
    [
        {
            "score": 0.89,
            "original": "床前明月光，疑是地上霜。",
            "translation": "床前洒满明亮的月光...",
            "title": "静夜思",
            "author": "李白",
            "table": "tangsong",
            "poem_id": 12345,
            "sentence_index": 0
        }
    ]
    """
```

### 实现

```python
def search_poetry(query: str, tables: list[str] = None, limit: int = 10):
    query_vector = embed(query)

    query_filter = None
    if tables:
        query_filter = models.Filter(
            should=[
                models.FieldCondition(key="table", match=models.MatchValue(value=t))
                for t in tables
            ]
        )

    results = client.search(
        collection_name="poetry_sentences",
        query_vector=query_vector,
        query_filter=query_filter,
        limit=limit
    )

    return [{"score": r.score, **r.payload} for r in results]
```

### 使用示例

```python
# 搜索所有表
results = search_poetry("思念家乡的月亮")

# 仅搜索唐诗宋诗
results = search_poetry("离别的悲伤", tables=["tangsong"])

# 搜索宋词和诗经
results = search_poetry("爱情", tables=["songci", "shijing"])
```

## 项目结构

```
chinese-poetry/
├── vector_search/
│   ├── __init__.py
│   ├── config.py          # 配置
│   ├── embedding.py       # SiliconFlow API 封装
│   ├── importer.py        # 导入脚本
│   ├── searcher.py        # 检索接口
│   └── qdrant_data/       # Qdrant 数据目录
├── chinese_poetry.db
└── requirements.txt
```

### 配置 (config.py)

```python
# Qdrant
QDRANT_PATH = "./vector_search/qdrant_data"

# SiliconFlow
SILICONFLOW_API_KEY = "your-api-key"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
EMBEDDING_MODEL = "BAAI/bge-m3"

# Collection
COLLECTION_NAME = "poetry_sentences"
VECTOR_DIM = 1024
```

### 新增依赖

```txt
qdrant-client>=1.7.0
httpx>=0.25.0
```

## 运行命令

```bash
# 导入
python -m vector_search.importer --tables tangsong songci

# 检索
python -c "from vector_search.searcher import search_poetry; print(search_poetry('月亮'))"
```

## 迁移到服务器

使用 Qdrant 快照功能：

```python
# 本地创建快照
local_client.create_snapshot(collection_name="poetry_sentences")

# 服务器恢复
remote_client.recover_snapshot(
    collection_name="poetry_sentences",
    location="path/to/snapshot.snapshot"
)
```
