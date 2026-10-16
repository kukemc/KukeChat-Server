# 风纪系统 v2 联调实例

给客户端做端到端走查用的本地后端：一条命令起服务，库里已经有一期、二期、三期（`jury-v2.md` 第 0–3 节）要用到的全部场景。

只在本机跑。数据库和上传的图片都在系统临时目录里，不连任何真实数据库、Redis、对象存储、审核服务、邮件或 CCW。

## 启动

```bash
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo.py              # 没有库就建库灌数据（约 50 秒），然后起服务
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo.py --reset      # 删库重建，再起服务
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo.py --seed-only  # 只建库灌数据，不起服务
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo.py --port 8791
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo.py --db-dir kukechat-jury-demo-test --port 8793   # 另起一套互不影响的实例
```

| | |
| --- | --- |
| 监听 | `127.0.0.1:8791`，单进程。`--port` 改端口，`--host 0.0.0.0` 让局域网里的设备也能访问 |
| 数据库 | `%TEMP%\kukechat-jury-demo\jury-demo.sqlite3`（启动时会打印完整路径） |
| 上传目录 | `%TEMP%\kukechat-jury-demo\uploads\` |
| 换一个目录 | `--db-dir 目录`：写相对路径时相对系统临时目录；必须在临时目录里，否则直接退出。两个实例各用各的目录和端口，互不影响 |
| 停止 | Ctrl+C |
| 重置 | 先停服务，再 `--reset`。服务开着时库文件删不掉，脚本会直接报错 |

库已经存在时不会重新灌数据，直接起服务（只有一、二期数据的旧库会在启动时补上三期；不过旧库里的时间已经旧了，建议直接 `--reset`）。数据里的时间都是按**生成那一刻**往前推的，所以放得越久越「旧」（见最后一节），走查前 `--reset` 一次最省事。灌完数据脚本会自己核对一遍走查依赖的前提，不满足就报错退出。

后端代码更新之后，已经在跑的服务进程里还是旧代码：要停掉重起；表结构有变化时还要 `--reset`。

## 客户端怎么指过来

| | |
| --- | --- |
| 接口基础地址 | `http://127.0.0.1:8791`，接口都在 `/api/v1` 下 |
| 用户实时通道 | `ws://127.0.0.1:8791/ws?token=<access_token>` |
| 后台实时通道 | `ws://127.0.0.1:8791/admin/ws?token=<access_token>` |
| 图片 | `http://127.0.0.1:8791/uploads/...`（接口里给的是 `/uploads/...` 这样的相对路径） |

KukeChat-Client 的 `.env`：

```ini
VITE_API_BASE_URL=http://127.0.0.1:8791
VITE_WS_URL=ws://127.0.0.1:8791/ws
```

它的开发服务器默认把 `/api`、`/uploads`、`/ws` 代理到 `127.0.0.1:8000`；设 `KUKE_DEV_REMOTE=1` 才会改成 `.env` 里的地址。

**登录**：`POST /api/v1/auth/login`，body `{ "username_or_email": "juror", "password": "demo123456" }`（字段名写成 `username` 也收），返回 `{ "access_token", "token_type": "bearer", "user": {…} }`。之后的请求带 `Authorization: Bearer <access_token>`，令牌 7 天有效；这个实例的签名密钥是固定的，重启、`--reset` 之后旧令牌照样能用。

**客户端证明头**：`/api/v1/*` 的每个请求都要带，否则 403。

```text
X-Kuke-Client:    kukechat-web/<版本>        （后台用 kukechat-admin/<版本>）
X-Kuke-Timestamp: 毫秒时间戳                  （和服务器相差不能超过 5 分钟）
X-Kuke-Nonce:     12–64 个字符的随机串
X-Kuke-Proof:     sha256("<METHOD>|<去掉 /api/v1 的路径，不含查询串>|<时间戳>|<nonce>|KukeChat") 的十六进制
```

例：`GET /api/v1/jury/votes?state=open` 参与计算的路径是 `/jury/votes`。`/uploads/*`、`/health`、`/ws` 不需要。

**CORS**：`localhost`、`127.0.0.1`、`tauri.localhost` 的任意端口默认放行。别的来源（比如跑在 `https://www.ccw.site` 页面里的扩展）要在启动时加 `--cors-origin https://www.ccw.site`，可以写多个。

两个容易卡住的地方：

- 网页端算证明头用的是 `crypto.subtle`，它只在安全上下文里有。用 `http://localhost` / `http://127.0.0.1` 打开页面没问题；用局域网 IP（`http://192.168.x.x:5180`）打开时证明头算不出来，所有接口都是 403。
- `POST /auth/cookie-login` 能调通，但它发的 Cookie 是 `SameSite=None` 且不带 `Secure`（这个实例跑在 http 上），浏览器不会保存。走查请用上面的令牌登录。

## 演示库里打开的开关

生产环境里这几个开关默认都是**关**的。演示库为了能走查三期，打开了其中两个：

| 开关 | 演示库 | 生产默认 | 打开之后 |
| --- | --- | --- | --- |
| `appeals` | **开** | 关 | 满足条件的申诉先进资深复审，其余仍直接到管理员（3.2） |
| `machine_review` | **开** | 关 | 机器「拿不准」的群聊文本和公开动态作为没有举报人的案件进委员队列（3.3） |
| `auto_appoint`、`exam_required`、`public_group_content`、`opinions_open_to_jurors` | 关 | 关 | 和生产一样，要看效果在后台 `PUT /admin/jury/settings` 里改 |

`GET /jury/config` 的 `switches` 里因此是 `{ "auto_appoint": false, "appeals": true, "machine_review": true }`。举报人复议、重复举报拦截、资深合议、判例、议事厅没有开关，三期代码上线就生效。

注意这个实例的内容审核服务是关的，所以**新发的消息不会真的变成机器案件**：库里那几件机器来源的案件是灌数据时直接调建案函数建出来的。

## 红包

红包（`red-packets.md`）在演示库里是**开着**的（生产默认关），顺手灌了三个，都在「深夜灌水小分队」群里，发出时间就是灌数据的时刻，24 小时后过期：

| 红包 | 谁发的 | 状态 | 用来看什么 |
| --- | --- | --- | --- |
| #1 拼手气 60 金币 5 份「开工大吉」 | `senior1` | 已抢完 | 领取明细、手气最佳。`juror`、`trainee1`、`trainee2`、`regular2`、`senior2` 各领了一份 |
| #2 拼手气 88 金币 8 份「周末愉快，手快有」 | `juror` | 进行中，被领了 3 份 | 用 `senior2`、`senior3`、`eligible` 这些还没领的账号去抢；`trainee1`、`senior1`、`regular2` 领过了（再领是 409）；没领的人看不到金额 |
| #3 普通 5 金币 × 6 份「恭喜发财」 | `senior2` | 进行中，没人领过 | 从头抢一个 |

这个实例没有真的余额来源：脚本注入了一个只存在于它自己进程里的假账本，**绑定了 CCW 的演示账号各有 1000 金币的累计投币**（没绑定的 `newbie`、`reporter2`、`reporter4`、`appellant` 和 m001–m300 是 0，但都能抢）。所以商店的花币购买在这个实例上也能用。这三个红包的 id 记在 `system_settings` 的 `jury_demo.red_packets` 里。只有前三期数据的旧库在启动时会补上这一期。

## 礼物、生日、限时商品

礼物（`gifts.md`）在演示库里也是**开着**的（生产默认关）。灌了四条送礼记录，都走真的服务层（扣币、心意值、对方的拥有记录、聊天消息都是真的）：

| 记录 | 谁送给谁 | 在哪 | 用来看什么 |
| --- | --- | --- | --- |
| #1 蛋糕 ×3，留言「生日快乐！今天你最大」 | `juror` → `trainee1` | 「深夜灌水小分队」 | 生日当天的礼物（`on_birthday: true`）、群里的礼物卡片和留言、聊天区动效（tier 2） |
| #2 头像框「天使」（限时 30 天），留言「戴上试试」 | `senior1` → `trainee1` | 同上 | 送商店商品：`trainee1` 的商店里多了一件 30 天后到期的头像框，购买记录里 `source = gift` |
| #3 小花 ×9，留言「谢谢你上次帮忙看案子」 | `senior2` → `juror` | 个人空间（不落消息） | 留言只有双方看得到：用第三个账号看 `juror` 的礼物墙记录，这一条的 `message` 是 null；后台流水里也是 null |
| #4 火箭 | `regular2` → `senior1` | 「深夜灌水小分队」 | 全屏动效（tier 3），没有留言 |

