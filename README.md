# SCC_TT

轻量级 Webhook 接收与留存服务：Python + FastAPI + SQLite，无需安装独立数据库服务。

## 设计范围

本项目**只负责接收和持久化 Webhook**，不做推送、不做事件去重、不提供查询/统计/管理 API。

- 唯一业务接口：`POST /webhook`，默认监听 `0.0.0.0:8081`。
- 原样保存 UTF-8 请求体；非 UTF-8 二进制内容以 Base64 JSON 包装保存。
- 按北京时间自然月自动分表：`webhook_YYYYMM`。
- 表字段只有：`id`、`content`、`received_at`。
- SQLite WAL、`synchronous=FULL`、busy timeout。
- 有界异步接收队列 + 批量事务写入，降低高流量下逐条提交的开销。
- **只有数据库批量事务提交成功后，才向发送方返回 HTTP 200**。队列满时施加背压，不丢弃内存队列中的请求；数据库故障返回 HTTP 503。

## Linux 部署

```bash
git clone https://github.com/miaoyongsen/SCC_TT.git
cd SCC_TT
cp .env.example .env
# 编辑 .env，设置 WEBHOOK_TOKEN 为随机长密钥
chmod +x run.sh
./run.sh
```

首次运行会创建 Python 虚拟环境并安装依赖。SQLite 是 Python 标准库，无需安装数据库服务。

默认数据库：`data/webhook.db`；日志：`logs/service.log`。

## 发送示例

```bash
curl -X POST http://127.0.0.1:8081/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Webhook-Token: your-secret' \
  -d '{"event":"demo","value":123}'
```

成功响应只表示数据已经提交到 SQLite：

```json
{"ok":true,"received_at":"2026-11-10T16:00:00.000+08:00"}
```

## 配置

见 `.env.example`：

- `PORT`：监听端口，默认 8081。
- `WEBHOOK_TOKEN`：可选接收密钥；生产环境务必设置。
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
