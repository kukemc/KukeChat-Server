<div align="center">

<img src=".github/assets/cover.png" alt="KukeChat Server" width="880">

# KukeChat Server

**为创作社区而写的实时聊天后端**

KukeChat 的服务端：一套 FastAPI 应用，用 REST 与 WebSocket 承载私聊、群聊、社区动态和社区自治。

<p align="center">
<img src=".github/assets/icons/lock.svg" width="16" height="16" align="absmiddle">&nbsp;私聊不可翻阅&emsp;&emsp;
<img src=".github/assets/icons/radio.svg" width="16" height="16" align="absmiddle">&nbsp;REST + WebSocket&emsp;&emsp;
<img src=".github/assets/icons/scale.svg" width="16" height="16" align="absmiddle">&nbsp;社区自治&emsp;&emsp;
<img src=".github/assets/icons/server.svg" width="16" height="16" align="absmiddle">&nbsp;可自部署
</p>

[![License](https://img.shields.io/badge/License-MIT-111111?style=flat-square&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-111111?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-111111?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-111111?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-7-111111?style=flat-square&logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-Compose-111111?style=flat-square&logo=docker&logoColor=white)](docker-compose.yml)
[![CI](https://img.shields.io/github/actions/workflow/status/kukemc/KukeChat-Server/ci.yml?branch=main&style=flat-square&label=CI&color=111111&logo=githubactions&logoColor=white)](https://github.com/kukemc/KukeChat-Server/actions/workflows/ci.yml)

**简体中文** · [English](README.en.md)

[关于](#关于) · [核心承诺](#核心承诺) · [功能](#功能) · [快速开始](#快速开始) · [配置](#配置) · [集成点](#集成点) · [部署](#部署) · [相关仓库](#相关仓库)

</div>

---

## 关于

KukeChat 是一个面向 Scratch / CCW 创作社区的聊天应用。这个仓库是它唯一的后端：四百多条 REST 路由、四条 WebSocket 通道，覆盖消息、群组、好友、动态、组队、机器人、举报与风纪委员、后台管理和应用发版。

配合 [KukeChat-Client](https://github.com/kukemc/KukeChat-Client) 与 [KukeChat-Android](https://github.com/kukemc/KukeChat-Android)，你可以为自己的社区完整地部署一套聊天服务。仓库不内置任何生产地址、密钥或运营方标识。想直接用官方服务，到 [KukeChat 下载页](https://chat-api.kuke.ink/download) 下载客户端。

## 核心承诺

聊天服务不可避免地持有用户的私人对话，所以这一节放在最前面。

> [!IMPORTANT]
> **管理员不能主动翻阅私聊。** 读取消息内容的唯一入口是「有人举报了这条消息」；审查私聊内容，只能通过举报提交时固化的证据快照。

### 管理员能看到什么

| 能力 | 管理员 |
| --- | :---: |
| 查看**被举报**的消息及其前后文 | 能 |
| 查看举报提交时固化的证据快照 | 能 |
| 主动搜索全站消息 | 不能 |
| 按用户翻阅其全部发言 | 不能 |
| 逐条浏览任意会话的消息 | 不能 |

这条边界在端点层强制执行：读取消息的路径都要先经过 `services/reports.py::ensure_message_is_under_report`，没有对应的举报记录一律返回 403。举报整个会话或某个用户，也不会解锁该会话里的其他消息 —— 那类举报的证据在提交时已固化进 `target_snapshot`，管理员只看快照。

边界由测试锁定，见 [`tests/test_admin_message_scope.py`](tests/test_admin_message_scope.py)。

### 风纪委员能看到什么

启用风纪委员后，一部分举报会交给通过考核的社区成员评审。他们看到的内容比管理员更少：

| 内容 | 风纪委员 |
| --- | :---: |
| 被举报的**群聊消息**及其前后几条（匿名：没有用户名、头像、群名） | 能 |
| 被举报的**公开动态** | 能 |
| **私聊**里被举报的消息 | 不能 |
| 举报人是谁、被举报人是谁 | 不能 |
| 自己所在的群、自己的好友、自己参与举报的案件 | 自动回避 |

私聊举报、对用户和对群的举报只由管理员处理。风纪委员看到的同样是固化快照，匿名化在服务端完成（`services/jury.py::juror_case_view`）。

### 其他需要知道的

- **敏感读取留痕**：查看被举报消息的上下文、查看案件快照等操作都会调用 `log_sensitive_read()`，写入只增不改的 `admin_audit_logs` 表：谁、什么时候、看了什么、范围多大。
- **自动审核**：开启后，消息、动态和图片会发送给你配置的外部审核服务。被判违规或「拿不准」的内容留下审核记录供人工复核；判为正常的记录默认 7 天后清理（`CONTENT_MODERATION_LOG_RETENTION_DAYS`）。
- **防小号的设备标识**：客户端会上报服务端签发的设备令牌、机器码哈希（桌面端、安卓）和浏览器指纹（网页端）。服务端只存这些值的 HMAC，后台只看得到前 8 位，用来判断几个账号是否来自同一台设备。详见 [`docs/device-bans.md`](docs/device-bans.md)。
- **游戏模式是唯一的公开例外**：群主可以把一个公开群接入 CCW 作品，此后这个群的近期消息对作品里的所有玩家可见，包括未登录的访客。只有群主能开启，且只有公开群能开启。

如果你部署这个服务，你就是这些数据的实际控制者。请在隐私说明里写清楚管理员具备哪些访问能力、在什么场景下使用，定期检查审计日志，并且不要为了排查方便重新加回已被移除的消息浏览端点。

## 功能

| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;领&#8288;域&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | 能力 |
| --- | --- |
| <img src=".github/assets/icons/message-circle.svg" width="16" height="16" align="absmiddle">&nbsp;消息 | 私聊、群聊、临时会话；文本、图片、语音、表情包；引用、@ 提及、表情回应、戳一戳；撤回、转发与合并转发、收藏、精华消息；投票；正在输入；语音转文字（需配置识别服务）；只限本人会话的聊天记录搜索 |
| <img src=".github/assets/icons/users.svg" width="16" height="16" align="absmiddle">&nbsp;群组 | 角色与权限、禁言、慢速模式、加群审批、邀请链接；成员头衔、群等级、签到排行；群公告；群表情；群升级（人数上限、群文件、永久精华、群待办） |
| <img src=".github/assets/icons/heart.svg" width="16" height="16" align="absmiddle">&nbsp;社交 | 好友与申请流、好友亲密度与成就、好友火花、资料点赞、账号等级、生日 |
| <img src=".github/assets/icons/newspaper.svg" width="16" height="16" align="absmiddle">&nbsp;社区 | 动态（话题、评论、点赞、置顶）、组队中心、任务协作、站内公告与回应、通知 |
| <img src=".github/assets/icons/coins.svg" width="16" height="16" align="absmiddle">&nbsp;金币 | 商店装饰、礼物、红包（普通、拼手气、接龙、语音口令、专属）；余额来自外部金币账本，见[集成点](#集成点) |
| <img src=".github/assets/icons/bot.svg" width="16" height="16" align="absmiddle">&nbsp;机器人 | 机器人广场与评价、安装到群、Bot API（Key 鉴权）与 `/bot/ws` 实时事件、消息内按钮组件 |
| <img src=".github/assets/icons/shield-check.svg" width="16" height="16" align="absmiddle">&nbsp;治理 | 举报并案处理、违规台账与阶梯处罚、申诉；风纪委员（考试、投票、资深合议与复审、判例、议事厅）；外部内容审核接入；设备封禁与注册限制；审计日志 |
| <img src=".github/assets/icons/plug.svg" width="16" height="16" align="absmiddle">&nbsp;平台 | CCW 账号绑定与资料同步、游戏模式（作品内嵌聊天）、应用发版与检查更新、下载页、作品介绍页用的公开数据接口、本地或 S3 兼容存储、SMTP 邮件、健康检查 |

机器人接入文档在客户端仓库：[`docs/bot-api.md`](https://github.com/kukemc/KukeChat-Client/blob/main/docs/bot-api.md)。

## 技术栈

<p align="center">
<img src="https://skillicons.dev/icons?i=py,fastapi,postgres,sqlite,redis,docker,nginx&theme=dark" height="40" alt="Python, FastAPI, PostgreSQL, SQLite, Redis, Docker, Nginx">
</p>

| 层 | 选型 |
| --- | --- |
| 框架 | FastAPI + Pydantic v2 |
| ORM | SQLAlchemy 2，全异步 |
| 数据库 | 生产推荐 PostgreSQL（asyncpg）；开发默认 SQLite（aiosqlite） |
| 实时 | 原生 WebSocket；多进程时用 Redis Pub/Sub 跨进程广播 |
| 鉴权 | JWT（python-jose）+ 会话 Cookie，密码哈希 passlib / bcrypt |
| 存储 | 本地磁盘或 S3 兼容对象存储（boto3），可选私有桶预签名直传 |
| 图片 | Pillow，自动压缩并生成缩略图 |
| 运行 | Gunicorn + Uvicorn Worker |

表结构在启动时按模型自动创建，老库缺的列也会自动补上，不需要单独跑迁移。

## 快速开始

### Docker

```bash
git clone https://github.com/kukemc/KukeChat-Server.git
cd KukeChat-Server
cp .env.example .env

# 生成签名密钥，填进 .env 的 JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(64))"

docker compose up -d
```

服务监听 `http://127.0.0.1:8000`，接口文档在 `/docs`，健康检查在 `/health`。

`docker-compose.yml` 包含 API 和 Redis，不含数据库。使用 SQLite 时，请把数据库放进挂载卷：在 `.env` 里设置 `DATABASE_URL=sqlite+aiosqlite:///./data/kukechat.db`。容器以 UID 10001 运行，宿主机上的 `data/` 和 `uploads/` 需要对它可写。

### 本地开发

```bash
python -m venv .venv
source .venv/bin/activate          # Windows：.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # 至少填 JWT_SECRET_KEY
uvicorn app.main:app --reload
```

运行测试：

```bash
pip install pytest
python -m pytest tests/ -q
```

> [!NOTE]
> `/api/v1` 下的请求需要客户端证明头（见 `app/core/request_guard.py`），用 curl 直接调用会被拒绝。机器人接口 `/api/v1/bot-api` 用 Key 鉴权，不受此限。

## 配置

所有配置通过 `.env` 注入，完整清单见 [`.env.example`](.env.example)，全部字段与默认值见 [`app/core/config.py`](app/core/config.py)。

| 变量 | 说明 |
| --- | --- |
| `JWT_SECRET_KEY` | **必填**。为空、仍是占位值或短于 32 字符时服务**拒绝启动** |
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./kukechat.db`；生产推荐 `postgresql+asyncpg://…` |
| `CORS_ORIGINS` / `CORS_ORIGIN_REGEX` | 必须包含你的前端来源，默认只有本地开发地址 |
| `COOKIE_SECURE` | 默认 `true`，会话 Cookie 只走 HTTPS；生产环境保持开启 |
| `REDIS_URL` | 单进程可留空；**多 worker 或多实例时必填**，否则 WebSocket 无法跨进程广播 |
| `WEB_CONCURRENCY` | `deploy_start.py` 的 worker 数；留空时 Redis 可连通为 2，否则为 1 |
| `STORAGE_BACKEND` | `local` 或 `s3`；选 `s3` 时填写 `S3_*`，`S3_PRIVATE_BUCKET` 用于群文件 |
| `CONTENT_MODERATION_ENABLED` | 默认关闭。本项目不自带审核服务，开启时必须提供 `CONTENT_MODERATION_URL` |
| `VOICE_ASR_URL` | 语音识别服务地址，留空则语音转文字和语音口令红包不可用 |
| `CCW_BIND_*` | CCW 账号绑定，留空则功能不可用；另需接入 CCW 网关，见[集成点](#集成点) |
| `SMTP_*` | 邮件发送，默认关闭 |
| `INVITE_BASE_URL` | 邀请链接的落地页，通常指向你部署的客户端 |
| `JURY_*` | 风纪委员的各项阈值，都有默认值，一般不用改 |

**关于 `JWT_SECRET_KEY`**：服务在启动时拒绝空值、已知占位值和过短的密钥。这是刻意的 —— 可预测的签名密钥意味着任何人都能伪造任意用户（包括管理员）的令牌。请使用随机值，并且不要在多个环境之间复用。

## 集成点

有两处功能依赖外部平台，**怎么向外部平台取数不在这个仓库里**。仓库只在 `app/integrations/` 定义接口和占位实现；绑定流程、资料同步、商店、作品链接卡片等业务逻辑都在开源部分。

| 接口 | 文件 | 提供什么 | 没有实现时 |
| --- | --- | --- | --- |
| CCW 网关 `CcwGateway` | `app/integrations/ccw_gateway.py` | `latest_comments(subject_oid, subject_type)` 某个评论区最新一页评论；`student_payloads(student_oid)` 某个学生的资料与主页统计；`creation_detail(oid, access_key)` 某个作品的详情。取不到时统一抛 `CcwUnavailable` | CCW 账号绑定显示为未开放（发起绑定返回 503，轮询评论的后台任务不启动）；资料同步和作品链接卡片静默跳过；评论验证码改密码不可用 |
| 金币账本 `CoinLedger` | `app/integrations/coin_ledger.py` | `credited_to(account_id)` 某个账号累计入账；`recent_credits()` / `all_credits()` 名单；`topup_url()` 充值页；`configured()` / `source()`。核实不了时统一抛 `CoinsUnavailable` | 商店能逛、能领等级解锁的商品、能装备；余额恒为 `stale`，花币购买返回 503（见 [`docs/shop.md`](docs/shop.md)） |

**提供自己的实现**：在 `app/_private/` 放一个 Python 包（已在 `.gitignore` 中，不会入库），导出两个不带参数的工厂函数：

```python
# app/_private/__init__.py
from app.integrations import CcwGateway, CoinLedger

def create_ccw_gateway() -> CcwGateway: ...
def create_coin_ledger() -> CoinLedger: ...
```

启动时 `app/integrations/loader.py` 找到这个包就用它，找不到就用占位实现。包存在但有问题（导入报错、缺工厂函数、返回的不是接口实现）时服务**拒绝启动**，不会悄悄退回占位实现。只想接其中一个时，另一个工厂返回 `NullCcwGateway()` / `NullCoinLedger()` 即可。实现需要自己保证不阻塞事件循环（同步请求放进线程池），两个接口的约定写在各自文件的文档字符串里。

测试里用 `integrations.use_ccw_gateway(...)` / `use_coin_ledger(...)` 换成假实现，示例见 [`tests/integration_fakes.py`](tests/integration_fakes.py)。全部测试在没有 `app/_private` 的情况下通过。

## 项目结构

```text
app/
  api/v1/          REST 端点，按业务域拆分；router.py 汇总
  api/public_showcase.py  作品介绍页读取的公开数据
  core/            配置、数据库、鉴权依赖、请求校验、设备信号
  integrations/    外部平台的接口与占位实现（见「集成点」）
  models/          SQLAlchemy 模型
  schemas/         Pydantic 请求 / 响应模型
  services/        业务逻辑
  websocket/       连接管理与四条实时通道
  templates/       下载页模板
  static/          下载页静态资源
docs/              接口契约与运维手册
scripts/           运维脚本（SQLite 迁移到 PostgreSQL、S3 直传自检等）
tests/             测试
deploy_start.py    生产启动脚本（Gunicorn + Uvicorn Worker）
```

实时通道：`/ws`（用户）、`/admin/ws`（后台刷新信号）、`/bot/ws`（机器人事件）、`/game/ws`（游戏模式）。

## 文档

| 文档 | 内容 |
| --- | --- |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`moderation-v2.md`](docs/moderation-v2.md) | 审核、处罚、举报、风纪委员的接口契约 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`jury-v2.md`](docs/jury-v2.md) | 风纪系统 v2：考试、合议、复审、判例、议事厅 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`jury-demo.md`](docs/jury-demo.md) | 风纪系统本地联调实例 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`device-bans.md`](docs/device-bans.md) | 设备信号、账号关联与设备封禁 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`chat-features.md`](docs/chat-features.md) | 投票、正在输入、群表情、戳一戳、公告回应、资料点赞等 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`group-plus.md`](docs/group-plus.md) | 群升级、群文件、永久精华、群待办、聊天记录搜索 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`group-announcements.md`](docs/group-announcements.md) | 群公告 v2 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`group-levels.md`](docs/group-levels.md) | 群等级与等级头衔 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`account-level.md`](docs/account-level.md) | 账号等级 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`intimacy.md`](docs/intimacy.md) | 好友亲密度与好友成就 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`sparks.md`](docs/sparks.md) | 好友火花 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`shop.md`](docs/shop.md) | 商店 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`gifts.md`](docs/gifts.md) | 礼物 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`red-packets.md`](docs/red-packets.md) | 红包 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`releases.md`](docs/releases.md) | 发版服务：应用版本、安装包、更新日志、自动更新 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`postgres-cutover.md`](docs/postgres-cutover.md) | 从 SQLite 切换到 PostgreSQL 的手册 |