- **今天过生日的账号**：`trainee1`（见习小林）。生日设成了灌数据那一天（UTC+8），没公开年份；用它登录，`GET /users/me/birthday` 的 `celebrate_today` 是 true（庆生动画）；别人看到的它 `is_birthday_today: true`。`senior1` 也设了生日（40 天前，公开年份），用来看平时的样子。**只在灌数据的那一天成立**，隔天要 `--reset`。
- **快到期的限时头像框**：`juror` 戴着「小恶魔」，两天后到期。服务启动后的第一次清扫（约 9 秒后）会给它发一次 `shop.item.expiring`——那时候没连上就收不到了，要再看这条提示得 `--reset` 之后先登录再等；两天后到期，自动卸下并发 `shop.item.expired`。商店里这件的 `obtainable` 是 `renew`，再买一次是在原到期时间上加 30 天。
- 心意榜：`trainee1` 180、`senior1` 100、`juror` 9。
- 这四条记录的 id 记在 `system_settings` 的 `jury_demo.gifts` 里。只有前几期数据的旧库在启动时会补上这一期。

## 两种案件编号

委员侧接口（评审台、投票、合议、案件详情、观点区、我的评审、风纪值明细、标判例、发给委员的实时事件）里的 `case_id` 是**评审编号**：每个委员、每个案件各不相同，只对本人有效。公示栏、判例和后台里的 `case_id` 是案件的**真实编号**。两者不能互相代入，拿真实编号或别人的评审编号去调委员侧接口是 404（契约 1.0）。申诉复审 / 复议用的是第三种：复审自己的 `review_id`。

所以这份文档里：

- 说到委员看到的案件，一律用「被举报的那句话」来指，不写编号；
- 写成 `#数字` 的都是真实编号，只在公示栏、判例和后台里用得上。每次 `--reset` 之后真实编号都一样。

要把两者对上：已经公示的案件，委员侧的详情和 `/jury/votes` 里有 `public_case_id`；后台 `GET /admin/cases/{id}` 的 `votes[].review_id` 是每位委员看到的评审编号。

## 账号

密码都是 `demo123456`。

| 用户名 | 昵称 | 身份 | 适合用来看什么 |
| --- | --- | --- | --- |
| `admin` | 演示管理员 | 管理员 | 后台 `/admin/*`：案件队列（含合议队列）、委员名单、题库、考试记录、观点管理、开关、申诉、复审监控、判例、议事厅 |
| `juror` | 阿澄 | 在任 · 正式委员（**主账号**） | 评审台、投后详情、观点区、我的评审；风纪值 562、连续 14 天、5 个成就、三个排行榜都在榜；离资深还差两项；练习场做了 9 道；议事厅里发过帖、投过提案；不是资深，`/jury/panel/next` 是 403 `not_senior` |
| `trainee1` | 见习小林 | 在任 · 见习委员 | 晋升正式的四项条件都已满足：他投过的案件再有一个定案就升为正式（主账号评审台上那条抽奖动态，投「不违规」就会触发）。议事厅里只有评论 |
| `trainee2` | 见习小鹿 | 在任 · 见习委员 | 是「像素画社」成员，那个群的案件分不到他；考了两次才通过；主动回避过两次（都已定案，`/jury/votes` 里 `can_view: false`、`stage: null`） |
| `regular2` | 正式阿木 | 在任 · 正式委员 | 由系统自动从见习晋升（通知中心有一条）；公开了委员身份；是 `offender` 的好友，`offender` 的案件分不到他；议事厅里发过两个提案 |
| `senior1` | 资深老周 | 在任 · 资深委员 | **三期资深侧的主账号**：合议 4 件可取、复审 3 件可取；标过 5 条判例（4 条有效，1 条被管理员撤下了），也有可以标成判例的案件；议事厅里发过提案和讨论帖；公开了委员身份 |
| `senior2` | 资深青禾 | 在任 · 资深委员 | 合议里已经投过一票的那件不会再分给他（3 件可取）；复审 3 件可取；公开了委员身份 |
| `senior3` | 资深南风 | 在任 · 资深委员 | `/jury/me.warnings` 里的 `demote_risk`（最近 30 张已结票一致率 75.9%）；合议 4 件可取，复审里已经投过一件（2 件可取） |
| `hesitant` | 犹豫的小周 | 在任 · 见习委员 | `warnings` 里的 `high_abstain`：最近 22 张票里「拿不准 / 上下文不足」占 36.4% |
| `cooldown` | 歇一歇的阿信 | 在任 · 见习委员，弃权冷却中 | `/jury/me.cooldown_until`；取案是 429 `jury_cooldown` |
| `dayfirst` | 今天还没投的小钟 | 在任 · 见习委员 | **看 `daily_first` 用他**：今天（UTC+8）一票没投，投今天的第一张有效票，响应的 `reward` 里有 `daily_first`（+2） |
| `streak6` | 连投六天的小夏 | 在任 · 见习委员 | **看 `streak_7` 用他**：已经连续 6 天有票、今天还没投；投今天的第一张有效票，`reward` 里同时有 `daily_first`（+2）和 `streak_7`（+10），并拿到「七日之约」成就 |
| `suspended` | 被暂停的小北 | 被系统暂停的委员 | `/jury/me` 的 `suspended` 和 `review_note`；通知中心有一条；不能评审，仍能回看投过的案件、能练习 |
| `mutedjuror` | 被禁言的阿泽 | 在任 · 见习委员，禁言中 | `/jury/me.restriction`；取案、发观点、在议事厅发言都是 403 `juror_restricted`；回看和练习场不受影响 |
| `expired` | 任期已满的老陈 | 任期届满的委员 | `/jury/me` 的 `expired`：有违规记录所以没有自动续任，通知中心有一条 |
| `eligible` | 够格的小满 | 普通用户，四项条件都满足 | **没考过，可以直接开考**；申请入口；练习场；总览、公示栏、判例、议事厅的非委员视角 |
| `eligible2` | 够格的小雪 | 普通用户，四项条件都满足 | 第二个可以直接开考的账号 |
| `examfail` | 没考过的小鲁 | 普通用户，5 小时前考了 6 分 | `GET /jury/exam` 的 `last_result` / `next_attempt_at`；开考是 429 `exam_cooldown` |
| `exampass` | 考过了的小顾 | 普通用户，两天前考了 9 分，还没申请 | `exam.passed: true`；`POST /jury/apply` 之后带成绩进审批队列；练习场做完了一整轮（`round.done == round.total`） |
| `examout` | 考了三次的小范 | 普通用户，30 天内三次都没过 | 开考是 429 `exam_attempts_exhausted` |
| `newbie` | 新来的豆豆 | 普通用户，注册 2 天 | `/jury/me.requirements` 里三项不达标；考试和练习都是 403 `jury_not_eligible`；是「周末联机」的成员，可以用他试「重复举报」 |
| `applicant` | 待审批的阿宁 | 普通用户，已申请待审批 | `/jury/me` 的 `applied`；后台审批队列里带着考试成绩（9 分） |
| `reporter1` | 举报人小艾 | 举报人 | `GET /reports/me`：各种处理结果；一条**可以申请复议**（`can_reconsider: true`），一条复议转给了管理员、还没结果（`pending`） |
| `reporter2` | 举报人小贝 | 举报人 | 同上；一条复议正在资深复审里（`reconsider_status: "pending"`） |
| `reporter3` | 举报人小柯 | 举报人 | 同上；一条复议已维持原结论（`upheld`），通知中心有一条 |
| `reporter4` | 举报人小丁 | 举报人 | 同上；一条复议已改判为违规（`overturned`，举报变成「已处理」），通知中心有一条 |
| `offender` | 被举报的小黑 | 被处罚的用户 | `GET /moderation/me`：一条可以申诉的处罚。它的原审里有一位资深委员，凑不齐三位，所以**申诉会直接到管理员** |
| `appellant` | 申诉中的小白 | 被处罚的用户 | 一条申诉待处理的处罚：是开关打开之前提交的，在管理员手里（`appeal_stage: "admin"`） |
| `appealing` | 申诉中的小何 | 被处罚的用户，禁言中 | 四条处罚，申诉都还没结果：两条在资深复审里（委员定案的、机器处罚的各一条），一条复审超时转了管理员，一条私聊相关的直接到管理员 |
| `punished` | 被处罚的小乔 | 被处罚的用户 | 三条申诉已由资深复审出了结果（维持 / 减轻 / 撤销各一条），通知中心有三条；另有一条还没申诉的 |

