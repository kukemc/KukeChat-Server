# 礼物

用户花金币给别人送礼物，或者送一件商店商品。送礼是**真消耗**：对方不得币，只涨「心意值」（字段名 `charm`）。

路径前缀 `/api/v1`，都要求登录。错误的 `detail` 是 `{ code, message }`，`message` 是可以直接展示的中文。金额都是正整数金币，「今天」「本月」一律按 UTC+8。

## 1. 余额与心意值

```
余额 = CCW 核实的累计投币 − 商店花掉的 − 送礼花掉的 − 发出的红包总额 + 抢到的 + 退回的
```

- `balance` 对象就是商店的那个（`docs/shop.md` 3.1），多了一项 `gift_spent`：我送礼物、送商品一共花掉的币。`balance.balance` 已经按上面的公式算好。
- 送礼和商店购买一样要求绑定 CCW，而且要能向余额来源核实——核实不了是 503 `verification_unavailable`，不扣币。
- 同一个 CCW 账号的币不能靠换绑再送一遍：每条送礼记录上记着送的时候绑定的 CCW 账号；`balance.spent` 里包含这个 CCW 账号在**别的** KukeChat 账号上送礼花掉的部分，和商店里「在别的账号上已经花掉的」、红包里「在别的账号上发出去的」是同一条规则。解绑的那个账号自己送出去的仍记在它自己的 `gift_spent` 里（余额可以是负数，什么也花不了）。
- 心意值：收礼人的 `charm` += 花掉的币数（礼物 = 单价 × 数量，商品 = 当时的价格）。只增不减。`UserPublic` 新增 `charm: number`，直接读用户行上的一列。
- 送礼不会被撤销：撤回礼物消息只是把消息撤掉，礼物、币、心意值都不退；商品已经到对方名下。

## 2. 开关与限制

存在数据库里，后台可以随时改（`PUT /admin/gifts/settings`），另一个服务进程最多 5 秒后生效。

| 项 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `enabled` | **false** | | 总开关。**关掉只禁止送新的**（`POST /gifts` 返回 403 `disabled`）；目录、礼物墙、记录、心意榜这些只读接口照常返回数据，`UserPublic.charm` 也照常给。客户端在 `enabled = false` 时自己把这些入口藏起来 |
| `daily_send_limit` | 200 | 1–10000 | 每人每天最多送几**次**（UTC+8 的自然日）。礼物和商品合在一起算；一次送 99 个也是一次 |
| `message_max` | 60 | 1–200 | 留言最多多少个字 |

一次最多送 99 个（`count_max`，固定）。

## 3. 用户侧接口

### 3.1 `GET /gifts/catalog`

```jsonc
{ "enabled": true,
  "charm_label": "心意值",        // 收到礼物涨的那个数值对外叫什么；客户端界面上一律用它
  "gifts": [ { "key": "cake", "name": "蛋糕", "price": 20, "icon": "cake", "tier": 2, "is_active": true, "sort": 30 } ],   // 只有上架中的，按 sort
  "limits": { "message_max": 60, "count_max": 99, "daily_send_limit": 200 },
  "sent_today": 3 }               // 我今天已经送了几次
```

- `icon` 是客户端内置矢量图形的名字。首批 6 件（服务端首次启动时播种，只在 `key` 不存在时插入，后台改过的不动）：`flower` 小花 1 币 tier 1 / `lollipop` 棒棒糖 5 / 1 / `cake` 蛋糕 20 / 2 / `trophy` 奖杯 50 / 2 / `rocket` 火箭 100 / 3 / `castle` 城堡 300 / 3。另外内置但暂不上架、供后台以后新建礼物选用：`star` / `heart` / `balloon` / `crown` / `cat` / `icecream`。客户端遇到不认识的 `icon` 用通用礼盒图标。
- `tier` 决定动效规模：1 = 消息卡片内的小动画；2 = 聊天区内的一段动画；3 = 全屏动画（尊重「减少动态效果」和「性能优先」）。

