# 红包

用户拿自己的金币余额在会话里发红包，会话里的人来抢。币只在用户之间流动：一个红包发出去多少，最后「被领走的 + 退回发送者的」就是多少，总量不变。

路径前缀 `/api/v1`，都要求登录。错误的 `detail` 是 `{ code, message }`，`message` 是可以直接展示的中文。路径都**不带结尾的斜杠**（`/red-packets`、`/admin/red-packets`；带了也接，不会 307）。

## 1. 余额

```
余额 = CCW 核实的累计投币 − 商店花掉的 − 送礼花掉的 − 发出的红包总额 + 抢到的 + 退回的
```

（「送礼花掉的」见 [`gifts.md`](gifts.md)，`balance` 对象里是 `gift_spent`。）

- 发红包时立刻扣**整包**金额；没被领完的部分在红包结束（到期、被撤回、被撤销）时退回。
- `balance` 对象就是商店的那个（`docs/shop.md` 3.1），多了三项：`red_packet_sent`（发出的整包金额之和）、`red_packet_received`（抢到的）、`red_packet_refunded`（退回来的）。`balance.balance` 已经按上面的公式算好。
- 抢红包不要求绑定 CCW：币记在账上。**花**（商店购买、发红包）才要求绑定，而且要能向余额来源核实——沿用商店现有的规则，核实不了是 503 `verification_unavailable`，不扣币。
- 同一个 CCW 账号的币不能靠换绑再发一遍：`balance.spent` 里包含这个 CCW 账号在别的 KukeChat 账号上用红包发出去（没退回）的部分，和商店里「在别的账号上已经花掉的」是同一条规则。

## 2. 开关与限制

存在数据库里，后台可以随时改（`PUT /admin/red-packets/settings`），另一个服务进程最多 5 秒后生效。

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | **false** | 总开关。**关掉只禁止发新红包**；已经发出去的照常可以看、可以领、照常过期退回。要止损用后台撤销 |
| `max_total` | 500 | 单个红包的总额上限 |
| `max_count` | 100 | 单个红包最多几份 |
| `min_each` | 1 | 每一份至少多少 |
| `daily_send_limit` | 20 | 每人每天最多发几个，按 UTC+8 的自然日。被撤回、被撤销的也算一次 |
| `expire_hours` | 24 | 多久过期。过期时间在发的时候就定死，改这一项只影响之后发的红包 |

## 3. 用户侧接口

### 3.1 `GET /red-packets/config`

```jsonc
{ "enabled": true,
  "limits": { "max_total": 500, "max_count": 100, "min_each": 1, "daily_send_limit": 20, "expire_hours": 24 },
  "sent_today": 3,           // 我今天已经发了几个
  "features": ["chain", "exclusive"] }   // 服务端支持的玩法，见 3.6
```

开关关着也能调（`enabled: false`）。

`features` 列出服务端支持的玩法：`chain`（接龙红包）、`exclusive`（专属红包）。客户端看到对应的项才显示发红包界面上的新选项；旧服务端没有这个字段，当成空列表。

### 3.2 `POST /red-packets`

```jsonc
{ "conversation_id": 5, "kind": "lucky", "amount": 100, "count": 5, "greeting": "新年快乐",
  "chain_text": "谢谢老板",          // 可选：接龙口令，见 3.6.1
  "target_user_id": 42 }            // 可选：专属红包的目标，见 3.6.2
```

- `kind`：`normal` 每份一样多，`amount` 是**单个**金额，总额 = `amount × count`；`lucky` 拼手气，`amount` 是**总额**。金额、个数都是正整数。
- 私聊只允许 `normal` 且 `count = 1`。
- `greeting` 可空（默认「恭喜发财」），最长 30 字；首尾空格去掉，换行和连续空白折成一个空格，零宽字符删掉。会过文本审核（默认祝福语不送审；审核服务不可用时放行，消息发出去之后还会走一次异步审核）。
- 拼手气的分法：二倍均值法，每份不少于 `min_each`，加起来严格等于总额。
- `chain_text`、`target_user_id` 都不传（或 `null`）就是原来的红包。规则见 3.6。

成功返回 **201**：