另有 `m001`–`m300`：批量建的大厅成员，充当路人和被举报人，同一个密码，一般用不到。其中 `m011`–`m016` 是三期追加的 6 位委员（正式 3、见习 3）：只用来给三期的案件凑原审的票、在议事厅里说话，这样一、二期里那几位算好了一致率和晋升进度的委员不受影响。

总览里的在任委员是 18 人：见习 10（含禁言中、冷却中的）、正式 5、资深 3。

### 三期各场景用哪个账号

| 想看什么 | 账号 |
| --- | --- |
| 资深合议：取件、投票、定案、回避 | `senior1`（主）、`senior2`、`senior3` |
| 合议定案之后原审委员看到的样子（`verdict.source: "panel"`、`tally.panel`） | `juror` |
| 不是资深委员调合议 / 复审接口（403 `not_senior`） | `juror` |
| 申诉复审，资深侧：取件、三种投票、`/jury/appeals/mine` | `senior1`（主）、`senior2`、`senior3` |
| 申诉复审，被处罚人侧：进行中的四种状态 | `appealing` |
| 申诉复审，被处罚人侧：已出结果的三种 + 一条可以现提交的 | `punished` |
| 提交之后直接到管理员的申诉 | `offender`（凑不齐资深委员）、`appealing` 里私聊的那条 |
| 机器来源的案件：评审台上 | `juror` 或任何在任委员：排在评审台的最后三件，要先把前面举报来的案件评完（或回避掉） |
| 机器来源的案件：已公示的 / 「未作处理」的 | 公示栏 #137（任何账号）；`juror` 的「我的评审」里那条 `verdict_source: "auto"` 的 |
| 举报人复议，举报人侧 | `reporter1`（可申请、转管理员）、`reporter2`（进行中）、`reporter3`（已维持）、`reporter4`（已改判） |
| 举报人复议，资深侧 | `senior1` / `senior2` / `senior3`（`/jury/appeals/next` 里 `type: "reconsider"` 的那件） |
| 重复举报被拦（409 `already_reviewed`） | `newbie`（「周末联机」群里那句话） |
| 判例：能标的、自己创建的 | `senior1` |
| 判例：在任委员能看匿名上下文 | `juror` |
| 判例：普通用户只读 | `eligible`、`eligible2` |
| 议事厅：发帖、表决、评论 | `juror`（正式）、`senior1`（资深） |
| 议事厅：只能评论和表决的见习委员 | `trainee1`、`trainee2`、`hesitant` |
| 议事厅：普通用户只读（也可以举报） | `eligible`、`eligible2` |
| 议事厅：管理员（公告、采纳、关闭、处理举报） | `admin` |
| 通知：晋升 / 暂停 / 任期届满 | `regular2` / `suspended` / `expired` |
| 通知：申诉结果（`appeal_result`） | `punished`（已有三条）；走查时让资深委员把 `appealing` 的复审投完，他也会收到 |
| 通知：复议结果（`reconsider_result`） | `reporter3`、`reporter4`（已有）；`reporter2` 的投完之后也会有 |
| `daily_first` / `streak_7` | `dayfirst` / `streak6` |

## 一期的走查

群：「创作大厅」是 `open` 的大群，330 人，超过大型公开群阈值（200）——所有委员都在里面，但它的案件照样能分给他们（只回避在案件上下文里说过话的人）；「像素画社」「编程互助组」是要审批的小群，「周末联机」是刚建的公开小群；「深夜灌水小分队」是闲聊群，没有案件。

### 表决规则

有效票（不违规 / 违规的三档）全部一致且不少于 3 张，当场定案。一旦出现分歧，就要收满 5 张有效票，其中至少 4 张一致才定案，否则转出。所以 3 比 1 不会定案。「拿不准」「上下文不足」不算有效票；按现有票会定为严重违规（l3）时，必须收满 5 张有效票。

转出去的案件（五票没有共识、两张「上下文不足」、72 小时超时）先看此刻凑不凑得齐资深合议庭：有 3 位没参加过原审、不用回避的资深委员就进合议，否则转管理员。这个库里只有三位资深委员，所以**原审里只要有一位资深委员投过票，转出时就直接到管理员**。

### 主账号的评审台

现在能分给他 18 个案件：前 15 个是举报来的，按下面的顺序出来；最后 3 个是机器来源的（见三期）。质控题混在里面：他距上一道质控题已经投了 10 票，所以**取到的第一件一定是质控题**，之后平均每 5 件左右一道。质控题在投票之前认不出来：评审编号和普通案件是连着的，内容的时间也平移到了近一两天（投完揭晓之后才是真实时间）。

| 顺序 | 被举报的内容（开头） | 来自 | 看点 |
| --- | --- | --- | --- |
| 1 | 「再让我看见你发作品，我就找人去你学校门口堵你…」 | 大厅 | 三人举报；已有 3 张严重违规票。严重违规要 5 张有效票：再投一张严重违规**仍不定案**。投「不违规」就出现了分歧，同样要等第 5 张 |
| 2 | 「就你这种智商还学编程？…」 | 大厅 | 三人举报；上下文前后各 10 条；已有 2 张违规票。**再投一张违规票（哪一档都行）就是 3 张一致，当场定案**；投「不违规」则出现分歧，要收满 5 张 |
| 3 | 「官方内测资格免费领！…」 | 公开动态 | 带 1 张图；还没人投 |
| 4 | 「低价转让会员账号，先付款后给密码…」 | 大厅 | 已有 4 张有效票 2 比 2。**再投任何一张有效票都是 3 比 2：收满 5 张没有 4 张一致，转出**。三位资深委员都在原审里投过票，所以直接到管理员（投后 `stage` 是 `admin`） |
| 5 | 一张图片（二维码广告） | 大厅 | 被举报的是一条**图片消息**：`type: "image"`，图片地址在 `content` 里 |
| 6 | 「带你这种猪队友我宁可单机…」 | 周末联机 | 上下文很短（前 2 后 1）；两人举报 |
| 7 | 「签到签到签到…（今天第 20 遍）」 | 大厅 | 举报理由是「其他」（`suggested_category: "other"`），**投违规票时要自己选类别**；已有一张选了「刷屏」的轻微票 |
| 8 | 「【福利】关注我并转发这条动态…」 | 公开动态 | 带 2 张图；已有 2 张不违规票。**再投一张不违规票就是 3 张一致，当场定案**——其中一张是 `trainee1` 的，定案后他会晋升为正式委员 |
| 9 | 「加群看更多内部素材，下面两张是样品…」 | 大厅 | **文字带两张图**（在 `images` 里）；上下文里还有一条图片消息；已有 1 张违规票 |
| 10 | 「抄我代码还不署名，你就是个小偷…」 | 编程互助组 | 上下文里有一条已撤回的消息（`content` 是 null） |
| 11 | 「画成这样也敢发群里？…」 | 像素画社 | `trainee2` 是这个群的成员，分不到 |
| 12 | 「说实话，这届比赛前三名的作品…」 | 公开动态 | 看起来只是发牢骚 |
| 13 | 「某些人自己菜还到处举报别人…」 | 公开动态 | 作者是 `offender`，`regular2` 分不到 |
| 14 | 「懂的都懂，上次那件事就是他干的…」 | 大厅 | 已有 1 张「上下文不足」（资深委员投的）。**再投一张「上下文不足」就转出**（`jury_outcome: need_context`，凑不齐合议庭，到管理员） |
| 15 | 「结局就是主角死了，没看过的活该…」 | 大厅 | 理由是「其他」 |
| 16–18 | 机器来源的三件 | 大厅 ×2、公开动态 | 见三期「机器来源的案件」 |

