# SCC_TT

轻量级 Webhook 接收与留存服务：Python + FastAPI + SQLite，无需安装独立数据库服务。

## 当前能力

- `POST /webhook` 接收原始请求体；**数据库事务提交成功后才返回 HTTP 200**。
- 按北京时间自然月自动分表：`webhook_YYYYMM`。
- 每条记录包含：`id`、`content`、`received_at`、`pushed`（0 未推送，1 已推送）。
- SQLite WAL、`synchronous=FULL`、busy timeout、写入锁和索引。
- 查询列表、查询单条、统计、更新推送状态。
- 默认监听 `0.0.0.0:8081`。

## 快速部署（Linux）

```bash
git clone https://github.com/miaoyongsen/SCC_TT.git
cd SCC_TT
cp .env.example .env
# 编辑 .env：设置 WEBHOOK_TOKEN 为随机长密钥
chmod +x run.sh
./run.sh
```

默认数据库：`data/webhook.db`；日志：`logs/service.log`。建议把数据目录放在持久化磁盘并定期备份。

## API

### 接收 Webhook

```bash
curl -X POST http://127.0.0.1:8081/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Webhook-Token: your-secret' \
  -d '{"event":"demo","value":123}'
```

成功响应示例：

```json
{"ok":true,"id":1,"month":"202611","received_at":"2026-11-10T16:00:00.000+08:00"}
```

- `GET /health`：健康检查
- `GET /stats?month=202611`：月份统计
- `GET /records?month=202611&pushed=0&limit=100&offset=0`：分页查询
- `GET /records/1?month=202611`：查询单条记录
- `PATCH /records/1/push-status?month=202611&pushed=1`：更新推送状态

## 高流量与可靠性说明

- SQLite 是单写者数据库。当前版本使用单 Uvicorn Worker，并在进程内串行化写事务；不要直接增加 `--workers`。
- WAL 允许读写并行；`synchronous=FULL` 优先保证已确认写入的持久性，代价是写入吞吐可能低于 NORMAL。
- 数据写入失败返回 HTTP 503，发送方必须配置重试。发送方重试可能产生重复记录；当前版本不做业务级去重。
- 月表中的 `id` 只在该月内唯一，查询单条记录时必须同时提供月份。
- 单机持续写入达到 SQLite 瓶颈时，再考虑引入消息队列或专用数据库；目前不需要额外安装数据库。

## 安全建议

- 不要把真实 Token 提交到 GitHub；只在服务器 `.env` 或环境变量中配置。
- 公网部署应使用 HTTPS 反向代理，并按需配置来源 IP 白名单、限流和请求体大小限制。
- 仓库为 Public，切勿提交真实 Webhook 数据、数据库文件、日志、密钥或生产配置。
