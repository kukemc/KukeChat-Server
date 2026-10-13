# KukeChat Server

FastAPI + SQLAlchemy 2（全异步）+ WebSocket。206 个 REST 端点，三条实时通道。

## 这个仓库的定位

**后端代码的唯一真相源。** 改后端一律在这里改。

相关仓库：

| 仓库 | 位置 | 说明 |
| --- | --- | --- |
| KukeChat-Server | 本仓库（公开） | 后端 API |
| KukeChat-Client | `../KukeChat-Client`（公开） | 客户端三端 |
| KukeChat | `../KukeChat`（私有） | 后台管理端、部署与运维 |

> 后端只有本仓库这一份，开发和发布都在 `main` 上。

## 常用命令

```bash
uvicorn app.main:app --reload      # 开发
python -m pytest tests/ -q         # 测试，提交前必须通过
python -c "from app.main import app; print(len(app.openapi()['paths']))"   # 加载检查
```

本机的开发用 venv 在 `E:\kukechat-venv`（C 盘空间不足，装不下依赖）：

```bash
E:/kukechat-venv/Scripts/python.exe -m pytest tests/ -q
```

本地开发需要 `.env`：`cp .env.example .env`，至少填 `JWT_SECRET_KEY`。

## 硬性约定

### 1. 隐私边界 —— 最重要的一条

**管理员不能主动翻阅私聊。** 读取消息内容的唯一入口是「有人举报了这条消息」。

任何新增的消息读取路径必须：

1. 先调用 `report_service.ensure_message_is_under_report(db, message_id)`，无对应举报记录返回 403
2. 调用 `report_service.log_sensitive_read(...)` 写审计（`admin_audit_logs` 表，只增不改）

**不要**新增按用户 / 按会话 / 全站的消息浏览端点 —— 这三类端点已被移除，重新加回来会让整个隐私承诺失效。举报会话或用户也不解锁其中的消息：那类举报的证据在提交时已固化进 `target_snapshot`。

边界由 `tests/test_admin_message_scope.py` 锁定，改动后必须仍然通过。

### 2. 不要写死服务器地址、密钥或运营方标识

一律走 `app/core/config.py` + `.env.example`。仓库不含任何生产地址。

`JWT_SECRET_KEY` 没有默认值，空值 / 已知占位值 / 短于 32 字符时**启动即失败** —— 这是刻意的，可预测的签名密钥等于任何人都能伪造管理员令牌。不要为了方便加回默认值。

同理：`CONTENT_MODERATION_URL`、`CCW_BIND_*`、`SMTP_HOST`、`INVITE_BASE_URL` 均无生产默认值，未配置时对应功能降级或不可用。

## 目录结构

```text
app/
  api/v1/       REST 端点，按业务域拆分；router.py 汇总
  core/         config / database / dependencies / security / request_guard
  models/       SQLAlchemy 模型
  schemas/      Pydantic 请求响应模型
  services/     业务逻辑（大头在这里）
  websocket/    manager.py 连接管理 + routes.py 三条通道
tests/
deploy_start.py Gunicorn + Uvicorn Worker 生产启动
```

三条 WebSocket 通道：`/ws`（用户）、`/admin/ws`（后台刷新信号）、`/bot/ws`（机器人事件）。

## 几个容易误解的模块

**`services/moderation.py` 不是后台审核界面**，它是**内容自动审核服务**的接入层，被 conversations / posts / teamup / bots / task / users / bot_api 和 main 的后台 worker 依赖。默认关闭（`CONTENT_MODERATION_ENABLED=false`），本项目不自带审核服务。

**`services/reports.py` 一半 C 端一半后台**：`create_report` / `list_my_reports` / 快照构造器是用户侧；`review_report` / 封禁 / 角色管理 / 审计日志是后台侧。

**处罚只有一个入口**：`services/penalties.py::record_violation`。自动审核、举报处置、风纪委员、管理员手动封禁全部经过它，由它记分、算阶梯、落地禁言 / 封禁。不要在别处直接改 `user.status` / `banned_until` / `muted_until` —— 那样处罚不进台账（`user_violations`），申诉和撤销都找不到它。

**举报按案件处理**：`services/cases.py` 把同一目标的举报并成 `report_cases`，结论回写给案件内的每条举报。`services/jury.py` 是风纪委员：只能看到群聊消息和公开动态的匿名快照，私聊案件永远不进他们的队列（`ReportCase.jury_eligible`）。这条边界和管理员的消息边界一样是公开承诺，见 README。

接口契约在 `docs/moderation-v2.md`，改了接口要同步改它 —— 客户端和后台都照它写。

**举报证据在提交时固化**进 `target_snapshot`，不是实时查库。改动快照结构要小心，它是申诉与追溯的依据。

## 已知问题

`_message_report_snapshot` 加载 `ConversationMember` 时没有 `joinedload(user)`，随后同步访问 `member.user`。生产环境靠 identity map 侥幸成立，跨 session 的隔离测试会抛 `MissingGreenlet`。属于潜在脆弱点，动它会碰到举报证据逻辑，目前未修。

## 部署

```bash
python deploy_start.py     # Gunicorn + Uvicorn Worker
```

或 `docker compose up -d`。建议监听 `127.0.0.1:8000` 由 Nginx 反代到 HTTPS，反代必须保留 `X-Real-IP` / `X-Forwarded-For`（同 IP 自动登录依赖）和 `Upgrade` / `Connection`（WebSocket 依赖）。

多 worker / 多实例时 `REDIS_URL` 必填，否则 WebSocket 消息无法跨进程广播。

**不要覆盖的运行数据**：数据库文件、`uploads/`、`.env`。
