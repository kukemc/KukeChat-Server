<div align="center">

<img src=".github/assets/cover.png" alt="KukeChat Server" width="880">

# KukeChat Server

**KukeChat 的后端服务** · FastAPI + SQLAlchemy 2 (async) + WebSocket

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/kukemc/KukeChat-Server/actions/workflows/ci.yml/badge.svg)](https://github.com/kukemc/KukeChat-Server/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11+-3776ab.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com)

配套客户端：[KukeChat-Client](https://github.com/kukemc/KukeChat-Client) · 社区扩展 / 桌面端 / 移动端

</div>

---

## 关于

这是 [KukeChat](https://github.com/kukemc/KukeChat-Client) 的服务端 —— 一个面向 CCW / Scratch 创作社区的实时聊天系统。214 个 REST 端点 + 三条 WebSocket 通道，覆盖私聊、群聊、好友、动态、组队、任务、机器人、举报处置与后台管理。

配上客户端仓库，你可以完整地自建一套属于自己社区的聊天服务。

## 隐私与管理员权限边界

聊天服务不可避免地持有用户的私人对话，所以这件事必须写在最前面，而不是藏在文档角落。

**管理员能看到什么**

| 能力 | 是否具备 |
| --- | :---: |
| 查看**被举报**的消息及其前后文 | ✅ |
| 查看举报提交时固化的证据快照 | ✅ |
| 主动搜索全站消息 | ❌ |
| 按用户翻阅其全部发言 | ❌ |
| 逐条浏览任意会话的消息 | ❌ |

管理员**无法主动翻阅私聊**。读取消息内容的唯一入口是「有人举报了这条消息」，由
`services/reports.py::ensure_message_is_under_report` 在端点层强制执行，没有对应
举报记录一律返回 403。举报整个会话或某个用户，也不会因此解锁该会话里的其他消息 ——
那类举报的证据在提交时已固化进 `target_snapshot`，管理员看快照即可。

这条边界有测试锁定，见 [`tests/test_admin_message_scope.py`](tests/test_admin_message_scope.py)。

**风纪委员能看到什么**

启用风纪委员后，一部分举报会交给通过审批的社区成员评审。他们看到的内容比管理员少得多：

| 内容 | 风纪委员 |
| --- | :---: |
| 被举报的**群聊消息**及其前后几条（匿名：没有用户名、头像、群名） | ✅ |
| 被举报的**公开动态** | ✅ |
| **私聊**里被举报的消息 | ❌ |
| 举报人是谁、被举报人是谁 | ❌ |
| 自己所在的群、自己的好友、自己参与举报的案件 | ❌（自动回避） |

私聊举报、举报用户、举报群聊只由管理员处理。风纪委员看到的同样是举报提交时固化的快照，
没有任何接口能让他们读到快照以外的消息；匿名化在服务端完成（`services/jury.py::juror_case_view`）。

**自动审核**

开启内容自动审核时，消息、动态和图片会发送给你配置的审核服务。被审核服务判为违规或
「拿不准」的内容会留一条审核记录供管理员复核；判为正常的记录默认 7 天后清理
（`CONTENT_MODERATION_LOG_RETENTION_DAYS`）。

**防小号的设备标识**

为了识别「被封后换个号继续违规」，客户端会上报设备标识：服务端签发的设备令牌、机器码（桌面端、安卓，客户端先加盐哈希）、
网页端的浏览器指纹。服务端只存这些值的 HMAC 哈希，后台只能看到前 8 位，用来判断几个账号是不是同一台设备、封禁设备；
IP 和以前一样记录，只作旁证。详见 [`docs/device-bans.md`](docs/device-bans.md)。

**每一次敏感读取都留痕**

两个上下文查看端点都会调用 `log_sensitive_read()`，向 `admin_audit_logs` 表写入
一条记录：谁、什么时候、看了哪条消息、上下文范围多大。这张表只增不改。

**如果你要部署这个服务**

你就是这些数据的实际控制者。请至少做到：在你的隐私说明里写清楚管理员具备哪些访问
能力、在什么场景下会被使用；定期检查审计日志；不要为了排查方便重新放开上面那三个
被移除的端点。

## 技术栈

| | |
| --- | --- |
| 框架 | FastAPI + Pydantic v2 |
| ORM | SQLAlchemy 2（全异步） |
| 数据库 | SQLite + aiosqlite（默认），可换任意 SQLAlchemy 异步方言 |
| 实时 | 原生 WebSocket，可选 Redis Pub/Sub 做跨进程广播 |
| 鉴权 | JWT（python-jose）+ passlib/bcrypt |
| 存储 | 本地磁盘或 S3 兼容对象存储（boto3） |
| 图片 | Pillow，自动压缩与生成缩略图 |
| 部署 | Gunicorn + Uvicorn Worker |

## 功能

<table>
<tr><td width="33%" valign="top">

**消息**
- 私聊 / 群聊 / 临时会话
- 文本、图片、语音、表情包
- 引用、@提及、表情回应
- 撤回、转发、收藏、精选
- 消息内按钮组件
- 全文检索与上下文定位

</td><td width="33%" valign="top">

**社交与内容**
- 好友关系与申请流
- 群权限、禁言、慢速模式、加群审批
- 群成员头衔、等级、签到
- 动态：发布、评论、点赞
- 组队中心、任务系统
- 公告与通知

</td><td width="33%" valign="top">

**平台**
- 机器人 API（Key 鉴权 + WS 事件）
- 内容自动审核接入点
- 举报处置与审计日志
- 用户封禁与密码重置审批
- CCW 账号绑定
- S3 / 本地双存储

</td></tr>
</table>

机器人接入文档见客户端仓库的 [`docs/bot-api.md`](https://github.com/kukemc/KukeChat-Client/blob/main/docs/bot-api.md)。

## 快速开始

### Docker（推荐）

```bash
git clone https://github.com/kukemc/KukeChat-Server.git
cd KukeChat-Server
cp .env.example .env

# 生成签名密钥并写入 .env
python -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_urlsafe(64))"

docker compose up -d
```

服务监听 `http://127.0.0.1:8000`，接口文档在 `/docs`。

> Docker 部署时建议把数据库放进挂载卷，在 `.env` 里设置：
> `DATABASE_URL=sqlite+aiosqlite:///./data/kukechat.db`

### 本地开发

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # 记得填 JWT_SECRET_KEY
uvicorn app.main:app --reload
```

跑测试：

```bash
pip install pytest
python -m pytest tests/ -q
```

## 配置

所有配置通过 `.env` 注入，完整清单见 [`.env.example`](.env.example)。几个要点：

| 变量 | 说明 |
| --- | --- |
| `JWT_SECRET_KEY` | **必填**。未设置、仍是占位值或短于 32 字符时服务**启动即失败** |
| `CORS_ORIGINS` | 必须包含你的前端来源，默认只有本地开发地址 |
| `DATABASE_URL` | 默认 SQLite；换 PostgreSQL 等只需改这一项（需装对应异步驱动） |
| `REDIS_URL` | 单进程可留空；**多 worker / 多实例时必填**，否则 WebSocket 无法跨进程广播 |
| `STORAGE_BACKEND` | `local` 或 `s3` |
| `CONTENT_MODERATION_ENABLED` | 默认关闭。本项目不自带审核服务，启用时必须提供 `CONTENT_MODERATION_URL` |
| `CCW_BIND_*` | CCW 账号绑定，留空则该功能整体不可用。另外还需要接入 CCW 网关，见下面的「集成点」 |

仓库不内置任何生产地址、密钥或运营方标识。

### 集成点

有两处功能依赖外部平台，**怎么向外部平台取数不在这个仓库里**。仓库只定义接口（`app/integrations/`）和占位实现，
业务逻辑（绑定流程、资料同步、商店、作品链接卡片怎么拼）都在开源的部分：

| 接口 | 文件 | 提供什么 | 没有实现时 |
| --- | --- | --- | --- |
| CCW 网关 `CcwGateway` | `app/integrations/ccw_gateway.py` | `latest_comments(subject_oid, subject_type)` 某个评论区最新一页评论；`student_payloads(student_oid)` 某个学生的资料与主页统计；`creation_detail(oid, access_key)` 某个作品的详情。取不到统一抛 `CcwUnavailable` | CCW 账号绑定显示为未开放（发起绑定返回 503，轮询评论的后台任务不启动）；资料同步、消息 / 动态 / 组队里的作品链接卡片静默跳过；用评论验证码改密码不可用 |
| 金币账本 `CoinLedger` | `app/integrations/coin_ledger.py` | `credited_to(account_id)` 某个账号累计入账多少；`recent_credits()` / `all_credits()` 名单；`topup_url()` 充值页；`configured()` / `source()`。核实不了统一抛 `CoinsUnavailable` | 商店能逛、能领等级解锁的商品、能装备；余额恒为 `stale`，花币购买返回 503（详见 [`docs/shop.md`](docs/shop.md)） |

**自己部署时怎么提供实现**：在 `app/_private/`（已在 `.gitignore` 里，不会入库）放一个 Python 包，导出两个不带参数的工厂函数：

```python
# app/_private/__init__.py
from app.integrations import CcwGateway, CoinLedger

def create_ccw_gateway() -> CcwGateway: ...
def create_coin_ledger() -> CoinLedger: ...
```

启动时 `app/integrations/loader.py` 发现这个包就用它，没有就用占位实现。这个包存在但有问题（导入报错、缺工厂函数、
返回的不是接口的实现）时服务**启动即失败**，不会悄悄退回占位实现。两个接口各自的约定写在对应文件的文档字符串里；
实现要自己负责不阻塞事件循环（同步请求放进线程池）。只想接其中一个，另一个工厂返回 `NullCcwGateway()` / `NullCoinLedger()` 即可。

测试里用 `integrations.use_ccw_gateway(...)` / `use_coin_ledger(...)` 换成假实现，例子见 `tests/integration_fakes.py`。
本仓库的全部测试在没有 `app/_private` 的情况下通过。

### 关于 JWT_SECRET_KEY

服务会在启动时拒绝空值、已知占位值和过短的密钥。这是刻意的：一个可预测的签名密钥
意味着任何人都能伪造任意用户（包括管理员）的访问令牌。请务必用随机值，并且**不要**
在多个环境之间复用。

## 项目结构

```text
app/
  api/v1/       REST 端点，按业务域拆分
  core/         配置、数据库、鉴权依赖、安全工具
  integrations/ 外部平台的集成点：只有接口与占位实现（见「集成点」）
  models/       SQLAlchemy 模型
  schemas/      Pydantic 请求/响应模型
  services/     业务逻辑
  websocket/    连接管理与实时路由
tests/          单元测试
deploy_start.py 生产启动脚本（Gunicorn + Uvicorn Worker）
```

## 生产部署

`deploy_start.py` 会检查依赖并用 Gunicorn 启动：

```bash
python deploy_start.py
```

建议监听 `127.0.0.1:8000`，由 Nginx 反向代理到 HTTPS。反代必须保留这些请求头，
否则真实 IP 识别和 WebSocket 都会出问题：

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

**不要覆盖的运行数据**：数据库文件、`uploads/` 目录、`.env`。

## 贡献

欢迎 Issue 和 Pull Request。提交前请确保 `python -m pytest tests/ -q` 通过。

两条硬性约定：

1. **不要在代码里写死服务器地址、密钥或运营方标识**，一律走 `app/core/config.py` + `.env.example`。
2. **不要放宽管理员对消息的读取范围**。任何新增的消息读取路径都必须经过
   `ensure_message_is_under_report()`，并调用 `log_sensitive_read()` 留痕。

## License

[MIT](LICENSE) © kukemc