### 3.2 `POST /gifts`

```jsonc
{
  "to_user_id": 42,
  "conversation_id": 7,        // 在哪个会话里送；null 表示从对方个人空间 / 资料卡送（不落聊天消息）
  "kind": "gift",              // gift 送目录里的礼物 / item 送一件商店商品
  "gift_key": "cake",          // kind = gift
  "count": 3,                  // kind = gift：1–99；kind = item 固定 1（可以不传）
  "item_sku": "frame.angel",   // kind = item
  "message": "生日快乐！"       // 留言，可空，≤ message_max 个字
}
```

成功返回 200（带不带结尾的斜杠都接）：

```jsonc
{ "record": { /* GiftRecord，见 3.3；其中 to_user.charm 是送完之后的新值 */ },
  "message": { /* MessageRead：会话里落下的那条消息；conversation_id 为 null 时是 null */ },
  "balance": { /* balance 对象，已经扣掉这一次 */ } }
```

- 不能送给自己，也不能送给机器人账号。`conversation_id` 非空时**双方**都必须是该会话的成员（私聊里对方就是收礼人）；为 null 时不要求双方有任何关系。
- 留言：首尾空格去掉，换行和连续空白折成一个空格，零宽字符删掉；空的当成没写。会过文本审核（审核服务不可用时放行；落了消息的话，消息发出去之后还会走一次异步审核）。
- **送商品**：按商品现价扣币，对方直接获得拥有权（不自动装备），心意值 = 价格。分三种情况：
  - 对方没有（或者限时的已经到期）：得到一份；限时商品从现在起算 N 天。
  - 对方已有这件**限时**商品、还没到期：续期，在原到期时间上加 N 天（见 `docs/shop.md` 2.5）。对方拥有的是限时的、而商品现在是永久的：换成永久。
  - 对方已经**永久**拥有：409 `recipient_owns`，不扣币。
  - 价格为 0（只能等级解锁 / 赠送）、已下架、过了限时销售期（`available_until`）、不存在的商品：422 `not_giftable`。
  - 收礼人的购买记录（`GET /shop/purchases`）里这一条 `source = "gift"`、`price` 为 0、带 `from_user`。不计入收礼人的已花，也不算商品的「卖出」数量。
- **会话里落的消息**（`conversation_id` 非空时）：

  ```jsonc
  { "type": "gift",
    "content": "[礼物] 送给 阿夸 蛋糕 ×3",          // 给不认识这个类型的旧客户端、搜索和通知预览用；送商品是「[礼物] 送给 阿夸 「天使」」
    "metadata": { "gift": {
      "id": 12,                                    // 送礼记录的 id
      "kind": "gift",
      "gift": { "key": "cake", "name": "蛋糕", "icon": "cake", "price": 20, "tier": 2 },                   // kind = item 时为 null
      "item": null,                                // kind = item：{ sku, name, category, style, duration_days }
      "count": 3,
      "to_user": { "id": 42, "display_name": "阿夸", "avatar_url": "…" },
      "message": "生日快乐！"                       // 没写留言是 ""
    } } }
  ```

  - 消息走正常的发消息路径和广播（其它成员收到 `message.created`，计入未读、会话预览、群活跃度，也受发言限频、群内禁言约束）。发消息那一步被拒，整个送礼回滚：不扣币、不写记录、不涨心意值、对方也不会得到商品。
  - `metadata.gift` 里的礼物 / 商品是**送的那一刻**的样子，之后改名、改价不影响。
  - 不能通过普通的发消息接口发 `type = "gift"`（422）。礼物消息不能被编辑（服务端没有编辑消息的接口）、不能复读。
  - **礼物消息不能转发**：合并转发（`POST /conversations/{id}/forward-bundle`）选中的消息里只要有一条礼物消息，整个请求就是 422 `gift_not_forwardable`；别的类型的消息在 `metadata` 里带 `gift` 也是这个错误。引用回复礼物消息不受影响。
  - 礼物消息的机器审核只看留言（没写留言的不送审）。
