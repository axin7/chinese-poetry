# RN 唐诗子集部署与验证

2026-09-22，北京时间。已部署完成并保持运行。

RN 现在仅提供 `yudingquantangshi` 和 `tangshisanbaishou` 的自然语言语义检索。
复用已有的 127,031 条向量，未补译、未重新生成诗词向量。
完整中文查询的短批次实测为 8.67–9.76 QPS，180/180 成功。
建议初期使用 4–8 个同时请求；16 并发已通过短测，但没有提高吞吐。
这些是容量建议，尚未在 Cloudflare Worker 上设置入口限流。

## 数据范围与持久化

| 数据集 | SQLite 原始记录 | 已部署句向量 |
| --- | ---: | ---: |
| `yudingquantangshi` | 43,103 | 125,500 |
| `tangshisanbaishou` | 320 | 1,531 |
| 合计 | 43,423 | 127,031 |

SQLite 保留两表的原始记录和 ID；向量完全沿用原索引的可检索内容。
没有为缺译或原先未进入索引的记录新增向量。
不同数据集中的同一首诗仍保留各自版本，本次没有合并、去重或修正文献。

原索引为 1,717,558 条向量，本次部署约为其 7.40%，减少约 92.60%。
模型仍为 SiliconFlow `BAAI/bge-m3`，1,024 维、Cosine、INT8 量化。
集合改为 `poetry_tang_20260922_v1`；语料版本仍为 `poetry-20260921-v1`，
因为原文、译文、作品 ID、句子定位及向量均未改变。

所有持久化文件位于 RN 宿主机的 `/opt/poetry-tang-20260922`：

| 路径 | 用途 | 当前占用 |
| --- | --- | ---: |
| `storage/` | Qdrant 索引，绑定挂载到容器 | 约 746 MiB |
| `corpus/` | SQLite 与两数据集配置，只读挂载 | 约 49 MiB |
| `snapshots/` | 可恢复的 Qdrant 快照 | 约 746 MiB |
| `bin/vector-api` | 已验证的静态 API 程序 | 复用原版本 |
| `.env` | 模型凭证，权限 0600 | 不包含在报告中 |
| `client-token` | 平台调用的 Bearer token，权限 0600 | 不包含在报告中 |
| `compose.yaml` | 两个容器的启动与资源配置 | — |

已实际强制重建两个容器，再核对全部计数、索引状态、SQLite 完整性和查询接口。
数据保留完整，说明持久化不依赖容器本身。宿主机目录仍需单独备份，容器持久化不等于异地备份。

旧全量索引保留在 `/opt/poetry-rn-benchmark-20260922/storage`，旧 API 和 Qdrant 容器仍停止。
该旧目录还包含现有证书管理工具，不要整体删除。

## 已启用的接口

搜索：`POST https://rn-proxy-test.anyveo.com/poetry/search`

```json
{"query":"想找写春天花开、溪水清澈的唐诗。"}
```

平台服务端发送 `Authorization: Bearer <RN client-token 文件中的值>`。
此 token 应作为后端或 Worker secret 使用，不放入浏览器前端。
本次完成诗词服务部署，没有新增 DeepFocus 会员界面或 Worker 业务接线。

详情：`GET /poetry/poems/<work_id>?generation=poetry-20260921-v1`。
搜索结果中的 `detail_url` 仍为 `/poems/...`，通过此域名访问时需加 `/poetry` 前缀。

源站 HTTPS 与经 Cloudflare 的请求均通过以下检查，并在重建后再次通过：

| 检查 | 结果 |
| --- | --- |
| 无 token、错误 token | 401 |
| 正确 token 搜索 | 200，返回目标诗集中的原文及译文 |
| 正确 token 获取详情 | 200 |
| 原有 `/rn-proxy-check` | 200 |
| 非部署数据集 `tangsong` 的详情 | 404 |

本机客户端经公网访问也返回预期的 401，TLS 验证成功，单次耗时约 1.27 秒。
Qdrant 的 17333/17334 端口及 API 的 18080 端口均只监听 `127.0.0.1`。
公网路由修改前已备份 vhost，OpenResty 配置检查通过后平滑重载。

本次未改动 Cloudflare DNS 或防机器人设置，也未重新执行全球多节点测试。
当前两个访问来源成功，不代表已消除此前全球测试中的 Bot Fight Mode 拦截。
原有限制参见 [网络报告](../rn-concurrency-20260922/network/report.md)。

## 功能与完整性检查

- 本地复制后逐条比对点 ID、payload 和 float32 向量，127,031 条完全一致。
- 上传快照、SQLite、配置与 API 程序的 SHA-256 校验通过。
- RN 集合状态 green，点数与已索引数均为 127,031；两个数据集的精确计数吻合。
- SQLite 只含指定两张业务表，`PRAGMA integrity_check` 为 `ok`。
- 14 次有效中文搜索及 1 次排除数据集检查通过。
- 14 份详情原文、译文、句子下标与搜索结果逐项吻合。
- 详情缺少 generation 或 generation 不匹配时均返回 404。
- 重建容器后数据计数不变，HTTPS 搜索、详情和鉴权再次通过。

功能测试检查结构和定位一致性，没有对诗意相关性进行人工评分。