任何一个案件都可以用来试「主动回避」（`vote: "recuse"`）：响应里 `case` 是 null，之后在 `/jury/votes` 里是一条 `can_view: false`、`stage: null` 的记录，带 `state` / `outcome` / `agreed` 筛选时不会返回。主账号今天还剩 4 次回避。回避不占每天 30 件的名额。

### 主账号投过票、还没定案的（`GET /jury/votes?state=open`）

| 被举报的内容（开头） | 情况 |
| --- | --- |
| 「就你这水平还出教程教别人？…」 | 大厅消息。四个人投过票（一人弃权）。**观点区最全**：有赞数到 3 的、主账号赞过的、回复、回复别人的回复、一条被两人举报后折叠的、一条本人删除后留下的占位 |
| 「收徒！手把手教你做爆款游戏…」 | 公开动态。主账号投了不违规，另一位投了违规，各写了一条备注 |
| 「哈哈哈哈哈哈…」 | 编程互助组的消息。主账号投的是「拿不准」 |
| 「谁要游戏激活码生成器…」 | 大厅消息。五票 3 比 2 没有共识，原审里有两位资深委员，凑不齐合议庭，已经转给管理员（`stage: "admin"`） |
| 「你上次说的那个事，我可都记着呢…」 | 大厅消息。主账号投的是「上下文不足」，案件还在委员队列里 |
| 「说实话这游戏做得跟屎一样…」 | 大厅消息。五票 3 比 2 没有共识，**正在资深合议中**（`stage: "panel"`，这一条的 `vote_stage` 是 `jury`） |

另有一条主账号 5 小时前**主动回避**的记录（「隔壁三班的小林…」）：`vote: "recuse"`、`can_view: false`、`stage: null`，点不进详情；它只在不带 `state` 筛选的列表里出现。

### 主账号的历史

66 张票（另有 1 次回避），48 张已出结论、42 张一致（一致率 0.875），质控题 9 道对 8 道；投票分散在近 48 天。里面有委员共识认定违规 / 不违规的、一致的和不一致的、管理员定案的、合议定案的、被改判的、带类别的违规票，还有一个以「未作处理」结案的机器案件。定案超过 30 天的 4 个案件 `content_available` 是 false。

### 公示栏（真实编号）

62 个案件。

| 形态 | 例子 |
| --- | --- |
| 公开动态、认定违规，带内容 | #33、#42、#48、#62、#99、#111、#137 |
| 群聊消息、认定违规，不带内容（`group_message`） | 很多 |
| 认定不违规，不带内容（`clean`） | 很多 |
| 隐私类，不带内容（`sensitive`） | #45：委员没有共识，管理员按「泄露隐私」定案，`source` 是 `admin` |
| 已改判，不带内容（`overturned`） | #51、#133：违规 → 不违规（#133 是申诉被资深复审撤销）；#60、#142：不违规 → 违规（#142 是举报人复议后改判） |
| 委员没投完、管理员定案 | #50：超时转出，只有一张票 |
| 资深合议定案（`source: "panel"`） | #126。公示栏的 `tally` / `votes` 仍只是原审的五张票 |
| 机器来源（`case_origin: "machine"`） | #137：`report_count: 0`，`reasons` 是 `[{ "reason": "机器筛查 · 诈骗", "count": 1 }]` |
| 是判例的（`precedent_id` 有值） | #42、#48、#55、#67、#69、#109 |
| 定案不足 72 小时，**不在**公示栏 | #72–#75、#112–#119、#136、#146 等 |
| 处罚有待处理的申诉，**不在**公示栏 | #65（`appellant` 的动态）。后台通过申诉后它会以「已改判」回到公示栏 |

`GET /jury/public/cases/{id}/opinions` 默认是 403 `opinions_closed`；后台把 `opinions_open_to_jurors` 打开后，在任委员可以只读。

### 管理员队列（`GET /admin/cases?queue=admin`，真实编号）

#92 理由是「色情低俗」，整类不进委员队列；#93 举报用户；#94 举报群聊；#95 举报私聊消息；#79 委员没有共识、凑不齐合议庭（`panel_outcome: "unavailable"`）；#121 两张「上下文不足」转出，同样凑不齐；#127 进了合议，48 小时没出结果转来的（`panel_outcome: "timeout"`）。

### 处罚

`offender` 的那条来自 #67（委员认定违规），可以申诉；`appellant` 的那条来自 #65，申诉待处理；`mutedjuror` 是管理员直接禁言 14 天；`expired` 有一条轻微的违规记录。`appealing`、`punished` 见三期。

## 二期的走查

`GET /jury/config` 的 `capabilities` 里有 `exam`、`practice`、`votes_v2`、`points`、`tiers`、`achievements`、`leaderboard`、`badge`。

### 入门考试

| 想看什么 | 用哪个账号 |
| --- | --- |
| 从头考一场：开考、答题、交卷、看每题的标准答案和讲解 | `eligible`、`eligible2`（都没考过） |
| 考过没通过，冷却中（`next_attempt_at`，开考 429 `exam_cooldown`） | `examfail`，约 19 小时后可以重考 |
| 30 天内 3 次用完（429 `exam_attempts_exhausted`） | `examout` |
| 已经通过、还没申请 | `exampass`：`POST /jury/apply` 之后是 `applied`，后台能看到成绩 |
| 带着成绩等审批 | `applicant` |
| 不满足资格（403 `jury_not_eligible`） | `newbie` |

题库里用于考试和质控的题有 39 道，够开考。考试只判「违规 / 不违规」；8 分通过。想故意考不过就乱选或者少答几道，没答的按答错算。考试题和质控题一样是旧案件，内容的时间平移到了开考之前 2–40 小时。交过卷再交是 409 `exam_closed`，`detail.reason` 是 `submitted`（超时作废的是 `expired`，要等 60 分钟加 90 秒宽限，这里没法现场看）。

`auto_appoint`、`exam_required` 两个开关默认都是关的，在后台 `PUT /admin/jury/settings` 里改：`auto_appoint` 打开后，通过考试的人交卷（或申请）时直接成为见习委员；`exam_required` 打开后，没通过考试的人申请是 403 `exam_required`。

### 练习场

- `juror`：做过 9 道（对 7 道），这一轮 32 道还没做完；最近 24 小时做了 2 道，所以 `left_today` 是 18、`resets_at` 有值。
- `exampass`：做完了一整轮，`round.done == round.total`，再取到的都是 `answered_before: true` 的题。
- `examfail`、`eligible`、`eligible2`：没练过，`resets_at` 是 null。一口气做 20 道可以看到 `left_today: 0` 和 429 `practice_daily_limit`。
- `mutedjuror`、`suspended`：受处罚 / 被暂停也可以练。

练习场出的题（32 道）和考试、质控用的题（39 道）不重叠。成了判例的案件恒属练习那一份。

### 新的投票选项

- **主动回避**：任何案件都行。`trainee2` 的 `/jury/votes?vote=recuse` 里有两条已经定案的回避记录（`stage`、`outcome` 都是 null；加上 `&state=closed` 或 `&outcome=…` 就一条都不返回）。
- **上下文不足**：评审台第 14 个案件已有 1 张，再投一张就转出。主账号自己也投过一张（见上面「投过票、还没定案的」）。
- **选类别**：评审台第 7 个案件理由是「其他」。别的案件的 `suggested_category` 是按举报理由映射好的默认值。
- **严重违规要 5 票**：评审台第 1 个案件。
- **`reward`**：质控题当场给 `quality_correct` / `quality_wrong`；案件定案的加分不在响应里，走 `jury.points.changed` 事件。
- **`daily_first`**（每天第一张有效票 +2）：主账号和其他有名字的委员今天都已经投过票，看不到。**用 `dayfirst`**：他今天（UTC+8）一票没投，也没有当天的 `daily_first` 流水，评审台上有案件；登录后随便投一张「不违规 / 违规」，响应的 `reward.items` 里就有 `daily_first`。同一天的第二张不再给。「拿不准」「上下文不足」「回避」不算有效票，不触发。
- **`streak_7`**（每连满 7 天 +10）：**用 `streak6`**，他前 6 天每天都有票、今天还没投；今天的第一张有效票会同时带来 `daily_first` 和 `streak_7`，成就里的「七日之约」也会点亮。

  这两个账号「今天还没投」的状态只在生成数据的当天成立（按 UTC+8 的自然日）：过了零点 `streak6` 的连续天数就断了，要 `--reset`。灌完数据脚本会自检这两点。