- 从个人空间 / 资料卡送（`conversation_id` 为 null）不落消息，不受发言限频约束，只受每日次数限制。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 403 | `disabled` | 礼物功能没开 |
| 422 | `invalid_target` | 收礼人是自己、不存在、是机器人，或者 `to_user_id` 不是正整数 |
| 403 | `not_member` | 我或者对方不在这个会话里（或者会话不存在） |
| 403 | `muted` | 被平台禁言（带 `muted_until`；从个人空间送也不行），或者在这个群里被禁言 / 群开了全员禁言（群主、管理员不受群内禁言限制） |
| 403 | `blocked` | 对方把我拉黑了（现有的拉黑关系：`temporary_conversation_blocks`，就是临时会话里点的「拉黑」） |
| 403 | `ccw_not_bound` | 没绑定 CCW 账号 |
| 422 | `invalid_gift` | `kind` 不是 `gift` / `item`，或者没有这个礼物、礼物已下架 |
| 422 | `invalid_count` | 数量不是 1–99 的整数；送商品不是 1。带 `count_max` |
| 422 | `not_giftable` | 这件商品不能送（不存在、价格为 0、已下架、过了限时销售期） |
| 422 | `message_rejected` | 留言超长（带 `message_max`）、格式不对，或者没通过文本审核 |
| 409 | `recipient_owns` | 对方已经永久拥有这件商品 |
| 429 | `daily_limit` | 今天送的次数到上限了。带 `daily_send_limit` |
| 503 | `verification_unavailable` | 现在核实不了余额（和商店购买一致），稍后再试。没有扣币 |
| 402 | `insufficient_balance` | 余额不够。带 `balance`（数字，当前余额）、`total`（这一次要多少）、`shortfall`、`donate_url` |
| 429 | `rate_limited` | 撞上了发消息的限频（每人每分钟 20 条，或者群自己设的发言限频）。没有扣币 |

检查顺序就是表里从上到下的顺序（留言的文本审核在 `recipient_owns` 之后、`daily_limit` 之前）。除了成功，任何一种结果都不扣币、不写记录、不落消息。

### 3.3 `GiftRecord`

```jsonc
{ "id": 12, "kind": "gift",
  "gift": { "key": "cake", "name": "蛋糕", "icon": "cake", "price": 20, "tier": 2 },   // kind = item 时为 null
  "item": null,                       // kind = item：{ sku, name, category, style, duration_days }
  "count": 3,
  "amount": 60,                       // 送礼人花掉的币，也是收礼人涨的心意值
  "from_user": { /* UserPublic */ }, "to_user": { /* UserPublic */ },
  "conversation_id": 7, "message_id": 901,      // 从个人空间送的两项都是 null
  "message": "生日快乐！",              // 见下
  "on_birthday": true,                // 送的那一刻是不是收礼人的生日（UTC+8）；之后不会变
  "created_at": "…" }
```

**留言的可见性**：`message` 只有送的人和收的人能看到，其他任何人拿到的记录里都是 `null`——不管这份礼物是在群里、私聊里还是从个人空间送的，也不管看的人是不是同一个群的成员、群主或平台管理员。所有返回 `GiftRecord` 的接口都是这个规则。唯一能让第三方看到留言的地方是**群聊里那条礼物消息本身**（`metadata.gift.message`）：那张卡片群成员本来就都看得到。

### 3.4 礼物墙与记录

- `GET /users/{id}/gifts` → `{ charm, total_count, summary: [{ kind, gift | null, item | null, count, last_at }] }`。这个人收到的礼物按种类（礼物按 `key`、商品按 `sku`）聚合，公开，不含留言。`summary` 按单价从高到低排，`count` 是这一种一共收到多少个，每一种展示的是最近一次收到时它的样子；`total_count` 是各种的 `count` 之和。用户不存在是 404 `user_not_found`。
- `GET /users/{id}/gifts/records?gift_key=&item_sku=&limit=&offset=` → `{ items: [GiftRecord], total }`。公开：谁送的、送了什么、什么时候，新的在前；留言按 3.3 的规则隐藏。`limit` 1–100（默认 20）。
- `GET /gifts/mine?direction=sent|received&limit&offset` → `{ items: [GiftRecord], total, sum }`。`direction` 不传默认 `received`；`sum` 是这个方向的 `amount` 之和（送出 = 花掉的币，收到 = 涨的心意值）。