```jsonc
{ "red_packet": { /* 红包对象，见 3.3 */ },
  "message": { /* MessageRead：会话里落下的那条消息 */ },
  "balance": { /* balance 对象，已经扣掉整包 */ } }
```

会话里落的消息：

```jsonc
{ "type": "red_packet",
  "content": "[红包] 新年快乐（更新到新版才能领取）",       // 给不认识这个类型的旧客户端、搜索和通知预览用
  "metadata": { "red_packet": { "id": 12, "kind": "lucky", "greeting": "新年快乐", "count": 5 } } }
```

接龙 / 专属红包的 `metadata.red_packet` 多几项（**只在是的时候才有**，普通红包的 metadata 和以前完全一样）：

```jsonc
{ "id": 13, "kind": "normal", "greeting": "生日快乐", "count": 1,
  "chain_text": "谢谢姐",                 // 接龙红包才有
  "target_user_id": 42,                   // 专属红包才有
  "target": { /* UserPublic，发的时候的快照 */ } }   // 专属红包才有
```

`content`（旧客户端、会话列表预览、通知用的兜底文字）按玩法区分：

| 玩法 | `content` |
| --- | --- |
| 普通 | `[红包] 新年快乐（更新到新版才能领取）` |
| 接龙 | `[接龙红包] 新年快乐（更新到新版才能领取）` |
| 专属 | `[专属红包] 给 小波：生日快乐（更新到新版才能领取）`（名字取群昵称 > 昵称 > 用户名，发的时候定下） |
| 专属 + 接龙 | `[专属接龙红包] 给 小波：生日快乐（更新到新版才能领取）` |

- `metadata.red_packet` 里只有不会变的字段。进度、状态、我领没领、金额都要调 `GET /red-packets/{id}`，或者听 `red_packet.updated`。
- 消息走正常的发消息路径和广播（其它成员收到 `message.created`，计入未读、会话预览、群活跃度，也受发言限频约束）。
- 不能通过普通的发消息接口发 `type = "red_packet"`（422）。红包消息不能被编辑（服务端没有编辑消息的接口）。
- **红包消息不能转发**：合并转发（`POST /conversations/{id}/forward-bundle`）选中的消息里只要有一条红包消息，整个请求就是 422 `red_packet_not_forwardable`；别的类型的消息在 `metadata` 里带 `red_packet` 也是这个错误（防止把红包消息原样抄一份发出去）。引用回复红包消息不受影响。
- **消息被撤回 = 红包提前结束**，没领的退回发送者，状态变成 `revoked`；已经被领走的不收回。发送者自己撤回、群主 / 群管理员撤回、平台处置（举报、机器审核）撤回都一样。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 403 | `disabled` | 红包功能没开 |
| 403 | `not_member` | 不在这个会话里（或者会话不存在） |
| 403 | `muted` | 被平台禁言（带 `muted_until`），或者在这个群里被禁言 / 群开了全员禁言（群主、管理员不受群内禁言限制） |
| 422 | `invalid_kind` | `kind` 不是 `normal` / `lucky`，或者私聊里 / 专属红包发了 `lucky` |
| 422 | `invalid_count` | 个数不是 1 到 `max_count` 的整数，或者私聊里 / 专属红包不是 1。带 `max_count` |
| 422 | `invalid_amount` | 金额不是正整数、总额超过 `max_total`、或者分不出每份至少 `min_each`。带 `max_total`、`min_each` |
| 422 | `greeting_rejected` | 祝福语超过 30 字，或者没通过文本审核（`message` 说明是哪一种） |
| 422 | `chain_text_rejected` | 接龙口令不合法：不是字符串、有换行、超过 30 字，或者没通过文本审核（`message` 说明是哪一种）。审核在扣币之前做，被拒不扣币 |
| 422 | `invalid_target` | 专属红包的目标不合法。带 `reason`：`direct`（私聊里不能发专属红包）、`self`（指定了自己）、`not_member`（不是这个群的成员，或者用户不存在）、`bot`（机器人）、`invalid`（不是正整数） |
| 403 | `ccw_not_bound` | 没绑定 CCW 账号 |
| 429 | `daily_limit` | 今天发的个数到上限了。带 `daily_send_limit` |
| 503 | `verification_unavailable` | 现在核实不了余额（和商店购买一致），稍后再试。没有扣币 |
| 402 | `insufficient_balance` | 余额不够。带 `balance`（数字，当前余额）、`total`（这个红包要多少）、`shortfall`、`donate_url` |
| 429 | `rate_limited` | 撞上了发消息的限频（每人每分钟 20 条，或者群自己设的发言限频）。没有扣币 |

