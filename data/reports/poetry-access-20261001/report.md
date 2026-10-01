# 诗词访问控制与限流发布

2026-10-01，发布编号 `access-20261001T024128Z`。
BWG 为当前公网源站，RN 保持运行作为回滚源站；两处均已部署本次版本。
本次只更新诗词 API 二进制及对应运行配置。

## 访问边界

公网 Caddy / OpenResty 在转发诗词请求前校验服务 Bearer token。
缺少或错误 token 返回 401；正确 token 的搜索和详情可访问。
原生 API 保持 `127.0.0.1:18080`，RN 容器端口也只发布到宿主机回环。
用户登录、权益和逐用户限流由 DeepFocus Worker 校验；服务 token 不下发扩展。

Go 使用固定的服务共享限额，不采信客户端 IP 或身份头。
同服务凭证轮换不会创建独立桶；以下设置覆盖单个 Go 进程。

| 限额 | 两处线上设置 |
| --- | --- |
| 搜索入口 | 2 次/秒，突发 4 次 |
| 详情入口 | 10 次/秒，突发 20 次 |
| HTTP 并发 | 64 |
| 未缓存检索并发 | 16 |
| 未缓存详情并发 | 8 |
| embedding 并发 | 4 |
| 实际 embedding 请求 | 滚动 60 秒内 120 次，失败请求也计数 |
| 详情缓存 | 8 MiB，TTL 1 小时，版本/数据集/作品 ID 隔离 |

缓存命中不消耗 embedding 额度，但仍受入口速率限制。
相同详情缓存未命中合并为一次查询；错误不缓存。
完整策略及单进程限制见 [限流配置](../../../deploy/rate-policy.md)。

## 发布证据

两处使用同一静态 Linux amd64 二进制，其 SHA-256 为：

```text
c6647371d45f9df843670d9807e8c2681728259349948a1145b9784103b50681
```

BWG 二进制为 `/opt/poetry-tang/bin/vector-api`，
配置为 `/etc/poetry/vector-api.env`，
备份为 `/opt/poetry-tang/access-20261001T024128Z/backup`。
RN 二进制为 `/opt/poetry-tang-20260922/bin/vector-api-cloud`，
配置为 `/opt/poetry-tang-20260922/compose.cloud.yaml`，
备份为 `/opt/poetry-tang-20260922/access-20261001T024128Z/backup`。

发布前先在 `127.0.0.1:18081` 启动候选实例并通过 5 项检查，
然后切换正式服务。
BWG 只重启 `poetry-api.service`；RN 只重建 `vector-api`。
候选实例已停止；Qdrant 和 OpenResty 容器 ID、启动时间保持一致。

两处 SQLite 与数据集配置哈希均未改变：

```text
SQLite: c95b12576cf24cf9f67bcfad8c7d4aff098ad137abb11919fcea2b935324c799
Config: fad238022fdb8364ac8b0555f682321458efaf75caaa4680af837aaaad90d644
```

继续使用 Qdrant Cloud TLS、集合 `poetry_tang_20260922_v1`、
语料版本 `poetry-20260921-v1` 和原有模型、代理凭证。

## 验证结果

| 检查范围 | BWG | RN |
| --- | --- | --- |
| 候选 API | 5/5 | 5/5 |
| 正式 API 与源站鉴权 | 10/10 | 10/10 |
| 源站 HTTPS 转发 | 4/4 | 4/4 |
| Cloudflare 公网转发 | 4/4 | 回滚源站未切换公网 |
| 最终健康、文件和监听状态 | 5/5 | 5/5 |

正式搜索返回 `{id, original}`，详情返回完整原文，旧版本详情返回 404。
未带及错误凭证的源站搜索/详情均返回 401，正确凭证详情返回 200。
直接 API 搜索突发结果为 `[422,422,422,422,429,429]`；
缓存详情突发的前 20 次为 200，随后 4 次为 429。
搜索突发使用无效 JSON，不触发模型调用；详情突发只访问已缓存作品。

公网搜索突发结果为 `[429,422,422,422,429,422]`，拒绝响应包含
`code=search_rate_exceeded`、`retry_after=1`、`Retry-After: 1`，
`X-Origin-ID=bwg-poetry-network-20260930`。
公网详情缺少、错误和正确凭证分别返回 401、401、200。
第一次公网脚本将响应头转换为大小写敏感字典，误判小写的 `retry-after`。
已修正脚本并在新文件保存复核结果，首次记录保留。

本地 `go test ./...` 通过：154 个测试函数，13 个有测试的包。
HTTP API、limits、cache、search、SiliconFlow 和 API 启动的 race 测试通过。
RN 两份 Compose 配置检查通过。
未在线上强制耗尽容量；容量 503 和 embedding 预算由确定性 Go 测试覆盖。
120 RPM 是本服务准入策略，尚未证实为供应商配额；未验证或假设 TPM 配额。

额外受限诊断：BWG 使用现有 SiliconFlow 凭证调用 `Qwen/Qwen3.5-9B`，
`enable_thinking=false`、`max_tokens=64`、JSON 响应格式，返回 200，耗时 1393 ms。
未修改模型凭证；该结果不能替代 DeepFocus Worker 的完整场景评估验收。

脱敏原始证据：

- [BWG 发布前](bwg-baseline.json)、[发布后](bwg-released.json)
- [RN 发布前](rn-baseline.json)、[发布后](rn-released.json)
- [BWG 候选](bwg-verification-candidate.json)、[正式](bwg-verification-production.json)
- [RN 候选](rn-verification-candidate.json)、[正式](rn-verification-production.json)
- [BWG HTTPS](bwg-verification-gateway.json)、[RN HTTPS](rn-verification-gateway.json)
- [首次公网检查](bwg-verification-public.json)
- [公网复核](bwg-verification-public-rechecked.json)
- [BWG 最终状态](bwg-verification-final.json)、[RN 最终状态](rn-verification-final.json)

## 回滚

通过 ssh-skill 将对应 `backup` 内的二进制和配置成对恢复到原路径。
BWG 重启 `poetry-api.service`；RN 使用恢复后的 Cloud Compose，
仅重建 `vector-api`，保留其他容器。
随后检查回环健康、鉴权、搜索和详情；
本次二进制回滚不要求改 DNS 或重建索引。
若另行将公网切回 RN，按
[BWG 回滚说明](../../../deploy/bwg/README.md#rollback) 执行。
远端发布目录中的候选环境和配置包含凭证，不下载整目录或打印其内容。
