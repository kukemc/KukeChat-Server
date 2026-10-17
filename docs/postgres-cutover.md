# 从 SQLite 切到 PostgreSQL：切换手册

## 为什么换

SQLite 只有一把整库写锁。线上多次出现「某处代码在事务里写了数据、然后去等网络」，写锁一直不放，全站写操作都
`database is locked`，连接池被占满，服务反复卡死。PostgreSQL：

- 行级锁：一处 bug 只锁住它碰过的那几行，别的写照常进行；
- 数据库角色上设了三个超时兜底，坏事务会被数据库自己杀掉，而不是拖死整个服务：
  - `idle_in_transaction_session_timeout = 30s`：开着事务发呆（比如事务里去等网络）超过 30 秒，连接被断开、事务回滚；
  - `statement_timeout = 30s`：单条语句跑超过 30 秒被取消；
  - `lock_timeout = 10s`：等锁超过 10 秒报错，而不是无限排队。

代码同时支持两种库，靠 `DATABASE_URL` 切换：

```text
DATABASE_URL=sqlite+aiosqlite:///./kukechat.db                         # 原来的
DATABASE_URL=postgresql+asyncpg://kukechat:<密码>@127.0.0.1:5432/kukechat   # 切换后
```

测试和本地开发继续用 SQLite 即可；测试也能整套跑在 PG 上（见文末）。

## 整体思路

分两次上线，把「换代码」和「换库」拆开，每一步都能单独回滚：

1. **先部署新代码，库还是 SQLite**（不停服，和平时发版一样）。新代码在 SQLite 上的行为和原来一致，先在线上跑一阵。
2. **维护窗口里换库**：停服 → 最后备份 SQLite → 迁移到 PG → 改 `.env` 的 `DATABASE_URL` → 启动 → 验证 → 开服。

本地用 2026-10-17 的线上热备份（556 MB，96 万行）演练过，迁移脚本本身约 2.5 分钟（见文末「演练记录」）。
**预计停服 10～15 分钟**，大头是验证。

下文 `$HOST` 是后端服务器（地址见私有运维仓库），后端目录 `/www/wwwroot/api`，服务 `kukechat-api`，
Python `/www/server/pyporject_evn/versions/3.11.15/bin/python3.11`（包装在 www 用户的 `~/.local`）。

## 第 0 步：准备（不停服，提前一天做）

### 0.1 装 asyncpg（约 1 分钟）

```bash
ssh root@$HOST
PY=/www/server/pyporject_evn/versions/3.11.15/bin/python3.11
sudo -u www -H $PY -m pip install --user 'asyncpg>=0.29.0'
sudo -u www -H $PY -c 'import asyncpg; print(asyncpg.__version__)'
```

### 0.2 确认 PostgreSQL 和角色设置（约 1 分钟）

```bash
sudo -u postgres psql -d kukechat -c "\drds kukechat"     # 应看到三个超时和 timezone=UTC
sudo -u postgres psql -d kukechat -c "\dn+ public"        # kukechat 对 public 要有 CREATE 权限（库属主默认就有）
sudo -u postgres psql -c "SHOW max_connections"           # 默认 100：两个 worker × (10 + 10) = 40，够
```

迁移和启动时会自动 `CREATE EXTENSION IF NOT EXISTS pg_trgm`（聊天记录搜索的三元组索引）。pg_trgm 是可信扩展，
库属主能装；Debian 的 `postgresql-15` 包自带 contrib。万一装不上，搜索照样能用，只是没有索引、慢一些；
也可以提前用超级用户装好：`sudo -u postgres psql -d kukechat -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"`。

### 0.3 部署 postgres 分支（库还是 SQLite，约 2 分钟，不停服）

在本地私有仓库根目录（`KukeChat/`）：

```powershell
# 预检
.\ops\deploy-backend.ps1 -ServerRepo ..\_pg-server -HostName $HOST
# 先在服务器上做一次 SQLite 热备份，再正式发布
.\ops\deploy-backend.ps1 -ServerRepo ..\_pg-server -Apply -DatabaseBackupConfirmed -HostName $HOST
```

（`-ServerRepo` 指向 postgres 分支的工作目录；或者先把 postgres 分支合进 `main` 再按平时流程发。）