检查顺序大体就是表里从上到下的顺序（`invalid_target` 的 `direct` 在 `invalid_kind` 之前判断；口令格式、目标在 `ccw_not_bound` 之前，口令审核在它之后、`daily_limit` 之前）。除了成功，任何一种结果都不扣币、不落消息。

### 3.3 `GET /red-packets/{id}`

```jsonc
{
  "id": 12,
  "kind": "lucky",
  "greeting": "新年快乐",
  "sender": { /* UserPublic */ },
  "chain_text": null,           // 接龙口令；不是接龙红包为 null
  "target_user_id": null,       // 专属红包给谁；不是专属红包为 null。会话成员谁都能看
  "target": null,               // 同上，UserPublic
  "conversation_id": 5,
  "message_id": 901,
  "total": 100,                 // 见下面的可见性规则
  "count": 5,
  "claimed_count": 2,           // 已经被领走几份，谁都能看
  "claimed_amount": 37,         // 见下面的可见性规则
  "refunded_amount": 0,         // 结束时退回发送者的部分（进行中、抢完的是 0）；见下面的可见性规则
  "status": "active",           // active 进行中 / finished 抢完了 / expired 到期 / revoked 提前结束（消息被撤回，或者后台撤销）
  "my_claim": { "amount": 21, "created_at": "…" },   // 我领到的；没领是 null
  "expires_at": "…",
  "created_at": "…",
  "finished_at": null,          // 抢完、到期或被提前结束的时间
  "claims": [                   // 见下面的可见性规则
    { "user": { /* UserPublic */ }, "amount": 21, "created_at": "…", "is_luckiest": false }
  ]
}
```

- 只有会话成员能看（403 `not_member`）；不存在是 404 `not_found`。
- `id` 是整数。
- **可见性**：`total`、`claimed_amount`、`refunded_amount`、`claims` 只在「我已经领了」「我是发的人」或者「红包已经不是 `active`」时给出，否则都是 `null`——没抢的人在红包还进行着的时候看不到任何金额。`claims` 是完整的列表（最多 `max_count` 项），按领取顺序排。
- `is_luckiest`（手气最佳）只在 `lucky` 而且**抢完了**（`finished`）的红包里标一个：金额最大的那一份，一样大时算先抢到的。
- 过了 `expires_at` 就是 `expired`，不依赖后台的定时清扫（读这个接口时会顺手把它结束掉并退回）。

### 3.4 `POST /red-packets/{id}/claim`

没有请求体。成功返回：

```jsonc
{ "amount": 21, "red_packet": { /* 红包对象，这时一定带金额和 claims */ }, "balance": { /* balance 对象 */ },
  "message": null }   // 接龙红包：领取时以我的身份自动发出的那条消息（MessageRead），见 3.6.1；其它红包为 null
```

- 群里发的人自己也能抢；私聊里发的人不能领自己的。
- 没绑定 CCW 的人也能领。
- 不看总开关（见第 2 节）。
- 被平台禁言的人不能领；群内禁言不影响领——**接龙红包例外**：领了就要发言，群内被禁言（或群开了全员禁言）的人领不了，返回 `muted_in_group`（群主、管理员不受群内禁言限制）。
- 专属红包只有目标本人能领，其他人（包括发的人）是 `not_target`。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 404 | `not_found` | 红包不存在 |
| 403 | `not_member` | 不在这个会话里 |
| 403 | `muted` | 被平台禁言。带 `muted_until` |
| 403 | `not_target` | 这是给别人的专属红包 |
| 403 | `own_packet` | 私聊里领自己发的红包 |
| 403 | `muted_in_group` | 接龙红包：我在这个群里被禁言了，或者群开了全员禁言，领了发不了言所以不让领。什么都没发生 |
| 409 | `already_claimed` | 已经领过了（哪怕红包后来抢完、过期了，也是这个） |
| 410 | `finished` | 被抢完了 |
| 410 | `expired` | 过期了 |
| 410 | `revoked` | 被提前结束了（消息被撤回，或者后台撤销） |