### 风纪值、层级、成就、排行榜

- `juror` 的 `GET /jury/me/points`：一、二期的 10 种流水里除了「通过入门考试」之外都有（一致、不一致、质控对 / 错、每日首票、连续 7 天、观点获赞、改判补差、管理员调整）。「通过入门考试」看 `trainee1`、`trainee2`、`hesitant`。三期新增的「合议与结论一致」（`panel_agreed`）、「参与复审」（`review_vote`）看 `senior1`；「参与合议」（`panel_disagreed`，合议票与结论不一致）库里没有现成的，要现场投出来。
- `juror` 的 `tier_progress`（离资深）：已出结论的票 48 / 50 ✗，一致率 0.875 / 0.8 ✓，质控正确率 0.889 / 0.8 ✓，在任天数 52 / 60 ✗。
- `trainee1`：四项都 ✓ 但还是见习——层级只在有票出结论时检查。让他投过的任何一个案件定案（最方便的是用主账号把评审台上那条抽奖动态投成「不违规」），他就升为正式，并收到通知和 `jury.status.changed`。
- `senior3`：`demote_risk`。`hesitant`：`high_abstain`。`cooldown`：`cooldown_until`。预警文案里的比例保留一位小数（如 36.4%）。
- `juror` 的成就：已获得「第一案」「初窥门径」「七日之约」「言之有物」「正式委员」；「驾轻就熟」48 / 50、「风雨无阻」14 / 30 等有进度。
- 排行榜：周榜 11 人左右（看今天是周几）、月榜和总榜 18 人；`senior1`、`senior2`、`regular2` 是公开身份的，其余匿名；`juror` 在榜上（匿名，`me: true`）。
- 公开展示身份：用 `juror` 调 `PUT /jury/me/settings { "badge_visible": true }`，之后 `GET /users/{id}`、排行榜、他的消息发送者里的 `jury_badge` 都会变成 `regular`。大厅最近的几条消息里已经有三位带徽章的委员。

### 通知中心（`GET /notifications`，`type: "jury"`）

| `payload.kind` | 现成的 | 现场怎么触发 |
| --- | --- | --- |
| `promoted`（晋升） | `regular2` | 让 `trainee1` 投过的案件定案 |
| `suspended`（资格被暂停） | `suspended` | — |
| `term_expired`（任期届满未续任） | `expired` | — |
| `appeal_result`（申诉有结果了） | `punished`（三条：维持 / 减轻 / 撤销） | 资深委员把 `appealing` 的复审投完 |
| `reconsider_result`（复议有结果了） | `reporter3`（维持）、`reporter4`（改判） | 资深委员把 `reporter2` 的那件投完，或后台处理 `reporter1` 的那件 |

### 后台

- `GET /admin/jury/questions`：71 道题。来源四种都有——`legacy`（上线前的历史题，29 道，其中 24 道管理员核对过、一半写了讲解）、`consensus`（委员全票一致后自动入库，35 道）、`admin`（管理员结案的，5 道）、`precedent`（因为成了判例才进题库的，2 道：#69、#116）。#5 的标准答案被管理员改过（`answer_overridden`）；#30 被移出了题库。
- `GET /admin/jury/exams`：10 场考试记录。
- `GET /admin/jury/members`：21 条。`points`、`exam_score`、`abstain_rate`、`recuse_rate`、`quality_recused`、`badge_visible`、`days_served`；`status=expired` 能筛出任期届满的。
- `POST /admin/jury/members/{user_id}/review`：`adjust_points`、`set_tier`、带 `tier` 的 `approve`（创始任命，可以拿任何一个普通账号试）。

## 三期的走查

`GET /jury/config` 的 `capabilities` 追加了 `panel`、`appeal_review`、`machine_cases`、`reconsider`、`precedents`、`hall`；多了 `panel`、`appeals`、`reconsider`、`hall` 四块配置，`points` 多了 `panel_agreed`（15）、`panel_disagreed`（5）、`review_vote`（8）。资深委员的 `/jury/me.senior` 是 `{ panel, appeals }`，其他人是 null。

### 资深合议

合议队列里现在有 4 件（`GET /admin/cases?queue=panel`）。资深委员调 `GET /jury/panel/next`，按下面的顺序取到（先按优先级，再按进合议的先后）：

| 顺序 | 被举报的内容（开头） | 真实编号 | 情况 |
| --- | --- | --- | --- |
| 1 | 「说实话这游戏做得跟屎一样，建议作者先去玩玩别人的再来做」 | #128 | 大厅消息，三人举报。原审五票 3 比 2（`origin.reason: "no_consensus"`，`origin.tally` 是 不违规 2、轻微 2、一般 1），**还没人投合议票**。`origin.opinions` 里有三条原审委员的备注，每条带着他投的票。主账号 `juror` 在原审里投过票 |
| 2 | 「招募内测员：帮我测新游戏并写 50 字好评的同学，每人发 5 元红包…」 | #129 | 公开动态。原审 3 比 2。**已有一张合议票**（`senior2` 投的轻微违规，`panel.votes_cast: 1`）：再来一张违规票（哪一档都行）就定案；投「不违规」则 1 比 1，等第三票。`senior2` 自己取不到这一件 |
| 3 | 「某些人赢了也不光彩，怎么赢的自己清楚」 | #130 | 大厅消息。两张「上下文不足」转来的（`origin.reason: "need_context"`），`origin.opinions` 里有原审委员写的「不知道在说谁」 |
| 4 | 「滚滚滚，都给我滚，一群菜鸡也配评价我的作品」 | #71 | 大厅消息。进委员队列 72 小时只凑到一张票，超时转来的（`origin.reason: "timeout"`） |

三位资深委员能取到的件数：`senior1` 4 件、`senior2` 3 件（第 2 件他投过了）、`senior3` 4 件。

规则和看点：

- 只能投 `none` / `l1` / `l2` / `l3`（违规票可带 `category`）或 `recuse`；投 `abstain` / `need_context` 是 422 `invalid_vote`。**两票一致即定案**（「一致」看的是违规 / 不违规，等级取违规票里较低的那个），48 小时没定案转管理员（`panel.expires_at`）。
- 用 `senior1` 取第 1 件投一票，再取到的就是第 2 件：投一张违规票，当场定案，响应里 `settled: true`、`case.verdict.source` 是 `panel`、`case.tally.panel` 是 `{ none, l1, l2, l3 }`。之后他的风纪值明细里多一条 `panel_agreed`（+15）。
- 投完之后合议委员就是这个案件的参与者：`GET /jury/cases/{评审编号}`、观点区都能用（投票时写的备注就是他的第一条观点），`/jury/votes` 里这一条 `vote_stage` 是 `panel`。
- 合议中的案件，后投的合议委员在 `origin.opinions` 里**看不到**先投的合议委员写的备注，只有原审委员的。
- **回避**：这个库里只有三位资深委员，谁在合议里回避，这个案件就凑不齐三个席位了，会立刻转给管理员（`panel_outcome: "unavailable"`）。合议里的回避和原审共用每天 5 次的上限。
- **已经由合议定案的**：#126（「前十有什么了不起，全靠刷分的吧…」）。用 `juror` 在「我的评审」里找 `verdict_source: "panel"` 的那条：详情里 `verdict.source` 是 `panel`，`tally` 除了原审的五票还多一项 `panel: { none: 0, l1: 1, l2: 1, l3: 0 }`；观点区里两位合议委员都有发言（`tier: "senior"`），其中一位还回复了主账号的备注。它也在公示栏里（`source: "panel"`）。
- **合议超时转管理员的**：#127，在后台管理员队列里，`GET /admin/cases/127` 的 `votes[]` 里五张 `stage: "jury"`、一张 `stage: "panel"`。
- **凑不齐合议庭、直接转管理员的**：#79、#121（原审里有资深委员投过票），`panel_outcome` 是 `unavailable`、`panel_at` 是 null。主账号评审台第 4 件、第 14 件投完也是这个去向。
- 管理员可以直接接手合议中的案件：`POST /admin/cases/{id}/resolve`，之后 `panel_outcome` 是 `manual`。
- `juror`（正式委员）调 `/jury/panel/next`、`/jury/appeals/next` 都是 403 `not_senior`。