上线后观察半天：日志里没有新的报错、`init_db` 没有报「missing columns」，聊天、红包、商店、群文件都正常。
这一步出问题就按平时的方式回滚代码，和换库无关。

### 0.4 在服务器上用一份热备份再演练一次（可选，约 5 分钟，不停服）

```bash
cd /www/wwwroot/api
PY=/www/server/pyporject_evn/versions/3.11.15/bin/python3.11
sudo -u postgres createdb -O kukechat kukechat_rehearsal
export PGURL="postgresql+asyncpg://kukechat:$(cat /root/.kukechat-pg-pass)@127.0.0.1:5432/kukechat_rehearsal"
$PY scripts/migrate_sqlite_to_postgres.py --sqlite backups/<最新的>.db --pg "$PGURL" --report /root/pg-rehearsal.json
unset PGURL
sudo -u postgres dropdb kukechat_rehearsal
```

（`$(cat …)` 只在命令行里展开，不会打印出来；别用 `set -x`。）

## 第 1 步：维护窗口

提前在客户端公告里说一声（比如「今晚 2:00 起维护 15 分钟」）。下面每一步后面是预计耗时。

### 1.1 停服（< 1 分钟）

```bash
ssh root@$HOST
cd /www/wwwroot/api
systemctl stop kukechat-watchdog.timer      # 先停看门狗，不然它会在 2 分钟内把服务拉起来
systemctl stop kukechat-api
systemctl is-active kukechat-api            # 应输出 inactive
```

### 1.2 最后一次备份 SQLite（约 30 秒）

服务停了、没有写入，直接用 SQLite 的在线备份接口拷一份（会把 WAL 一起合进去）：

```bash
PY=/www/server/pyporject_evn/versions/3.11.15/bin/python3.11
STAMP=$(date +%Y%m%d-%H%M%S)
$PY -c "import sqlite3,sys; s=sqlite3.connect('kukechat.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); print(s.execute('pragma quick_check').fetchone()[0])" backups/kukechat-before-pg-$STAMP.db
ls -l backups/kukechat-before-pg-$STAMP.db
```

输出 `ok` 才继续。这份备份就是回滚时的基准。

### 1.3 迁移（约 2～3 分钟）

```bash
export PGURL="postgresql+asyncpg://kukechat:$(cat /root/.kukechat-pg-pass)@127.0.0.1:5432/kukechat"
$PY scripts/migrate_sqlite_to_postgres.py --sqlite backups/kukechat-before-pg-$STAMP.db --pg "$PGURL" --report /root/pg-migrate-$STAMP.json
echo "exit=$?"
unset PGURL
```

- 目标库必须是空的（有数据会拒绝；确认要重来才加 `--truncate`）。
- 整个导入在一个事务里，中途失败会整体回滚，修好后加 `--truncate` 重跑。
- 退出码 0 = 行数、抽样、序列全部核对通过；1 = 有核对没过（看最后的汇总）；2 = 没开始（参数 / 源库 / 目标库问题）。
- 脚本最后打印脏数据汇总（孤儿外键、超长、NUL 字符、无效 JSON……）和每类的处理方式。演练时见到的见文末。

### 1.4 改 `.env`（< 1 分钟）

原来那行注释掉留着，回滚时改回来就行。只改这一行，不读、不打印其余内容：

```bash
ls -l .env                                    # 记下属主和权限
cp -p .env .env.before-pg-$STAMP
sed -i 's|^DATABASE_URL=sqlite|# DATABASE_URL=sqlite|' .env
echo "DATABASE_URL=postgresql+asyncpg://kukechat:$(cat /root/.kukechat-pg-pass)@127.0.0.1:5432/kukechat" >> .env
grep -c '^DATABASE_URL=' .env                 # 应该是 1
grep -o '^DATABASE_URL=[a-z+]*' .env          # 应该是 DATABASE_URL=postgresql+asyncpg
ls -l .env                                    # 属主和权限应该和改之前一样
```

如果 `.env` 里原来没有 `DATABASE_URL` 这一行（用的是默认值），`grep -c` 也是 1，没问题。
密码里如果有 `@ : / ? #` 这类字符，要先做 URL 编码再放进连接串。

### 1.5 启动并验证（约 5～10 分钟）