检查顺序：`not_found` → `not_member` → `muted` → `not_target` → `own_packet` → `muted_in_group` → `already_claimed` / 410。

### 3.4.1 `GET /red-packets/pending?conversation_id=…`

这个会话里我还能领的红包，聊天右上角「有 N 个红包没领」的提示用，点了跳到那条消息。口径和第 6 节的 `red_packet_pending` 一样。

```jsonc
{ "items": [ { "id": 12, "message_id": 345, "sender_id": 7, "kind": "lucky", "greeting": "恭喜发财", "count": 10, "claimed_count": 3,
               "expires_at": "…", "created_at": "…",
               "chain_text": null, "target_user_id": null, "target": null } ] }   // 新的在前，最多 20 个；不带金额
```

- 专属红包只出现在目标本人的列表里（发的人、其他成员都看不到它）。

- 不是会话成员：403 `not_member`。
- 客户端维护：进会话时拉一次；收到 `type = "red_packet"` 的 `message.created`、`red_packet.updated`（`status` 不是 `active` 或 `claimed_count` 变了）、自己 claim 之后重拉或本地增删即可。过期按 `expires_at` 自己去掉。

### 3.5 `GET /red-packets/mine?direction=sent|received&limit=20&offset=0`

```jsonc
// direction=sent：每项直接是红包对象
{ "items": [ { "id": 12, "total": 100, "claimed_count": 2, "claimed_amount": 37, "refunded_amount": 0, "status": "active", /* … */ } ], "total": 12, "sum": 340 }
// direction=received：每项是我抢到的一笔
{ "items": [ { "amount": 21, "created_at": "…", "red_packet": { /* 红包对象 */ } } ], "total": 30, "sum": 512 }
```

- `direction` 不传默认 `received`；`limit` 1–100（默认 20）。`total` 是这个方向一共多少条，`sum` 是这个方向的合计金额。
- `sent`：我发出的，按发出时间倒序；`sum` 是发出的整包金额之和（不扣退回的；每项的 `refunded_amount` 是那一个退回了多少）。
- `received`：我抢到的，按领取时间倒序；`amount` / `created_at` 是我领到多少、什么时候领的；`sum` 是抢到的金额之和。
- 这两个列表里的红包对象**不带领取明细**（`claims` 恒为 `null`），要看明细调 3.3。其余字段一样（对我来说都是「发的人」或「已经领了」，所以金额字段都有）。

### 3.6 接龙红包与专属红包

两种玩法可以单独用，也可以组合（专属 + 接龙）。接龙可以和 `normal`、`lucky` 组合；专属只能是 `normal`、1 份。

#### 3.6.1 接龙红包（`chain_text`）

- 发的时候带 `chain_text`：去掉首尾空白、删掉零宽字符之后 1–30 字，**不能有换行**（`\n`、`\r`、U+2028 等，否则 `chain_text_rejected`）。去掉空白之后是空串，等于没传（不是接龙红包）。
- 发之前和祝福语走同一套文字审核，被拒是 422 `chain_text_rejected`，**不扣币**。审核服务不可用时放行；红包消息发出去之后的异步审核会把祝福语和接龙口令一起审（算在发红包的人头上）。
- 私聊、群聊都能发。
- **领取成功时，后端自动以领取人的身份在这个会话里发一条普通文本消息**，内容就是 `chain_text`：

  ```jsonc
  { "type": "text", "sender_id": /* 领取人 */, "content": "谢谢老板",
    "metadata": { "red_packet_chain": { "red_packet_id": 12 } } }   // 客户端据此显示「接龙」标记
  ```

  - 这条消息和领取**在同一个事务里提交**：要么钱领到了、消息也发出去了，要么都没发生（接口报错）。不会出现领到了钱但消息没发、或者消息发了但没领到。
  - 它走正常的发消息流程：其它在线成员收到 `message.created`、计入未读和会话列表的最后一条、群活跃度照算；领取人自己的已读位置推到这条消息。领取接口的返回里 `message` 就是它；领取人自己的在线连接也会收到这条 `message.created`（和自己发普通消息一样），按消息 id 和返回值去重。
  - 发的人自己在群里抢自己的接龙红包，也一样会以自己的身份发这句话。
  - 这条消息**不受发言限频约束**（每人每分钟 20 条、群的慢速模式 / 每分钟 N 条都不算它），也不算 @、不抓作品链接预览。领取次数本身有上限（一个红包一人一次）。
  - 它不单独送异步审核（口令是发红包的人写的，已经审过）。