## 部署

`deploy_start.py` 缺依赖时先安装 `requirements.txt` 和 Gunicorn，再用 Gunicorn 启动，默认监听 `127.0.0.1:8000`（可用 `HOST` / `PORT` 覆盖）：

```bash
python deploy_start.py
```

建议由 Nginx 反向代理到 HTTPS。反代必须保留下面这些请求头，否则真实 IP 识别和 WebSocket 都会出问题：

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

- **PostgreSQL**：把 `DATABASE_URL` 换成 `postgresql+asyncpg://…` 即可，启动时自动建表并尝试启用 `pg_trgm`（聊天记录搜索的索引）。从已有的 SQLite 库迁移，用 `scripts/migrate_sqlite_to_postgres.py`，步骤见 [`docs/postgres-cutover.md`](docs/postgres-cutover.md)。
- **多进程**：worker 多于一个或部署多个实例时，必须配置 `REDIS_URL`。
- **不要覆盖的运行数据**：数据库、`uploads/` 目录、`.env`。

## 相关仓库

| 仓库 | 说明 |
| --- | --- |
| <img src=".github/assets/icons/monitor.svg" width="16" height="16" align="absmiddle">&nbsp;[KukeChat-Client](https://github.com/kukemc/KukeChat-Client) | 社区扩展与桌面端（Windows，macOS 测试中） |
| <img src=".github/assets/icons/smartphone.svg" width="16" height="16" align="absmiddle">&nbsp;[KukeChat-Android](https://github.com/kukemc/KukeChat-Android) | 原生 Android 客户端，Kotlin + Jetpack Compose |
| <img src=".github/assets/icons/server.svg" width="16" height="16" align="absmiddle">&nbsp;**KukeChat-Server** | 本仓库：后端 API 与实时服务 |

## 贡献

欢迎 Issue 和 Pull Request。提交前请确保 `python -m pytest tests/ -q` 通过。

两条硬性约定：

1. **不要在代码里写死服务器地址、密钥或运营方标识**，一律走 `app/core/config.py` 与 `.env.example`。
2. **不要放宽管理员对消息的读取范围**。任何新增的消息读取路径都必须经过 `ensure_message_is_under_report()`，并调用 `log_sensitive_read()` 留痕。

## License

[MIT](LICENSE)

---

<div align="center">

<sub>MIT © 2026 kukemc</sub>

</div>