```bash
systemctl start kukechat-api
sleep 5
systemctl is-active kukechat-api
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health          # 200
journalctl -u kukechat-api --since "-5 min" --no-pager | grep -iE "error|traceback|missing columns" | head
sudo -u postgres psql -d kukechat -c "SELECT application_name, state, count(*) FROM pg_stat_activity WHERE datname='kukechat' GROUP BY 1,2"
```

然后用客户端（自己的号）实测：

- 打开会话列表、进一个群往上翻消息；
- 私聊 / 群聊各发一条消息，另一台设备能实时收到；
- 打开红包列表 / 发一个小红包并领取；
- 群文件列表、商店余额；
- 后台登录，打开风纪委员总览、举报案件；
- 聊天记录搜索搜一个词。

都正常就开看门狗、开服：

```bash
systemctl start kukechat-watchdog.timer
```

### 1.6 切换后一两天里要看的

```bash
# 被超时杀掉的事务 / 语句（说明有代码在事务里等东西，按日志里的位置修）
journalctl -u kukechat-api --since today | grep -E "idle-in-transaction|canceling statement|lock timeout" | head
# 慢查询与锁等待
sudo -u postgres psql -d kukechat -c "SELECT pid, state, wait_event_type, now() - xact_start AS xact_age, left(query, 80) FROM pg_stat_activity WHERE datname='kukechat' AND state <> 'idle' ORDER BY xact_age DESC NULLS LAST LIMIT 20"
```

## 回滚

切换后发现严重问题（启动失败、核心功能报错）：

```bash
systemctl stop kukechat-watchdog.timer
systemctl stop kukechat-api
cp .env.before-pg-$STAMP .env                # DATABASE_URL 回到 SQLite
systemctl start kukechat-api && systemctl start kukechat-watchdog.timer
```

约 1 分钟。代码不用回滚（postgres 分支在 SQLite 上照常跑）；要连代码一起回到旧版本就按平时的方式部署原版本。

**注意**：回滚之后用的是停服时的那份 SQLite，切换后写进 PG 的新消息、红包、购买等会丢。所以要回滚尽量在开服后
很短时间内决定。开服后过了较长时间才发现问题，优先在 PG 上修，而不是回滚。

PG 里的数据不用删，下次重试迁移时加 `--truncate`。

## 改了什么（给以后改代码的人）

- `app/core/database.py`：按 `DATABASE_URL` 选连接参数（PG：每 worker 10 + 10 条连接、`pool_pre_ping`）；SQLite 的
  PRAGMA 只对 SQLite 连接生效；`init_db` 的加列 / 加索引 / 回填改成一张清单，两种库通用（用 SQLAlchemy `inspect`
  查列，PG 用 `ADD COLUMN IF NOT EXISTS`）；两个 worker 同时启动时 PG 上用 advisory lock 串行建表。
  **以后加列**：在 `_ADDED_COLUMNS` 末尾加一行（按 SQLite 写法，DATETIME / BOOLEAN DEFAULT 0 会自动翻译）。
- `app/core/dialect.py`：`upsert_insert`（ON CONFLICT）、`greatest` / `least`（两值取大 / 小）、`hours_since`。
  **不要再直接 `from sqlalchemy.dialects.sqlite import insert`**，也不要用 `func.max(a, b)` 当两值取大（PG 上那是聚合函数）。
- `app/services/schema_extras.py`：聊天记录搜索索引和撤回触发器的 PG 版（plpgsql 触发器 + pg_trgm 索引，搜索用 ILIKE）。
- 并发保护（`lock_for_update` / `lock_row`，只在 PG 上执行）：SQLite 上一个事务写过就拿着整库写锁，很多「先查再写」
  天然全站串行；PG 只锁碰到的行，而且 READ COMMITTED 下等过锁的 UPDATE 里，对别的表的子查询仍是语句开始时的快照。
  所以花币（购买 / 红包 / 送礼 / 群升级 / 后台加扣币）先锁 `shop_accounts` 行，抢红包和结束红包先锁红包行，
  群升级 / 群人数上限 / 群文件空间先锁群行，风纪投票和结案先锁案件行。**以后新写「检查余额 / 名额 / 状态再写」的
  代码，要先用这两个函数锁住那一行**，不要指望第一条 UPDATE 能挡住别的进程。