- 能不能领要先过发言检查：被平台禁言是 `muted`，群内被禁言 / 群开了全员禁言是 `muted_in_group`（群主、管理员不受群内禁言限制）。这一步在抢之前做；万一在这之后、提交之前刚好被禁言，事务里发消息那一步会再拒一次，同样返回 `muted_in_group`，什么都不领。
- 普通的发消息接口里带 `metadata.red_packet_chain` 不会报错，但这一项会被去掉（比如把接龙消息原样转发）：「接龙」标记只能由领取产生。

#### 3.6.2 专属红包（`target_user_id`）

- **只能在群里发**（私聊里传是 422 `invalid_target`，`reason: "direct"`）。
- `target_user_id` 必须是这个群的成员、不是自己、不是机器人（否则 422 `invalid_target`，`reason` 见 3.2 的错误表）。
- `kind` 只能是 `normal`、`count` 只能是 1（否则 `invalid_kind` / `invalid_count`），`amount` 就是那一份的金额。
- 只有目标本人能领；其他人（包括发的人）领是 403 `not_target`。
- 会话成员 `GET /red-packets/{id}` 都能看到 `target_user_id` / `target`（这是给谁的）。金额可见性规则不变：目标没领之前，其他人看不到金额。
- 第 6 节的 `red_packet_pending` 和 `GET /red-packets/pending`：专属红包**只对目标本人**算「可领」，发的人和其他成员都不算。客户端在本地置「[有红包]」时也要照这个判断：收到 `message.created` 的红包消息，`metadata.red_packet.target_user_id` 有值且不是我，就不置。
- 目标在红包结束前退群了：其他人还是领不了，到期退回发送者。

## 4. 实时事件

| 事件 | data | 说明 |
| --- | --- | --- |
| `red_packet.updated` | `{ id, conversation_id, message_id, claimed_count, count, status }` | 发给会话的在线成员：有人领、抢完、过期、被提前结束时。**不带任何金额**。有人领的事件是合并推送的（同一个红包 0.3 秒内只推一次最新状态），结束时立刻推。过期的事件由定时清扫发，最多比 `expires_at` 晚一分钟——客户端自己按 `expires_at` 把红包显示成已过期即可 |
| `shop.balance.changed` | `{ reason: "red_packet", change, balance, red_packet_id }` | 只发给本人：发了红包（`change: "red_packet.sent"`）、领到了（`"red_packet.claimed"`）、有退回（`"red_packet.refunded"`）。`balance` 是完整的 balance 对象，可以直接用，也可以收到就重拉。见 `docs/shop.md` 第 4 节 |

发红包本身不发 `red_packet.updated`，其它成员靠 `message.created` 知道（消息的 `type` 是 `red_packet`）。

接龙红包被领时，除了 `red_packet.updated`，会话里还会多一条领取人发的 `message.created`（`type = "text"`，`metadata.red_packet_chain.red_packet_id` 指向红包），见 3.6.1。没有新的事件类型。

## 5. 后台

都在 `/admin/red-packets` 下，要求管理员身份。

**隐私边界（硬性）**：后台不能借红包看私聊内容。私聊红包的 `greeting`、`chain_text` 在后台接口里一律是 `null`；后台接口不返回任何消息内容（没有 `content`、`metadata` 之类的字段），撤销也不碰会话里的那条消息。

