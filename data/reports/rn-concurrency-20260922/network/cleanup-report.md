# RN 清理记录

完成于 2026-09-22 10:57（北京时间）。所有远端操作均通过 ssh-skill 执行。

磁盘可用空间从 **2.88 GiB 增至 19.94 GiB**，增加 **17.06 GiB**；
使用率从 **93% 降至 52%**。
对应 `df` 可用字节数为 3,090,792,448 → 21,410,844,672。

已执行的清理：

- 删除 `/opt/poetry-rn-benchmark-20260922/archive-parts`，占用约 5.63 GiB。
- 删除同目录下 `native-transfer-sample.bin` 和 `native-transfer-sample-1.bin` 至
  `native-transfer-sample-4.bin`，合计约 320 MiB。
- 执行 `journalctl --vacuum-size=256M`，旧归档 journal 占用从约 3.64 GiB
  降至 **206.72 MiB**，当前日志保留。
- 执行 `apt-get clean`；下载缓存原本只有 24 KiB，无明显空间变化，未卸载软件包。
- 清空已停止的 `1Panel-new-api-80ss` 容器 JSON 日志，释放约 3.58 GiB；
  保留原日志文件。完整路径记录于同目录 JSON。
- 删除 `/opt/1panel/apps/new-api/new-api/logs` 中 13 个 `oneapi-*.log`
  历史日志，释放约 3.70 GiB；日志目录保留。
- 删除 `/opt/1panel/tmp/upgrade/v2.0.13/downloads` 与
  `/opt/1panel/tmp/upgrade/v2.1.3/downloads`，合计约 297 MiB。
  保留所有版本 `original` 回滚备份，剩余约 888 MiB。

删除前核对了目录真实路径、文件类型和占用，并确认本地备份与校验清单存在。
测试分片及样本可从本地 `envelope/transfer-fast`、`envelope/native-transfer-sample.bin`
恢复；**清理的旧 journal、应用日志和容器日志未在本次任务中备份，不能通过本次备份恢复**。
第二阶段执行前再次确认容器停止、日志和升级目录没有打开的文件句柄、
没有实际运行的升级/apt/dpkg 任务；常驻的自动升级关机等待进程未被停止。

完整向量存储前后占用均为 **9,660,526,592 字节**；SQLite、API、私密配置、
脚本和测试报告保留。new-api 的 SQLite、`.env`、`docker-compose.yml`
在清理前后的 SHA-256 一致。OpenResty 仍显示 `Up 7 weeks`。
没有停止服务、修改网络/防火墙/nginx 配置、执行 Docker prune 或删除卷。

仍保留的大项，仅列为后续候选：

| 项目 | 占用 | 说明 |
|---|---:|---|
| 1Panel 升级 `original` 目录 | 888 MiB | 历史程序与数据库回滚备份，保留 |
| Docker 报告的可回收镜像 | 1.86 GB | 未删除；需保留当前测试依赖的镜像 |
| `/opt/1panel/backup` | 267 MiB | 备份，保留 |

`df` 空间变化与各目录 `du` 差值之和存在少量差异，可能来自文件系统记账及同期写入。