- `jury_votes.vote` 从 `String(10)` 放宽到 16：装不下 `need_context`。SQLite 不检查长度，PG 会直接报错。
  以后改 `String(n)` 的长度，PG 上要 `ALTER TABLE ... ALTER COLUMN ... TYPE VARCHAR(m)`（init_db 不会改已有列的类型）。
- asyncpg 的 `executemany` 不报 `rowcount`（总是 -1），依赖影响行数的批量写在 PG 上改成了单条多行语句或逐行执行。
- `conversation_members.cleared_before_message_id` 去掉了外键：它是「清空聊天记录」的水位线，被指向的消息删掉后
  水位线也得留着（PG 会严格执行外键的 `ON DELETE SET NULL`，会让清空过的旧消息重新出现）。线上 SQLite 这一列本来就没有外键。
- 写法上要注意的 PG 差异：
  - `LIKE` 在 PG 上区分大小写，搜索用 `.ilike()`（SQLite 上也会编译成不区分大小写）；
  - `GROUP BY` 要写全，`SELECT DISTINCT` 的 `ORDER BY` 列必须在选择列表里；
  - 布尔列和 0 / 1 比较在 PG 上报错，写 `.is_(True)` 或用绑定参数；
  - 读出来的时间带时区（UTC），不要和不带时区的 `datetime` 比；
  - 原生 SQL 里的子查询要有别名。

## 测试跑在 PostgreSQL 上

```bash
TEST_DATABASE_URL=postgresql+asyncpg://kukechat@localhost:5433/kukechat_test python -m pytest tests/ -n 8 -p no:randomly
```

`tests/conftest.py` 会把测试里的 SQLite 引擎换成这个库里的独立 schema（schema 复用，测试之间清表）。
测试库的角色需要能建 schema（库属主即可），库里提前装好 `pg_trgm`（`CREATE EXTENSION pg_trgm`，装在 public）。
只适用于 SQLite 的测试会自动跳过。

## 演练记录

2026-10-17，本地 Windows 机器（PostgreSQL 15.14，fsync 开着），源库是当天 10:36 的线上热备份（556 MB）。

| 阶段 | 耗时 |
| --- | --- |
| SQLite quick_check | 7 s |
| init_db 建表（99 张表、触发器、pg_trgm） | 28 s |
| COPY 导入 963,300 行（messages 33.6 万行约 15 s，group_member_exp_logs 34.6 万行约 7 s） | 约 35 s |
| 重建 393 个非唯一索引 | 29 s |
| 装回 170 个外键并整体校验 | 2 s |
| 重建聊天记录搜索索引（313,917 行） | 27 s |
| 重置 96 个序列 + ANALYZE + 核对 | 12 s |
| **合计** | **142 s** |

这台机器上建文件很慢（每建一个表 / 索引约 45 ms），Linux 服务器上建表、建索引应该更快。

核对结果：99 张表行数全部一致（SQLite 963,300 → PG 963,300，跳过 0 行）；每张表抽首行、末行和 25 行随机行逐列比对，
共 2,015 行，0 不一致；序列全部在最大 id 之后。搜索索引：PG 重建后 313,917 行，和 SQLite 里的逐行比对分类、
文本长度完全一致；SQLite 多出的 1 行指向一条已经不存在的消息（旧的残留）。

迁移后在本地起后端（指向迁移后的库），用一个真实群主的身份请求：会话列表（125 个）、消息分页（两页各 50 条）、
发消息、红包 pending、群文件列表、聊天记录搜索、/auth/me、风纪委员总览，全部 200；新发的消息被 PG 触发器写进了搜索索引；
日志无报错，没有 idle in transaction 的连接。

演练中发现的脏数据（脚本会自动处理并在汇总里列出）：

- `content_moderation_records.content_text`：2 行文本里有 NUL 字符（PG 的 text 不允许），删掉 NUL；
- `admin_audit_logs.details`：12 行不是合法 JSON，原样保留（读的地方有兜底）；
- `conversation_members.cleared_before_message_id`：51 行指向已被修剪的消息。这一列现在没有外键（见上文），原样保留，
  语义不变；
- `group_emojis.sort_order`：备份时线上库还没有这一列，迁移时取默认值（空 = 按原来的顺序）；
- 孤儿外键、超长字符串、无效时间：无。