## 完整中文请求性能

路径为自然语言输入 → SiliconFlow 查询 embedding → Qdrant → SQLite → 原文和译文。
在 RN 本机回环测试，不含客户端公网、Cloudflare、会员鉴权和 Qwen 文案生成。
模型并发仍为 4、超时 5 秒，检索并发为 16，rerank 关闭。

查询从原先使用的 144 类中文主题与 12 种实际语义偏好组合中抽取，固定随机顺序。
共发出 180 条不同输入，没有用重复输入制造应用响应缓存命中。
模型调用仅用于在线查询向量；没有重新 embedding 诗词语料。

| 同时请求数 | 成功 / 总数 | 实际批次时长 | QPS | p50 | p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 60 / 60 | 6.92 秒 | 8.67 | 0.390 秒 | 0.928 秒 |
| 8 | 60 / 60 | 6.15 秒 | 9.76 | 0.761 秒 | 1.002 秒 |
| 16 | 60 / 60 | 6.44 秒 | 9.32 | 1.584 秒 | 1.872 秒 |

各阶段最多 15 秒或 60 次请求，以先达到者为准；本次均先达到请求上限。
因此这是短批次验证，不是长时间持续负载或绝对并发上限。

此前全量中文接口在 4/8 并发下约为 2.19/2.22 QPS，本次约为其 3.96/4.39 倍。
两轮时间、查询组合、运行时长和资源预算不同，这个倍数用于观察量级，不能当作严格 A/B。
本次 8 → 16 并发的吞吐没有改善，排队时间却增加，故建议初期使用 4–8 并发。
若需要初始入口限流，可先按 5–6 QPS 留余量，再按更长时间线上观测调整。

## 纯向量检索观察

另用 900 个不同的已有语料向量测 Qdrant，零模型调用。
使用与 Go 服务一致的 top-1、ef=64、indexed_only、generation 过滤和关闭 rescore 设置。

| 同时请求数 | 成功 / 总数 | 批次时长 | 批次折算 QPS | p95 |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 300 / 300 | 0.778 秒 | 385.6 | 15.9 ms |
| 8 | 300 / 300 | 0.686 秒 | 437.2 | 27.9 ms |
| 16 | 300 / 300 | 0.624 秒 | 480.7 | 45.6 ms |

这些阶段不足 1 秒，且使用已经预热的语料向量，不能据此宣称能持续承受约 480 QPS。
它们只说明当前小索引的查询延迟已明显低于完整请求。
自然语言查询向量与语料向量难度不同，REST 测试也有别于服务内部 gRPC。
综合低查询延迟、CPU 余量和模型并发 4，完整请求的主要限制已转向远程 embedding 路径；
本次未添加分阶段追踪，不能把两类测试的耗时直接相减作为模型耗时。

## 资源与维护

RN 配置仍为 2 vCPU、约 2.41 GiB RAM，未升级服务器。
Qdrant 上限为 768 MiB RAM、含 swap 共 1,024 MiB；API 上限 192 MiB、禁用 swap。
两个容器均设为 `restart: unless-stopped`，日志每份 10 MiB、最多 3 份。

| 观测 | 结果 |
| --- | --- |
| 中文测试后的 Qdrant cgroup 内存，含文件缓存 | 约 511.7 MiB |
| 中文测试后的 API cgroup 内存，含文件缓存 | 约 32.1 MiB |
| 两个新容器的 swap | 0 |
| 测试中的 OOM / 非预期重启 | 0 / 0 |
| 最后复查时宿主可用内存 | 约 1,259 MiB |
| 最后复查时磁盘剩余 | 约 19 GiB，使用率 56% |

中文请求阶段宿主 iowait 约 0–1%，未见持续换页压力。
后续读取原始向量样本时有短暂磁盘等待，不能把整个采样时段描述成零磁盘等待。
宿主仍有约 358 MiB 历史 swap 占用，并不来自两个新容器。
快照恢复曾触及容器内存限制并触发回收，没有 OOM；该计数在查询测试阶段没有增加。

通过 ssh-skill 在 RN 的部署目录执行维护命令：

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d
docker compose ps
curl --fail http://127.0.0.1:18080/health
```

不要输出不带 `--quiet` 的 Compose 展开配置，其中包含模型凭证。
测试域名证书到期时间为 2026-12-21，当前沿用手动续期脚本：
`/opt/poetry-rn-benchmark-20260922/certbot/renew-rn-test.sh`。
尚未新增自动续期定时器；转为长期正式入口时需落实续期维护。

## 复查材料

- [部署配置](../../../deploy/rn-tang/compose.yaml)
- [复制与快照清单](subset-manifest.json)
- [功能结果](actual-rn/evidence/smoke.json)
- [中文请求结果](actual-rn/evidence/text-benchmark.json)
- [纯向量结果](actual-rn/evidence/qdrant-benchmark.json)
- [中文测试后资源](actual-rn/evidence/after-text.json)
- [容器重建后检查](actual-rn/evidence/after-recreate.json)
- [重建后的 HTTPS 检查](actual-rn/evidence/https-after-recreate.json)

本次只新增独立部署材料、验证脚本及报告，没有修改检索业务代码。
