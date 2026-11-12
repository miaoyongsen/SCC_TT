# SCC_TT

轻量级 Webhook 接收与留存服务：Python + FastAPI + SQLite，无需安装独立数据库服务。

## 设计范围

本项目**只负责接收和持久化 Webhook**，不做推送、不做事件去重、不提供查询/统计/管理 API。

- 唯一业务接口：`POST /scc_msoc/log`，默认监听 `0.0.0.0:8081`。
- 原样保存 UTF-8 请求体；非 UTF-8 二进制内容以 Base64 JSON 包装保存。
- 按北京时间自然月自动分表：`webhook_YYYYMM`。
- 表字段只有：`id`、`content`、`received_at`。
- SQLite WAL、`synchronous=FULL`、busy timeout。
- 有界异步接收队列 + 批量事务写入，降低高流量下逐条提交的开销。
- **只有数据库批量事务提交成功后，才向发送方返回 HTTP 200**。队列满时施加背压，不丢弃内存队列中的请求；数据库故障返回 HTTP 503。

## Linux 部署

支持 **Python 3.9**，不需要升级 Python。部署脚本会固定安装兼容 Python 3.9 的 FastAPI/Uvicorn 版本。

一键部署（自动克隆、创建虚拟环境、安装依赖、在服务器本地生成 Token、nohup 后台启动）：

```bash
curl -fsSL https://raw.githubusercontent.com/miaoyongsen/SCC_TT/main/deploy.sh -o deploy.sh && bash deploy.sh install
```

默认安装到 `~/SCC_TT`。SQLite 是 Python 标准库，无需安装数据库服务。

默认数据库：`data/webhook.db`；日志：`logs/nohup.log` 和 `logs/service.log`。

## 发送示例

```bash
curl -X POST http://127.0.0.1:8081/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer your-secret' \
  -d '{"event":"demo","value":123}'
```

成功响应只表示数据已经提交到 SQLite：

```json
{"ok":true,"received_at":"2026-11-10T16:00:00.000+08:00"}
```

## 配置

见 `.env.example`：

- `PORT`：监听端口，默认 8081。
- `WEBHOOK_TOKEN`：部署时在服务器本地生成并写入 `.env`；不会从 GitHub 下载，也不会提交到仓库。
- `MAX_BODY_BYTES`：单个请求最大字节数，默认 10 MiB。
- `QUEUE_MAX_SIZE`：内存队列上限，默认 5000。
- `BATCH_SIZE`：单次批量写入最大条数，默认 200。
- `BATCH_WAIT_MS`：批量等待窗口，默认 10 毫秒。
- `DATA_DIR`、`LOG_DIR`：数据和日志目录。

## 高流量与可靠性说明

- 使用**单 Uvicorn Worker**，由一个后台写入协程串行批量写 SQLite；不要直接增加 Worker 数量。
- 请求进入有界队列后等待批量事务提交，再返回成功。队列满时请求会等待，形成背压。
- SQLite 仍然是单写者数据库。批量事务能降低每条请求单独提交的开销，但吞吐量最终受磁盘 I/O 限制。
- 若进程在请求入队后、事务提交前崩溃，该请求不会收到成功响应；发送端应对非 2xx/超时进行重试。由于本项目不做去重，发送方重试可能形成重复记录，按需求原样保留。
- 建议使用稳定磁盘、定期备份数据库，并通过 HTTPS 反向代理接入公网。
- Public 仓库不要提交真实 Token、生产数据、数据库文件或日志。

## 一键部署与 nohup 常驻运行

在 Linux 服务器执行下面一条命令即可自动克隆仓库、创建虚拟环境、安装依赖、在服务器本地生成随机 Webhook Token，并通过 `nohup` 后台启动（默认安装到当前用户的 `~/SCC_TT`）：

```bash
curl -fsSL https://raw.githubusercontent.com/miaoyongsen/SCC_TT/main/deploy.sh -o deploy.sh && bash deploy.sh install
```

脚本支持：

```bash
bash ~/SCC_TT/deploy.sh status   # 查看状态
bash ~/SCC_TT/deploy.sh logs     # 实时查看日志
bash ~/SCC_TT/deploy.sh restart # 重启
bash ~/SCC_TT/deploy.sh stop    # 停止
bash ~/SCC_TT/deploy.sh update  # 拉取最新代码、更新依赖并重启
```

服务 PID 保存在 `run/uvicorn.pid`，nohup 日志保存在 `logs/nohup.log`。应用日志保存在 `logs/service.log`。部署脚本默认单 Worker，避免多个进程并发写同一个 SQLite 文件。脚本会在首次部署时仅在服务器本地生成随机 `WEBHOOK_TOKEN`，保存在权限为 600 的 `~/SCC_TT/.env` 中；更新代码时会保留现有 Token。


### 接收响应与异步入库

接口通过 Bearer Token 校验后，将请求放入有界内存队列，由后台线程批量写入 SQLite。**只有 SQLite 事务成功提交后，接口才返回 HTTP 200**（包含 `persisted=true`）。如果数据库写入失败，接口返回 HTTP 503，不会向发送方谎报持久化成功；队列满时会产生背压，等待队列空间。发送方应对超时或 503 进行重试，并使用事件标识处理可能发生的重复投递。日志中的 `webhook accepted` 表示进入队列，`SQLite batch committed` 表示事务提交完成。