### 申诉复审

**被处罚人一侧**（`GET /moderation/me`，每条处罚多了 `appeal_stage` / `appeal_result` / `appeal_decided_by`）：

`appealing`（禁言中，`muted_until` 约 42 小时后）：

| 处罚 | 来源 | 申诉在哪 |
| --- | --- | --- |
| 「你是不是有病啊，同一个问题问八百遍…」一般，禁言 72 小时 | 委员定案（#136） | **资深复审中**（`appeal_stage: "senior"`），26 小时前申诉，还没人投 |
| 「免费代刷作品浏览量…」一般，禁言 24 小时（已过期） | 机器（`source: "auto"`） | **资深复审中**，5 小时前申诉，已有一位资深委员投了「维持」 |
| 「再不回消息，我就天天去你作品下面刷差评」一般，警告 | 私聊里的举报，管理员定案（`source: "report"`） | **直接到管理员**（`appeal_stage: "admin"`）：私聊相关的不进资深复审 |
| 「卧槽这什么垃圾匹配机制…」轻微，提示 | 机器 | 进过资深复审，72 小时没人投完，**超时转管理员**（`appeal_stage: "admin"`） |

`punished`（没有被禁言，记分 7）：

| 处罚 | 结果 | 字段 |
| --- | --- | --- |
| 「出自制的刷金币脚本…」垃圾广告，警告 | **维持** | `appeal_status: "rejected"`、`appeal_result: "upheld"`、`appeal_decided_by: "senior"` |
| 「你这种人也配当组长？…」辱骂骚扰，原来禁言 24 小时 | **减轻**：记录和记分保留，禁言解除 | `appeal_status: "accepted"`、`appeal_result: "reduced"`，`penalty_kind` 变成了 `warning` |
| 「笑死，这配色是色盲选的吧…」辱骂骚扰，原来禁言 72 小时 | **撤销**：处罚和记分一起撤销，案件 #133 改判为不违规 | `status: "revoked"`、`appeal_result: "revoked"` |
| 「签到打卡签到打卡…」刷屏，提示 | 还没申诉（`can_appeal: true`） | 用它现场提交一次：`POST /moderation/violations/{id}/appeal`，返回里 `appeal_stage` 是 `senior` |

另外两个账号：`offender` 的那条处罚现场申诉会直接到管理员（原审里有一位资深委员，凑不齐三位）；`appellant` 的申诉是开关打开之前提交的，一直在管理员手里。

**资深委员一侧**：`GET /jury/appeals/next` 按到期的先后取件（申诉复审和举报人复议走同一个接口）。`senior1` 依次取到：

1. `appealing` 那条委员定案的处罚：`type: "appeal"`、`source: "jury"`、`penalty: { kind: "mute", hours: 72 }`，带匿名上下文（`messages`）、原审的票数分布和两条观点（`origin`）、申诉理由。
2. `reporter2` 的复议：`type: "reconsider"`，见下一小节。
3. `appealing` 那条机器处罚：`source: "machine"`，没有案件快照，只有 `content: { text, image_count }`，`origin` 是 null；`votes_cast` 已经是 1。

`senior2` 取到的顺序相同；`senior3` 投过第 3 件，只有前两件。

`POST /jury/appeals/{review_id}/vote`，申诉投 `uphold` / `reduce` / `revoke`，两票相同即出结果，每张票当场 +8（`reward` 里的 `review_vote`）。想看「减轻」的实际效果：让两位资深委员把第 1 件都投 `reduce`，`appealing` 的禁言会当场解除（`/moderation/me.muted_until` 变成 null），那条处罚的 `penalty_kind` 变成 `warning`，他的通知中心多一条「你的申诉有结果了」。别人先投出结果之后，手里还拿着这件的第三位再投是 409 `review_closed`。

`GET /jury/appeals/mine`：`senior1` 已有 3 条（两件申诉、一件复议，都出了结果）。

后台：`GET /admin/jury/reviews` 能看到每件复审的申诉 / 复议理由和每位委员的票（实名）；`POST /admin/violations/{id}/appeal` 随时可以覆盖复审的结果（进行中的、已出结果的都行；复审已经撤销的处罚不能再驳回，409 `appeal_already_revoked`）。

### 机器来源的案件

- **评审台上有 3 件**（#139–#141），排在所有举报案件之后，所以要把前面的都评完（或回避掉）才轮得到：「这种垃圾作品也能上推荐？评审是瞎了吗」（大厅）、「有偿带过关，一关两块，童叟无欺」（大厅）、「实名吐槽隔壁班的王某：借了我的素材不还…」（公开动态）。它们的 `case_origin` 是 `machine`，`report_count` 是 0，`reasons` 只有一项，例如 `{ "reason": "机器筛查 · 辱骂骚扰", "count": 1 }`；`suggested_category` 是机器给的类别。主账号每天 30 件的名额初始已经用了 8 件，中间多用几次回避可以省一点。
- **已定案、进了公示栏的**：#137（「加我领取免费皮肤兑换码…」，公开动态，三位委员一致认定轻微违规）。
- **以「未作处理」结案的**：#138（「懂的都懂，那个谁最近又在搞小动作了」）。两张「上下文不足」，凑不齐合议庭；没有举报人在等结果，所以不转管理员，直接结案。主账号投过其中一张：在他的「我的评审」里是 `verdict_source: "auto"`、`outcome: "clean"`、`agreed: null` 的一条，详情里 `my_vote.points` 是 0，`public_case_id` 是 null（不进公示栏）。

### 重复举报与举报人复议

- **重复举报被拦**：「周末联机」群里 `m066` 说的那句「你昨天那把打得也太坑了吧，下次别选射手了」，一天半前被委员认定不违规（#146）。用群成员 `newbie` 再举报这条消息，返回 409 `already_reviewed`，`detail.case_id` 是 146。管理员认定不违规的内容同样会被拦。
- **可以申请复议**：同一条举报是 `reporter1` 提的，在他的 `/reports/me` 里 `can_reconsider: true`。`POST /reports/{id}/reconsider`，理由不足 10 个字是 422 `text_required`；申请之后 `reconsider_status` 变成 `pending`，再申请是 409 `reconsider_unavailable`。
- **复议进行中**：`reporter2` 的「别人做的都比你好，你还是别做了，浪费大家时间」（#145）。资深委员从 `/jury/appeals/next` 取到它：`type: "reconsider"`，带上下文、原审票数和观点、`verdict: { outcome: "clean", source: "jury", closed_at }`、`reconsider_text`、`requested_at`；类别、等级、处罚都是 null。投 `uphold` 或 `overturn`（可带 `level` 1–3 和 `category`），两票相同出结果。
- **已维持**：`reporter3`（#143，一条说比赛有黑幕的公开动态），`reconsider_status: "upheld"`。
- **已改判**：`reporter4`（#142，「想要这个游戏破解存档的私聊我…」），`reconsider_status: "overturned"`，这条举报的 `status` 变成了 `reviewed`；案件在公示栏里标着已改判。
- **转给管理员待处理**：`reporter1` 的另一条（#144）。原审里有一位资深委员，申请时凑不齐三位，直接到管理员；举报人看到的一直是 `pending`。后台用 `POST /admin/jury/reviews/{review_id}/decide`（`uphold` / `overturn`）处理。

### 判例

`GET /jury/precedents`（所有登录用户）有 8 条，类别有垃圾广告、辱骂骚扰、诈骗、违法违规，还有两条是「认定不违规」的：