| 接口 | 说明 |
| --- | --- |
| `GET /admin/red-packets/settings` | `{ enabled, max_total, max_count, min_each, daily_send_limit, expire_hours, bounds }`。`bounds` 是每一项允许的范围 `{ 名字: { min, max } }`（只读） |
| `PUT /admin/red-packets/settings` | body 是上面那几项（不含 `bounds`），可以整份提交，也可以只传要改的；返回完整设置。不合法是 422 `invalid_settings`，`detail.field` 指出哪一项。有变化才写审计日志 |
| `GET /admin/red-packets` | 列表，新的在前。筛选：`status`、`kind`、`sender_id`、`conversation_id`、`created_from` / `created_to`（ISO 时间，按发出时间，`created_from <= created_at < created_to`）；分页 `limit`（1–200，默认 50）、`offset`。返回 `{ items, total, summary }` |
| `GET /admin/red-packets/{id}` | 详情：列表里的那一项，加 `claims: [{ user, user_id, amount, created_at, is_luckiest }]` |
| `POST /admin/red-packets/{id}/revoke` | body `{ reason }`（必填，最长 500）。提前结束，没领的退回发送者，写审计日志；返回详情对象。已经结束的是 409 `not_active`；`reason` 为空是 422 |

列表里的每一项：

```jsonc
{ "id": 12, "kind": "lucky",
  "greeting": "新年快乐",                  // 私聊红包恒为 null
  "sender": { "id": 42, "username": "…", "nickname": "…", "avatar_url": "…" }, "sender_id": 42,
  "conversation_id": 5, "conversation_type": "group",   // direct / group
  "message_id": 901,
  "chain": false,                          // 是不是接龙红包（私聊的也标，只是不给口令）
  "chain_text": null,                      // 接龙口令；私聊红包恒为 null
  "target_user_id": null, "target": null,  // 专属红包的目标（target 和 sender 一样是 { id, username, nickname, avatar_url }）
  "total": 100, "count": 5, "claimed_count": 2, "claimed_amount": 37, "refunded_amount": 0,
  "status": "active", "created_at": "…", "expires_at": "…", "finished_at": null,
  "revoked_by_id": null, "revoke_reason": null }        // 后台撤销的才有
```

- `summary` 是 `{ total_sent, total_claimed, total_refunded, outstanding }`，**全站累计，不随筛选变**。`outstanding` 是还在进行中的红包里没被领走的；四个数满足 `total_sent = total_claimed + total_refunded + outstanding`。
- `total` 是筛选结果的条数。
- `status` 筛选按库里的状态：刚过期、定时清扫还没轮到的红包（最多一分钟）筛 `active` 还能筛到，但那一项展示的 `status` 已经是 `expired`。
- 范围：`max_total` 1–1,000,000，`max_count` 1–500，`min_each` 1–1,000,000（不能大于 `max_total`），`daily_send_limit` 1–1000，`expire_hours` 1–168。

审计日志（`admin_audit_logs`）的动作名：

| `action` | `target_type` / `target_id` | `details` |
| --- | --- | --- |
| `red_packet.settings.update` | `system_setting` / 空 | `{ 改了的项: { old, new } }` |
| `red_packet.revoke` | `red_packet` / 红包 id | `{ reason, refunded }`，`refunded` 是退回了多少 |

### 5.1 对账与账户列表

`POST /admin/shop/reconcile`（`docs/shop.md` 第 5 节）按新的余额公式改了：抢到的币也能花，所以「花的比投的多」不再必然是异常。现在查两件事：

- `mismatched`：按完整公式算出来**余额为负**的账户。
- `red_packets`：全站红包账的守恒检查

  ```jsonc
  { "total_sent": 1200, "total_claimed": 900, "total_refunded": 180, "outstanding": 120,
    "balanced": true, "problems": [] }
  ```

  四个数各从各的地方数（发出、已退来自红包表，已领、未结来自每一份的领取记录），`balanced` 为 true 表示 `total_sent = total_claimed + total_refunded + outstanding`，而且没有哪个红包自己对不上。`problems` 列出对不上的红包（最多 50 个，带各项计数），正常情况下是空的。

`GET /admin/shop/accounts` 的每一项多了 `red_packet_sent` / `red_packet_received` / `red_packet_refunded`，`balance` 按新公式算，`mismatch` 的口径是「余额为负」。抢到过红包的人也会出现在这个列表里（哪怕没绑定、从没核实过余额）。

## 6. 会话列表上的「[有红包]」

会话对象（`GET /conversations` 的每一项，以及所有返回单个会话的接口）新增：

```jsonc
"red_packet_pending": true     // 这个会话里有我还能领的红包
```

