# RN 唐诗子集

2026-09-30：当前 API 连接 Qdrant Cloud。维护使用 `compose.cloud.yaml`；
`compose.yaml` 和原二进制保留作本地回滚。

Go API 入口频率、并发、详情缓存与 embedding 调用预算见
[限流配置](../rate-policy.md)。新二进制需同时使用更新后的部署配置；
容器监听要求显式 `HTTP_PRIVATE_CONTAINER=true`，宿主机端口继续仅发布到回环。
2026-10-01：BWG 和 RN 均已部署，BWG 为公网源站；验证及备份见
[发布报告](../../data/reports/poetry-access-20261001/report.md)。

仅部署 `yudingquantangshi`、`tangshisanbaishou`，复用已有的 127,031 条 BGE-M3 向量。
不补译、不重新 embedding。保留原始作品 ID、payload 和 `poetry-20260921-v1` 版本标识。

## 文件与启动

远端根目录为 `/opt/poetry-tang-20260922`，准备以下文件后再启动：

| 路径 | 内容 |
| --- | --- |
| `compose.yaml` | 本目录部署配置 |
| `.env` | 实际 `SILICONFLOW_API_KEY`，权限 `0600` |
| `bin/vector-api` | 静态 Linux 可执行文件，权限 `0755` |
| `corpus/chinese_poetry.db` | 仅含两个数据集的 SQLite，保留原始 ID |
| `corpus/datas.json` | 仅含两个数据集的配置 |
| `storage/` | 独立 Qdrant 存储，集合 `poetry_tang_20260922_v1` |
| `snapshots/` | Qdrant 快照目录 |

数据库和索引均在宿主机上，删除或重建容器不会删除数据。
API 只读挂载语料；旧全量索引与本部署目录隔离。
两个容器复用已经存在的 Qdrant 镜像，API 使用挂载的 Go 二进制作为入口。

通过 ssh-skill 在远端根目录执行：

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d
curl --fail http://127.0.0.1:18080/health
```

Qdrant 的启动检查只检查端口；须确认 API `/health` 返回成功，且集合点数为 127,031。
验证中文查询、作品详情以及不属于这两个数据集的作品无法获取后，再接入代理。
`docker compose config` 不带 `--quiet` 会输出密钥，勿将其输出保存到报告或日志。

Qdrant 限制为 768 MiB 内存、含 swap 共 1,024 MiB；API 限制为 192 MiB 且禁用 swap。
模型并发 4、检索并发 16；2026-09-22 短批次实测完整中文查询 8.67–9.76 QPS。
180 次请求全部成功，建议初期使用 4–8 个同时请求；此结果不是持续容量保证。
Docker 默认私有桥接网络保留 API 对 SiliconFlow 的出站访问；所有宿主机端口只监听回环。

RN 已完成快照恢复、精确计数与容器重建验证，两个容器保持运行。
部署证据及维护限制见 [部署报告](../../data/reports/rn-tang-deploy-20260922/report.md)。

## Qdrant Cloud

`compose.cloud.yaml` 只部署连接 Cloud 的 API，复用 RN 上的 SQLite、模型和业务配置。
将 `.env.cloud.example` 的实际凭证写入远端 `.env.cloud`，权限 `0600`；保留原 `.env`。
`QDRANT_CLUSTER_ENDPOINT` 使用 HTTPS 地址，程序会转换为 gRPC 6334 并启用 TLS。
显式 `QDRANT_URL` 优先，部署 Cloud 时不要设置旧的本地地址。

先将新二进制部署为 `bin/vector-api-cloud`，保留 `bin/vector-api`。
迁移工具见 [scripts/qdrant_cloud](../../scripts/qdrant_cloud/README.md)。
复制和完整校验完成后，通过 ssh-skill 在远端根目录启动候选服务：

```sh
docker compose -f compose.cloud.yaml --env-file .env --env-file .env.cloud config --quiet
POETRY_HTTP_PORT=18081 docker compose -p poetry-tang-cloud-candidate \
  -f compose.cloud.yaml --env-file .env --env-file .env.cloud up -d
curl --fail http://127.0.0.1:18081/health
```

候选服务通过检索和详情验收后，切换原有 18080 端口：

```sh
docker compose -f compose.cloud.yaml --env-file .env --env-file .env.cloud \
  up -d --no-deps vector-api
curl --fail http://127.0.0.1:18080/health
```

原本的 Qdrant 容器会成为未引用服务，继续保留；不要使用 `--remove-orphans`。
回滚使用原部署文件和原二进制，恢复本地连接：

```sh
docker compose -f compose.yaml --env-file .env up -d --no-deps vector-api
curl --fail http://127.0.0.1:18080/health
```

两份配置不要同时管理 `vector-api`；维护重启应使用当前生效的部署文件。

`validate_cloud_api.py` 检查搜索、详情、过滤、版本和鉴权。
存储向量样本若与旧 ANN 命中不同，须用源集合精确检索确认云端命中该样本自身的 ID、原文；
验收报告保留差异和精确检索证据。INT8 重建后的近似分数可能变化，不能要求逐位相同。

## HTTPS 接口

`openresty-locations.conf` 仅供现有 `rn-proxy-test.anyveo.com` 的 HTTPS server 引用。
现有 OpenResty 必须使用宿主网络，才能访问 `127.0.0.1:18080`；部署时确认该前提。
保留其他路径、证书、ACME 与 `/rn-proxy-check` 配置。

RN 已生成独立随机 token，调用方凭证位于 `/opt/poetry-tang-20260922/client-token`。
以下宿主机文件保存鉴权指令，两份凭证文件权限均为 `0600`：

`/opt/1panel/www/sites/rn-proxy-test.anyveo.com/poetry-auth.conf`

其容器内路径为 `/www/sites/rn-proxy-test.anyveo.com/poetry-auth.conf`，内容结构如下。
`REMOTE_GENERATED_TOKEN` 必须替换为仅保存在服务器上的随机值，不能原样部署：

```nginx
set $poetry_tang_token "REMOTE_GENERATED_TOKEN";
if ($http_authorization != "Bearer $poetry_tang_token") { return 401; }
```

缺少此文件时配置检查应失败，不能跳过鉴权 include。
鉴权 token 仅用于平台后端调用，不放入前端；代理不向 API 转发 Authorization。
无 token、错误 token 返回 401，正确 token 的搜索与详情均返回 200。
源站和经 Cloudflare 的请求都已通过验证，容器重建后再次通过。

接口为 `POST /poetry/search` 和 `GET /poetry/poems/<work_id>`。
搜索 JSON 仅返回 `{ "id": "<work_id>", "original": "命中诗句" }`。
完整原文、译文、作者和标题通过详情接口按需查询。
搜索响应头 `X-Poetry-Generation` 和 `X-Poetry-Score` 供平台后端使用；详情接口可携带
`?generation=<X-Poetry-Generation>` 锁定版本，旧版本返回 404，省略时使用当前版本。
旧调用方需同步移除对 `match`、`poem` 和 `detail_url` 字段的依赖。
不公开 `/health` 或 Qdrant 端口。

Cloudflare 现有 Bot Fight Mode 可能在回源前拦截调用，Bearer 鉴权不能豁免该规则。
2026-09-30 实测 Python 默认 User-Agent 被 Browser Integrity Check 以 1010 拦截；
验收工具使用明确的应用标识，Go 默认客户端标识可正常到达源站。
测试域名证书到期时间为 2026-12-21；现有证书续期目前需手动执行。