### 3.5 心意榜

`GET /gifts/leaderboard?period=all|month&limit=` → `{ items: [{ rank, user: UserPublic, charm }], me: { rank | null, charm } }`

- `all` 总榜按 `users.charm`；`month` 本月榜按这个自然月（UTC+8，每月 1 日零点切）收到的记录求和。一样多时用户 id 小的在前，名次不并列。`limit` 1–100（默认 50）。
- `me` 是我自己的名次和这个榜上的心意值，不受 `limit` 影响；没上榜（这个榜上是 0）时 `rank` 为 null。
- 结果在每个服务进程里缓存 60 秒（`GIFT_LEADERBOARD_CACHE_SECONDS`）：本进程里有人送了礼会立刻作废，别的进程里送的最多晚 60 秒上榜。

## 4. 实时事件

| 事件 | data | 说明 |
| --- | --- | --- |
| `gift.received` | `{ record }` | **只发给收礼人**，无论有没有落消息。`record` 是 `GiftRecord`（带留言，`to_user.charm` 是新值）。别人送我商店商品时**只发这一条**，不再另发 `shop.item.granted` |
| `shop.balance.changed` | `{ reason: "gift", change: "gift.sent", balance, gift_id }` | 只发给送礼人本人（别的设备跟着变）。见 `docs/shop.md` 第 4 节 |

- 在会话里送的，其它成员靠 `message.created` 知道（消息的 `type` 是 `gift`）。
- 第三方看到的心意值没有实时事件，下次拉资料时更新（没有「用户资料更新」这种通用事件）。收礼人自己的新值在 `gift.received` 里，送礼人看到的在接口返回的 `record.to_user.charm` 里。

## 5. 后台

都在 `/admin/gifts` 下，要求管理员身份。

**隐私边界（硬性）**：后台不能借礼物看私聊内容。只有在**群聊**里送的礼物才给留言（那张卡片群成员本来就都看得到）；私聊里送的、从个人空间 / 资料卡送的，留言只有双方看得到，在后台接口里一律是 `null`。后台接口不返回任何聊天消息内容（没有 `content`、`metadata` 之类的字段）。会话后来被删掉了也一样：记录上记着送的时候它是群聊还是私聊。

| 接口 | 说明 |
| --- | --- |
| `GET /admin/gifts/settings` | `{ enabled, daily_send_limit, message_max }` |
| `PUT /admin/gifts/settings` | body 同上，可以整份提交，也可以只传要改的；返回完整设置。不合法是 422 `invalid_settings`，`detail.field` 指出哪一项。有变化才写审计日志 |
| `GET /admin/gifts/catalog` | `{ gifts: [Gift] }`，全部礼物，含已下架的 |
| `POST /admin/gifts/catalog` | 新建：`key`、`name`、`price`、`icon`、`tier` 必填，`is_active`（默认 true）、`sort`（默认 0）可选。返回 `Gift`。`key` 重复是 409 `key_exists`（`detail.field = "key"`） |
| `PUT /admin/gifts/catalog/{key}` | 修改：只动传了的字段。`key` 不能改（body 里带了不一样的 `key` 是 422 `key_immutable`）；礼物只能下架（`is_active: false`），不能删。不存在是 404 `gift_not_found`。改名、改价不影响已经送出的记录 |
| `GET /admin/gifts/records` | 全站的送礼流水，新的在前。筛选 `kind`、`from_user_id`、`to_user_id`、`created_from` / `created_to`（ISO 时间，`created_from <= created_at < created_to`）；分页 `limit`（1–200，默认 50）、`offset`。返回 `{ items, total, summary }` |

