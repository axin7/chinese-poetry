# 全量 Qdrant 本地资源限制试验

测试时间：2026-09-22 02:44–02:48，北京时间。

## 结论

全量索引在 **1,280 MiB 物理内存 + 1,024 MiB swap** 的隔离容器中可以恢复、
启动并完成检索；仅给 **1,280 MiB 物理内存且禁用 swap** 时，冷启动被 OOM 终止。
因此，“可用物理内存小于完整索引常驻内存”不等于“低并发也绝对无法启动”，
但此预算严重依赖换页，不能据此推荐为生产配置。

这是本地 Docker 的资源限制试验，**不是 rn 硬件实测**。本地使用 Apple/OrbStack，
Docker VM 有约 11.74 GiB 内存、zram 压缩交换区和 1 GiB 磁盘交换区；
rn 是老款 Xeon，现有 swap 环境不同。
本地 bind mount 还被 Qdrant 标记为 FUSE，磁盘与虚拟化路径同样不同。
下表吞吐只能说明这份完整索引在该实验环境可以工作，不能作为 rn 的 QPS 承诺。

## 隔离与数据一致性

- 镜像：`qdrant/qdrant:v1.15.5`；CPU 配额：2。
- 原始数据：已完成的全量快照，恢复到独立 `envelope/storage` 目录。
- 集合：`poetry_sentences_20260921_v1`，1,024 维 Cosine，INT8 常驻内存量化。
- 恢复后及最终冷启动后均确认 `green`，精确计数均为 **1,717,558**。
- 没有修改、重启或复用现有线上候选容器的可写数据目录。
- 所有试验容器均已停止；保留容器、日志、原始样本和约 9 GiB 独立数据作为证据。

## 启动试验

| 场景 | 物理内存上限 | 内存 + swap 总上限 | 结果 |
|---|---:|---:|---|
| 首次全量快照恢复 | 1,280 MiB | 2,304 MiB | 68.90 秒内恢复、精确计数成功 |
| 已恢复数据冷启动，禁用 swap | 1,280 MiB | 1,280 MiB | 3.62 秒内退出；OOM=true，exit=137 |
| OOM 后恢复为允许 swap，重新冷启动 | 1,280 MiB | 2,304 MiB | 3.85 秒内就绪，精确计数成功 |

允许 swap 的首次恢复及检索过程中，物理内存达到 1,280 MiB 上限，
swap 历史峰值为 **933.3 MiB**。各检索阶段采样的 swap 最大值约 706–733 MiB。
这些观测说明换页承担了实际索引内存，而不是仅仅配置了一个未使用的后备空间。

## 向量检索负载

从恢复后的集合抽取 128 个已存在向量，轮转查询。每级并发持续约 25 秒，
使用持久 HTTP 连接，`hnsw_ef=64`、`indexed_only=true`、`rescore=false`、
`limit=1`，带与应用一致的 generation 筛选和 payload 字段。
计时包含本地 HTTP、序列化与 Qdrant，**不包含模型 embedding、SQLite 原译文拼装、
公网链路和应用层鉴权**。重复使用 128 个向量不能代表长期无限多样查询的工作集。

| 同时检索数 | 成功请求 | 错误 | 吞吐 QPS | p50 ms | p95 ms | p99 ms | 最慢 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | 948 | 0 | 37.91 | 25.04 | 38.48 | 46.36 | 117.78 |
| 2 | 1,505 | 0 | 60.13 | 32.32 | 47.32 | 58.13 | 189.38 |
| 4 | 1,653 | 0 | 66.10 | 56.38 | 99.12 | 118.16 | 203.52 |
| 8 | 1,579 | 0 | 62.89 | 116.28 | 184.84 | 347.54 | 1,009.88 |

合计 **5,685 次成功，无错误**。并发从 4 增至 8 时吞吐没有继续上升，尾延迟恶化。
在检索阶段采样窗口内，cgroup 的 `pswpin` 增加约 1,329 万页，
`pswpout` 也增加约 1,329 万页；CPU 时间增量中约 85% 是内核时间。
这表明限制内存后的工作负载有大量换页开销。
zram 的结果尤其不能按比例换算为 rn 磁盘 swap 的表现。

## 证据与复现

- `run_envelope.py`：启动、监控、查询和干净停止的执行脚本。
- `poetry-envelope-rn-swap-startup.json`：首次恢复、cgroup 与精确计数。
- `poetry-envelope-rn-swap-c1.json` 至 `c8.json`：每次请求耗时及资源样本。
- `poetry-envelope-rn-swap-benchmark.json`：检索结果汇总。
- `poetry-envelope-rn-noswap-startup.json`：OOM 状态与退出码。
- `poetry-envelope-rn-frozen-startup.json`：最后一次全量可用性校验。
- `poetry-envelope-rn-frozen-stop.json`：干净停止记录。
- `corpus_sample_vectors.json`：128 个预计算查询向量，无模型 API 调用。

脚本还可通过 `benchmark --host HOST --port PORT --no-container-observations`
对另一独立 Qdrant 端点重放向量负载；目标端的资源观测必须另行采集。