| 判例 | 案件 | 谁标的 | 普通用户看详情 |
| --- | --- | --- | --- |
| 索要账号密码，不管用什么名义，一律按诈骗处理 | #42，公开动态 | 管理员 | **看得到被处理的内容**（`content`） |
| 刷粉刷赞的广告：留了联系方式就按一般处理 | #48，公开动态 | `senior1` | **看得到内容** |
| 在公开群里招揽付费答疑群，按轻微广告处理 | #72，大厅消息 | `senior1` | `content_hidden_reason: "group_message"` |
| 别人正常提问，上来就骂人：辱骂骚扰（一般） | #67，小群消息 | `senior2` | 同上 |
| 拿年龄嘲讽新手，属于辱骂骚扰 | #116，大厅消息 | `senior1` | 同上 |
| 免费分享破解资源同样违规 | #69，大厅消息 | `senior1` | 同上 |
| 批评得很直接，但说的是做法不是人：不算辱骂 | #55，大厅消息 | 管理员 | `clean` |
| 朋友之间的玩笑话，对方没有不适，不算辱骂 | #109，大厅消息 | 管理员 | `clean` |

- 普通用户（`eligible`）看详情：`case` 是 null，只有标题、要旨和按公示栏规则能看的 `content`。在任委员（`juror`）看同一条：`case` 里是匿名化的完整上下文。
- `senior1` 看列表：他标的四条 `mine: true`，可以 `PUT` 改、`DELETE` 撤。别人标的改不了（403 `not_precedent_author`）。
- **标判例**：`senior1` 的「我的评审」里已定案、没被改判、还不是判例的案件，详情里 `can_mark_precedent: true`（很多，随便找一条最近定案的）。`POST /jury/precedents`，`case_id` 填的是**评审编号**，返回的 `Precedent.case_id` 是公开编号。已经是判例的案件详情里是 `precedent: { id, title, mine }`。`juror` 调这个接口是 403 `not_senior`。
- 公示栏里对应的案件带 `precedent_id`（#72 定案不足 72 小时，还不在公示栏里）。
- **已撤下的**：「求购盗版资源和传播盗版同样处理」（#117，`senior1` 标的，三天前被管理员撤下并写了说明），只在后台 `GET /admin/jury/precedents` 里看得到；对外读是 404。
- 后台：`POST /admin/jury/precedents`（`case_id` 是公开编号，任何已定的案件都可以；隐私类如 #45、涉及私聊的不行，422 `case_not_eligible`）、`PUT`、`DELETE`（可带 `note`）。案件被改判时判例会自动撤下。
- `content_hidden_reason: "unpublished"`（公开动态认定违规、但案件此刻不在公示栏里）库里没有现成的例子。

### 议事厅

`GET /jury/hall/posts`（所有登录用户可读）有 13 个帖子：

| 帖子 | 类型 / 状态 | 作者 | 看点 |
| --- | --- | --- | --- |
| 议事厅开放说明（发帖前先看这里） | 公告，**置顶** | 管理员 | 正文超过 200 字：列表里 `content_truncated: true`，详情里是全文；`author_tier` 是 null |
| 三期功能上线：资深合议、申诉复审、判例 | 公告 | 管理员 | — |
| 建议把「刷屏」的默认等级从轻微调到一般 | 提案，**表决中** | `senior2` | **主账号没投过**。现在 3 赞成 1 反对，法定人数（`vote.quorum`）是 5：**再有一张赞成票**，人数（5）和比例（4/5 ≥ 2/3）就都够了，两天后到期时会是「通过」。再来的是反对票则人数够了、比例不够（3/5） |
| 建议质控题答错之后，给出同类判例的链接 | 提案，表决中 | `regular2` | **主账号投了赞成**（`my_vote: "yes"`）。2 比 2，离法定人数还差一人；结束前可以改票 |
| 建议「上下文不足」转合议时，把委员写的备注一起带上 | 提案，**已通过**（`passed`） | `senior1` | 6 比 1，等管理员决定是否采纳 |
| 建议在投票页直接显示举报理由对应的类别说明 | 提案，**已采纳**（`adopted`） | `senior3` | 带管理员说明（`admin_note`） |
| 建议取消每天 30 件的评审上限 | 提案，**未被采纳**（`declined`） | `senior2` | 6 比 2 通过了，但管理员没有采纳，带说明 |
| 建议见习委员的票只算半票 | 提案，**未通过**（`rejected`） | `regular2` | 2 比 6 |
| 「开玩笑」和「辱骂」的界线，大家平时怎么判断？ | 讨论 | `senior1` | **评论区最全**，见下 |
| 质控题答错了，觉得标准答案有问题，可以反馈吗？ | 讨论 | `juror` | 主账号自己的帖子：`mine` / `can_edit` / `can_delete` 都是 true；4 条评论 |
| 合议的 48 小时期限是不是太短了？ | 讨论，**已关闭**（`closed`） | `senior2` | 管理员关闭并写了说明；`can_comment: false` |
| （标题是占位文字「该帖已被折叠，等待管理员处理」） | 讨论，**被折叠**（`state: "hidden"`） | `m012` | 被三名在任委员举报后自动折叠，`content` 是 null；后台看得到原文 |
| 「拿不准」和「上下文不足」到底该怎么选？ | 讨论 | `m011` | 20 小时前发的，2 条评论 |

「开玩笑和辱骂的界线」那个帖子的评论（`GET /jury/hall/posts/{id}/comments`，平铺、时间正序，12 条）：

- 根评论 6 条，回复 6 条，回复的 `parent_id` 都指向根评论（只有一层）；
- 三条是**回复别人的回复**，被压到同一条根评论下面，带 `reply_to`（被回复的人）；
- 一条**已删除的占位**（`state: "deleted"`、`content: null`）：作者删了，但下面还有一条回复。另外两条被删的（一条没有回复的根评论、一条回复）不出现；
- 一条**被折叠的**（`state: "hidden"`）：在讨论未结案件、还猜当事人是谁，被三名在任委员举报；
- 一条被**普通用户** `eligible` 举报过的题外话：不是委员的举报不计入折叠，所以它还是 `visible`，后台的举报列表里有；
- 评论的人里有见习委员（`trainee1`、`trainee2`、`hesitant`、`cooldown`）：见习只能评论和表决，发帖是 403 `tier_required`。

现场可以试的：

- `juror` / `senior1` 发提案或讨论（每人最近 24 小时最多 3 帖，第 4 帖是 429 `hall_rate_limited`，`detail.resets_at`；删掉的也占额度）；提案有人表决之后不能再改（409 `post_locked`）。
- 表决：`POST /jury/hall/posts/{id}/vote`，见习委员也能投；再投一次不同的就是改票。已经截止的、不是提案的都是 409 `vote_closed`。
- 评论、回复、回复的回复；在已关闭 / 被折叠的帖子下评论是 409 `post_closed`。
- 举报帖子或评论（`POST /jury/hall/reports`，理由在 `config.hall.report_reasons`）：第三名在任委员举报之后返回的 `state` 变成 `hidden`。公告不会被自动折叠。
- `eligible` 等普通用户：只读，`can_comment` 和 `vote.can_vote` 都是 false；发帖、评论、表决是 403 `not_juror`，但可以举报。
- `mutedjuror`：403 `juror_restricted`。

### 后台（三期）

- `GET /admin/cases?queue=panel`：合议队列，4 件。`CaseRead` 新增 `case_origin`、`panel_at`、`panel_outcome`、`reconsider_status`；`open_counts` 多一项 `panel`。
- `GET /admin/jury/reviews?kind=&status=`：10 件复审（申诉 6、复议 4），每件带申诉 / 复议理由和每位委员的票。还开着的 5 件里，两件已经转给管理员（`stage: "admin"`）：一件申诉复审超时（`escalation: "timeout"`），一件复议凑不齐人（`escalation: "unavailable"`）。
- `POST /admin/jury/reviews/{review_id}/decide`：给复议出结果（用在申诉复审上是 409 `appeal_review`，那个要用 `POST /admin/violations/{id}/appeal`）。
- `GET /admin/violations?appeal_status=pending`：5 条（`appellant` 一条、`appealing` 四条）。
- `GET /admin/jury/precedents`：9 条，含那条已撤下的；带创建人、`in_bank` / `bank_added`。
- `GET /admin/jury/hall/posts`、`GET /admin/jury/hall/posts/{id}`：含已删除的，被折叠的也是原文；详情带全部评论的原文。
- `POST /admin/jury/hall/posts`：发公告；`POST /admin/jury/hall/posts/{id}/decision`：`adopt` / `decline`（那个 `passed` 的提案可以拿来试）、`close`、`delete`、`pin` / `unpin`。
- `GET /admin/jury/hall/reports`：9 条举报，其中 7 条待处理（被折叠的评论 3 条、被折叠的帖子 3 条、普通用户举报的 1 条），1 条已处理（删除了一条广告评论）、1 条已驳回。`POST /admin/jury/hall/reports/{id}/handle`：`delete` / `dismiss`（驳回后被折叠的内容恢复显示，同一目标上其余待处理的举报一起了结）。
- `POST /admin/jury/hall/comments/{id}/moderate`：不经过举报直接删除 / 恢复评论。
- `GET /admin/jury/settings`：`switches` 里 `appeals`、`machine_review` 是 true；`thresholds` 里有三期的全部阈值。