- 取值范围：`key` 小写字母、数字加 `._-`，最长 32；`name` 最长 20；`price` 1 到商店的 `PRICE_MAX`（1,000,000）；`icon` 小写字母、数字加 `_-`，最长 32（服务端不校验它是不是客户端认识的图形）；`tier` 1 / 2 / 3；`sort` ±1,000,000。校验错误是 422 `invalid_gift`，`detail.field` 指出哪一项。
- 流水的每一项是 `GiftRecord`（`from_user` / `to_user` 是 `UserPublic`；`message` 按上面的隐私边界），另加 `conversation_type`（`group` / `direct` / 从个人空间送的是 null）和 `conversation_title`（群名；私聊、个人空间、群已经不在了都是 null）。
- `summary` 是 `{ total_amount, total_count }`，**全站累计，不随筛选变**：`total_amount` 是花掉的币，`total_count` 是送礼**次数**（记录条数）。`total` 是筛选结果的条数。

审计日志（`admin_audit_logs`）的动作名：

| `action` | `target_type` / `target_id` | `details` |
| --- | --- | --- |
| `gift.settings.update` | `system_setting` / 空 | `{ 改了的项: { old, new } }` |
| `gift.catalog.create` | `gift` / 礼物的内部 id | `{ key, name, price }` |
| `gift.catalog.update` | `gift` / 礼物的内部 id | `{ key, changed: { 改了的字段: { old, new } } }` |

### 5.1 对账与账户列表

- `GET /admin/shop/accounts` 的每一项多了 `gift_spent`，`balance` 按新公式算；`shared_spent` 里包含这个 CCW 账号在别的账号上送礼花掉的。
- `POST /admin/shop/reconcile` 的返回多了 `gifts`：送礼的账的守恒检查

  ```jsonc
  { "total_amount": 1200, "total_count": 85, "total_charm": 1200, "total_received": 1200, "balanced": true, "problems": [] }
  ```

  `total_charm` 是所有用户的心意值之和，`total_received` 是送礼记录里收礼人还在的那些的金额之和（`total_amount` 是全部记录的，收礼人账号被删了的也算）。`balanced` 为 true 表示 `total_charm = total_received`，而且没有哪个人自己对不上；`problems` 列出心意值和收到的记录对不上的用户（最多 50 个，`{ user_id, charm, received }`），正常情况下是空的。「余额为负」的检查（`mismatched`）已经把送礼算进去了。

## 6. 实现上的细节

- **表**：`gifts`（目录）和 `gift_records`（每次送礼一行，带当时的礼物 / 商品快照、留言、发送时的 CCW 账号）。两张都是新表，启动时自动创建；`users` 上加了 `charm` 一列和它的索引。没有另外的余额列：余额公式里「送礼花掉的」直接对 `gift_records` 求和。
- **并发**（线上两个进程）：和红包、商店购买一样，写事务的第一条语句是带条件的单条 UPDATE（余额够才往下写），事务从这条语句起拿着数据库的写锁直到提交。用的是和购买、发红包同一个余额表达式，所以送礼与送礼、送礼与购买、送礼与发红包之间都不会双花。扣款、记录、心意值、对方的拥有记录、会话里的消息在一个事务里，任何一步不成全部回滚。每日次数上限在拿到写锁之后再数一遍，同时送出来的几个请求不能一起越过上限。
- **心意值的守恒**：`users.charm` 和 `gift_records` 的那一行在同一个事务里写（`UPDATE users SET charm = charm + amount`），所以任何时刻每个人的 `charm` = 他收到的记录的 `amount` 之和；对账会核对。记录不会被删、不会被撤销，所以心意值只增不减。
- **消息被清理 / 群被解散**：每个会话只保留最近 1000 条消息，礼物消息被挤掉之后记录还在（`message_id` 指向的消息不在了）。记录上的 id 都不建外键。
- **机器审核 / 举报撤回了礼物消息**：只是消息没了。记录里的留言当事人仍然看得到。