- 「我还能领」= 红包是 `active`、没过期（现场按 `expires_at` 判断，不依赖清扫）、我没领过、不是「私聊里我自己发的」、而且不是「给别人的专属红包」。群里我自己发的、我还没抢的算（专属红包除外）。
- 接龙红包在我被群禁言时仍然算「可领」（去领会得到 `muted_in_group`）——判断禁言要多查，这里不做。
- 没有新的实时事件，客户端自己维护：
  - 置上：收到 `message.created` 且 `type = "red_packet"`（私聊里自己发的除外）。
  - 清掉：自己 `claim` 成功（或者返回 409 / 410），或者收到这个红包的 `red_packet.updated` 且 `status` 不是 `active`。一个会话里可能同时有好几个红包，本地记不清的时候重拉会话列表为准。
  - 过期没有即时事件（见第 4 节），按 `expires_at` 自己清，或者下次拉列表时就是对的。
- 会话列表本身有 5 秒的缓存；自己 `claim` 之后会立刻作废自己的那份。
- 旧客户端不认识这个字段，不受影响。
- 成本：整个会话列表只多一条查询（专属红包的条件只看红包行自己的 `target_user_id`，不多查任何东西），从「全站进行中的红包」出发（走 `(status, expires_at)` 索引），每个红包做两次唯一索引上的点查（我在不在那个会话里、我领没领过），和我有多少会话、会话里有多少消息无关。全站没有进行中的红包时（功能没开过，或者都结束了）这条查询不执行：「全站有没有进行中的红包」每个进程每 3 秒最多问一次库，答案是没有的这几秒里完全不碰红包的表。
- 消息被机器审核 / 案件结案撤回、清扫还没来得及结束红包的那一小段时间（最多一分钟）里，这个字段可能还是 true（去领会得到 410 `revoked`）。

## 7. 实现上的细节

- **新列**：`red_packets.chain_text`（接龙口令）、`red_packets.target_user_id`（专属红包的目标），都是可空列，启动时自动 `ALTER TABLE ... ADD COLUMN`（`app/core/database.py`），老红包都是空，不用回填。
- **表**：`red_packets`（红包）和 `red_packet_shares`（每一份）。发的时候就把每一份的金额分好存成行，领取是把其中一份记到某个人名下。没有另外的余额列：余额公式里红包的三项直接对这两张表求和。两张都是新表，启动时自动创建，不改已有的表。两个进程同时启动、都去建同一张新表时，后到的那个会重试一遍而不是启动失败；稳妥起见上线前仍然先单进程启动一次把表建好。
- **并发**（线上两个进程）：每个写事务的第一条语句都是带条件的单条 UPDATE，事务从这条语句起拿着数据库的写锁直到提交。
  - 发：余额够才往下写（和商店购买是同一个余额表达式、同一种扣法，所以同时发两个、或者一边发一边买，都不会双花）；扣款、红包、每一份、会话里的消息在一个事务里，任何一步不成全部回滚。
  - 抢：一条 UPDATE 抢占「还没人领的第一份」，条件里带着「红包还在进行中、没过期、消息没被撤回」；`(红包, 用户)` 唯一约束保证一人一份。
  - 结束：一条 UPDATE 把状态从 `active` 改走，同时把没领的那些份之和记成退回。到期清扫、领取时的现场判断、撤回消息、后台撤销都走它，谁先到谁做，其余的什么都不发生。
- **过期**：每个进程每 `RED_PACKET_SWEEP_INTERVAL_SECONDS`（默认 60）秒清扫一次，多个进程同时清扫是幂等的。领取和读取时也会现场判断，不依赖清扫。清扫不看总开关。
- **撤回**：用户撤回、后台按消息 id 撤回是当场结束红包；机器审核、案件结案撤回的由清扫在一分钟内结束——但不管哪种，消息一撤回就领不了了。案件改判把消息恢复之后，红包不会恢复（没领的已经退回了）。
- **消息被清理 / 群被解散**：每个会话只保留最近 1000 条消息，红包消息被挤掉之后红包本身还在（到期照常退回）。群解散后红包由清扫提前结束并退回。
- **红包消息的机器审核**只看祝福语和接龙口令。接龙红包领取时自动发出的文本消息不单独审。