## 自测

```bash
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo_check.py                                  # 服务要先起着
E:/kukechat-venv/Scripts/python.exe scripts/jury_demo_check.py --base http://127.0.0.1:8793 --verbose
```

走真实的登录和证明头，把契约第 1、2、3 节的每个接口过一遍，字段和契约逐项比对：

- 一、二期：登录；配置；主账号连续取案投票直到几个有特别用途的案件都遇到（当场定案、无共识、上下文不足转出、严重违规等票、选类别、回避、质控题），并一直取到机器来源的案件；两种编号；观点区；我的评审；公示栏；各账号的状态和预警；晋升；风纪值、成就、排行榜、公开身份；考试；练习场；后台的题库、考试记录、成员、写操作；每日首票和连续 7 天。
- 第二轮修复：回避的记录 `stage` 为 null、带进展 / 结论筛选时不返回；质控题和考试题投票前内容的时间落在近期范围内，投完之后是真实时间。
- 三期：配置里的三期块和六个新 capability；`/jury/me.senior`；合议的取件、投票、定案、回避和各种错误码，以及定案后的详情、观点区、`vote_stage`；申诉进不进复审、三种结果对处罚的实际影响、`/jury/appeals/mine`；机器来源案件的字段；`already_reviewed`；复议的申请和两种结果；判例的全部读写接口和四种身份的权限；议事厅的全部读写接口（改票、一层回复、举报与折叠、频率限制）；后台的合议队列、复审监控、`decide`、覆盖复审结果、判例管理、公告、帖子 / 评论 / 举报的处置、设置里的阈值。
- 另外用 `senior1` 连 `/ws` 收实时事件（含 `jury.hall.changed`）。每个带上下文的视图都会核对消息时间随顺序递增。

结果分 PASS / FAIL，客户端要留意的地方记成 NOTE 汇总在最后。评审台那一段取几件、其中几道质控题是随机的，所以每次的 PASS 数会差几十。

**它会改数据**（投票、考试、练习、任命、改判、申诉、复审、标判例、发帖……）。跑完之后停掉服务，`--reset` 重建。要在不打扰别人的前提下跑，用 `--db-dir` 另起一套。

## 已知的局限

- **数据会变旧。** 所有时间都是按生成时刻倒推的，所以：
  - `dayfirst`、`streak6`「今天还没投票」只在生成数据的当天（UTC+8）成立；
  - 合议队列里的 4 件分别在 28–44 小时后到期（48 小时），到期会转给管理员；资深复审里的 3 件在 46–67 小时后到期（72 小时）；
  - 主账号评审台上的案件是 2–21 小时前进队列的，从生成算起大约 50 小时后会陆续超时（72 小时）转出，评审台就空了；
  - 表决中的两个提案分别在 2 天、5 天后到期出结果；`reporter1` 那条可以申请复议的举报 5 天半后过期；
  - `appealing` 的禁言约 42 小时后到期，`cooldown` 的弃权冷却约 23 小时后结束，`examfail` 的考试冷却约 19 小时后结束；
  - 周榜按自然周算，过了周一零点就只剩当周的得分；连续天数一天不投就断；
  - 「定案不足 72 小时」的案件，过了时间会进公示栏；满足条件的还会被定时任务自动收进题库；
  - 取到手没投的案件 24 小时后让给别人，而且不会再分给同一个人；
  - `mutedjuror` 的禁言约 13 天后到期，各位委员的任期 9–27 天后到期（没有违规记录的会自动续任）。

  需要干净的初始状态就 `--reset`。
- **只有三位资深委员。** 合议和复审都要三个席位，所以任何一位回避、被禁言或者在原审里投过票，对应的案件就进不了（或者留不住）合议 / 复审。这本身就是要走查的行为，但也意味着「有人回避、另一位顶上」这种情形在这个库里看不到。
- **总览和排行榜各有 60 秒缓存。** 投完票马上刷，全站数字可能还是旧的（`me` 不缓存）。
- **每天 30 件的上限是真的**，按最近 24 小时滚动计算，主账号初始已经用了 8 件，到了是 429 `jury_daily_limit`。投满了就 `--reset`，或者换一个委员账号。回避每天 5 次、练习每天 20 道、议事厅每天 3 帖也一样。合议和复审不受这个上限限制。
- **内容审核是关的。** 新举报没有机器预审，直接分队列；发消息、发动态、议事厅发言都不过审（所以 422 `content_rejected` 看不到）；新内容也不会变成机器来源的案件。
- **没有 Redis，只有一个进程。** 实时事件只在这个进程里发；`jury.queue.changed` 30 秒内最多发一次。
- **聊天内容是为案件造的。** 群里的历史是一段段不相干的对话，素材会重复（考试里可能遇到被举报内容相同、上下文不同的两道题），图片是带编号的占位色块，用户没有头像。大厅的消息按时间看是乱的：案件的上下文是后来补进去的。
- **CCW 绑定、邮件、对象存储都没接。** 相关功能在这个实例上不可用。余额来源是脚本里的假账本（见「红包」一节），不是真的。
- **`--reset` 之后数据基本一样**（真实编号、用户 id 都不变）。随机的只有：质控题什么时候出、出哪一道，考试抽到哪十道题，`dayfirst` / `streak6` 以前答过的是哪几道质控题。

## 灌数据的代码怎么组织

`scripts/jury_demo.py` 里灌数据的代码按期分函数：`seed_base`（账号、群、委员任命）、`seed_phase1`（案件、投票、观点、公示、改判、处罚、最早的一批题）、`seed_phase2`（二期上线后的案件、题库、考试、练习、风纪值调整、公开身份）、`seed_phase3`（开关、追加的委员、合议、申诉复审、机器案件、复议、判例、议事厅、两位今天没投票的委员）、`seed_red_packets`（打开红包开关、三个红包）、`seed_gifts`（打开礼物开关、四条送礼记录、一个今天生日的账号、一件快到期的限时头像框）。再加数据就写一个新函数追加到 `PHASES` 列表里。已经灌过的期不会重跑，所以在现有的库上直接启动只会补新的一期。

灌数据的约定：

- 有业务逻辑的数据（举报、分队列、投票、定案、合议、观点、改判、处罚、申诉、复审、复议、判例、议事厅的帖子 / 表决 / 评论 / 举报、考试、练习、任命、调分、开关）一律调服务层函数，这样代号、计数、一致率、风纪值、层级、成就、公示标记都是真的；服务层把时间写成「现在」，`Seeder` 里的工具随后把时间戳改到过去。超时这类「到点才发生」的事（委员队列超时、合议超时、复审超时、提案到期）是先把时间改到过去，再调定时任务里的同一个函数。
- 没有照搬服务层的地方：每日首票按这一票应该发生的那天记、连续天数最后按时间顺序统一重放（`_seed_daily_rewards`、`Seeder.rebuild_streaks`），因为灌数据不是按时间先后一票一票投的；各位委员的在任天数留到最后才改成真实值，免得灌历史数据的过程中被一路自动晋升（`regular2` 例外，他就是这样被系统晋升的）；机器处罚和机器来源的案件没有真的调审核服务，审查记录是直接写的，建案和记处罚仍然走服务层。
- 一个案件里的上下文消息时间必须随顺序递增：`Seeder.message_case` 发现会接上时间更晚的旧消息时，会把自己的上下文补满 10 条。灌完数据会整库核对一遍。
- 三期的案件原审都由追加的那 6 位委员投票，不动一、二期里几位有特别用途的委员；议事厅的提案在「今天没投票」的两位委员任命之前发，法定人数因此是按 16 位在任委员定下的（5 人）。
