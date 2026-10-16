# 群升级（Group Plus）、群文件、永久精华、群待办、聊天记录搜索

路径前缀 `/api/v1`，都要求登录（`/admin/group-plus/*` 要求管理员）。错误的 `detail` 是 `{ code, message }`，`message` 是可以直接展示的中文。
金额都是正整数金币。时间一律 ISO（UTC）。「今天」按 UTC+8，只在搜索的日期筛选里用到。

本文件是客户端 / 后台 / 安卓三端并行开发的依据。实现后改契约会同步改这里。

## 1. 档位与规则

| 档位 | key | 名称 | 人数上限 | 云端消息条数 | 群文件 | 精华消息上限 |
| --- | --- | --- | --- | --- | --- | --- |
| 免费 | `free` | 免费 | 200 | 1000 | 未解锁（`file_enabled=false`） | 100 |
| Pro | `pro` | Pro | 500 | 5000 | 临时文件（24 小时，单个 10MB）；没有空间（`file_quota_bytes=0`） | 300 |
| Pro+ | `pro_plus` | Pro+ | 1000 | 20000 | 200MB（`209715200` 字节）；空间满后可传 30MB 内的 24 小时临时文件 | 800 |
| MAX | `max` | MAX | 3000 | 50000 | 1GB（`1073741824` 字节）；空间满后可传 100MB 内的临时文件 | 3000 |

- 档位排序（`rank`）：free 0 < pro 1 < pro_plus 2 < max 3。
- 价格（金币 / 月，后台可改，存在 system_settings）：Pro 默认 80，Pro+ 默认 200，MAX 默认 500。**年费 = 10 个月的价格**（12 个月时长，两个月免费）。月 = 30 天，年 = 365 天。价格范围 1–1,000,000。
- 群主和管理员都能买。需要绑定 CCW 账号，要能向余额来源核实（同礼物 / 商店）。
- 买法：
  - **没有档位或已过期**：从现在起算 N 天（`action = "new"`）。
  - **买同档位 = 续期**：在原到期时间上加（`action = "renew"`）。
  - **买更高的档位 = 立即升级**：新到期 = 现在 + 购买时长；**原档位剩余时间作废**（`action = "upgrade"`，quote 里带 `forfeited_seconds` / `forfeited_days`）。升级且有剩余时间时，购买接口必须带 `confirm_forfeit: true`，否则 409 `forfeit_not_confirmed`（带 quote，客户端弹确认框再提交）。
  - **买更低的档位**：当前档位到期前不允许，422 `downgrade_not_allowed`。过期之后随便买。
  - **永久档位**（只有后台能给，见 9.2）：不能再买任何档位（续期没意义，升级 / 降级都只会让它变成有期限的），报价和购买都是 409 `already_permanent`。后台取消（`tier = free`）之后恢复正常。
- 到期降回免费：
  - 已有成员不踢，但**满员后不能再加人**（人数 >= 当前上限时加人返回 409 `group_full`）。
  - 云端消息超出免费条数的，在后续发消息时被逐步修剪（每次发消息最多修剪 25 条，精华 / 群待办有独立快照，不受影响）。
  - **群文件有 30 天宽限**：宽限内可读、可下载、可删除，**不能上传**；续费 / 重新开通恢复；过了宽限，后台 worker 把这个群的全部文件（含永久）删掉。
  - **精华不删**：已有精华保留，只是用量达到当前上限后不能再添加。
- 群文件、精华、待办之外，群升级只影响：人数上限、消息保留条数、角标。
- 总开关 `enabled`（默认 true，后台可关）：关掉只禁止**购买**；已有档位照常生效。
- 用户的余额公式新增一项：

```
余额 = CCW 核实的累计投币 − 商店花掉的 − 送礼花掉的 − 群升级花掉的 − 发出的红包总额 + 抢到的 + 退回的 + 后台赠送的
```

  「后台赠送的」（`balance.granted`）见 [`shop.md`](shop.md) 5.2。

  `balance` 对象多一项 `group_plus_spent`（我买群升级一共花掉的币），`balance.balance` 已经按上面的公式算好。`balance.spent` 里包含这个 CCW 账号在**别的** KukeChat 账号上买群升级花掉的部分（和商店 / 红包 / 礼物同一条换绑规则）。群升级不退款；后台手动赠送不收费、不进余额公式。
- 每个群的当前档位是**有效档位**：记录的档位已过期就是 `free`。所有对外接口给的 `plus_tier` 都是有效档位，`plus_expires_at` 在免费时为 `null`。

## 2. 群对象上新增的字段

`ConversationRead`（会话列表、会话详情等所有返回会话的接口）、`GroupDiscoveryRead`（群搜索 / 推荐）、聊天记录搜索结果里的会话摘要新增：

```jsonc
"plus_tier": "max",                        // free / pro / pro_plus / max，有效档位
"plus_expires_at": "2026-12-01T00:00:00Z", // 免费时为 null
"plus_permanent": false                    // 永久档位（后台给的）
```

**永久档位**：`plus_permanent: true`，`plus_expires_at` 是哨兵值 `2999-12-31T00:00:00Z`（所以只看到期时间的旧客户端也不会把它当成过期）。客户端看到 `plus_permanent: true` 显示「永久」，不显示日期 / 剩余天数；没有这个字段的旧数据按「到期年份 >= 2999」判断。

客户端用它画群头像右下角的角标：MAX 是专属标志，Pro / Pro+ 可以画不同的角标，free 不画。私聊的 `plus_tier` 恒为 `free`。旧客户端不认识这两个字段，忽略即可。

`ConversationRead` 另外新增：

```jsonc
"file_default_ttl_hours": 24,   // 群文件默认保留时长，24 / 168 / 720
"group_todo": null              // 群待办，见第 6 节；没有为 null
```

`PATCH /conversations/{id}/settings`（群设置，仅群主 / 管理员）新增可选字段 `file_default_ttl_hours`：只能是 `24`（1 天）/ `168`（7 天）/ `720`（30 天），否则 422 `invalid_file_ttl`。只影响之后上传的文件。

## 3. 群升级接口

### 3.1 `GET /group-plus/plans`

```jsonc
{ "enabled": true,
  "year_months_paid": 10, "month_days": 30, "year_days": 365,
  "file_max_bytes": 104857600,            // 旧字段：各档位临时文件上限里最大的（MAX 的 100MB）。新客户端看 tiers[].file_temp_max_bytes
  "file_temp_ttl_hours": 24,              // 临时文件保留时长
  "file_ttl_options_hours": [24, 168, 720],
  "file_grace_days": 30,
  "tiers": [
    { "key": "free", "name": "免费", "rank": 0, "member_limit": 200, "message_limit": 1000,
      "file_enabled": false, "file_quota_bytes": null, "file_temp_max_bytes": null,
      "featured_limit": 100, "price_month": 0, "price_year": 0 },
    { "key": "pro", "name": "Pro", "rank": 1, "member_limit": 500, "message_limit": 5000,
      "file_enabled": true, "file_quota_bytes": 0, "file_temp_max_bytes": 10485760,   // 没有空间，只能传 10MB 内的临时文件
      "featured_limit": 300, "price_month": 80, "price_year": 800 },
    { "key": "pro_plus", "name": "Pro+", "rank": 2, "member_limit": 1000, "message_limit": 20000,
      "file_enabled": true, "file_quota_bytes": 209715200, "file_temp_max_bytes": 31457280,
      "featured_limit": 800, "price_month": 200, "price_year": 2000 },
    { "key": "max", "name": "MAX", "rank": 3, "member_limit": 3000, "message_limit": 50000,
      "file_enabled": true, "file_quota_bytes": 1073741824, "file_temp_max_bytes": 104857600,
      "featured_limit": 3000, "price_month": 500, "price_year": 5000 }
  ] }
```

### 3.2 `GET /conversations/{cid}/plus`（群成员）

```jsonc
{ "conversation_id": 7,
  "tier": "pro_plus", "tier_name": "Pro+", "expires_at": "…",     // 免费时 expires_at 为 null
  "permanent": false,                                               // 永久档位（见第 2 节）：不能再购买
  "limits": { "member_limit": 1000,                // 这个群现在的人数上限；null = 不限
              "member_limit_unlimited": false,     // 人数不限（后台给这个群单独设的，见 9.2）
              "member_limit_override": null,       // 后台单独设的值：null 跟随档位 / 0 不限 / 正数
              "message_limit": 20000, "file_enabled": true,
              "file_quota_bytes": 209715200, "file_temp_max_bytes": 31457280, "featured_limit": 800 },
  "usage": { "member_count": 312, "featured_count": 12,
             "file_bytes_used": 1048576,   // 只算空间文件，不含临时文件
             "file_count": 3 },             // 没过期的文件数，含临时文件
  "files": { "state": "active",            // locked 未解锁 / active 可用 / grace 宽限期（可读不可传）
             "tier": "pro_plus",           // active / grace 时：群文件空间按哪一档算；locked 为 null
             "grace_until": null,          // grace 时是宽限到期时间；其它为 null
             "quota_bytes": 209715200, "used_bytes": 1048576, "file_count": 3,
             "default_ttl_hours": 24,
             "free_bytes": 208666624, "temp_max_bytes": 31457280, "temp_ttl_hours": 24,
             "max_file_bytes": 208666624 },  // 字段含义见 5.1 的 FileQuota
  "can_purchase": true }                   // 我是群主 / 管理员
```

### 3.3 报价 `POST /conversations/{cid}/plus/quote`

```jsonc
// 请求
{ "tier": "max", "period": "month" }          // period: month / year
// 响应（不扣币，不改任何东西；检查规则和购买一样，但不检查余额是否足够 —— 通过 enough 告诉客户端）
{ "conversation_id": 7,
  "action": "upgrade",                         // new / renew / upgrade
  "tier": "max", "period": "month", "days": 30,
  "price": 2000,
  "current_tier": "pro", "current_expires_at": "…",   // 当前有效档位；免费时 current_expires_at 为 null
  "new_expires_at": "…",
  "forfeited_seconds": 1036800, "forfeited_days": 12, // 升级时原档位被作废的剩余时间；其它为 0
  "forfeit_notice": "升级后原来 Pro 剩余的 12 天将作废",   // 没有作废时为 null
  "balance": 2500, "enough": true }             // 我现在的余额（按库里的数，不向 CCW 核实）
```

### 3.4 购买 `POST /conversations/{cid}/plus/purchase`

```jsonc
{ "tier": "max", "period": "year",
  "confirm_forfeit": true,     // 升级且原档位有剩余时间时必须为 true（见第 1 节）
  "expected_price": 20000 }    // 可选：客户端展示给用户的价格；和服务端现在的价格不一致 → 409 price_changed，不扣币
```

成功 200：

```jsonc
{ "record": { /* GroupPlusRecord，见 3.6 */ },
  "conversation": { "conversation_id": 7, "plus_tier": "max", "plus_expires_at": "…" },
  "plus": { /* 同 3.2 */ },
  "balance": { /* shop 的 balance 对象，已扣款，含 group_plus_spent */ } }
```

扣款、写记录、改群档位在**一个事务**里；检查顺序就是下表从上到下。除成功外任何结果都不扣币、不改群。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 403 | `disabled` | 群升级暂停购买 |
| 404 | — | 会话不存在，或者我不是成员（`detail` 是字符串） |
| 400 | `not_group` | 私聊不能升级 |
| 403 | `not_group_staff` | 不是群主 / 管理员 |
| 422 | `invalid_tier` | tier 不是 pro / pro_plus / max |
| 422 | `invalid_period` | period 不是 month / year |
| 409 | `already_permanent` | 这个群是永久档位，不能再买（报价也是这个错误）。带 `current_tier` |
| 422 | `downgrade_not_allowed` | 当前档位还没到期，不能买更低的档位。带 `current_tier`、`current_expires_at` |
| 409 | `forfeit_not_confirmed` | 升级会作废剩余时间，需要 `confirm_forfeit: true`。带 quote 的全部字段 |
| 409 | `price_changed` | `expected_price` 不等于现价。带 `price` |
| 403 | `ccw_not_bound` | 没绑定 CCW 账号 |
| 503 | `verification_unavailable` | 现在核实不了余额，稍后再试。没有扣币 |
| 402 | `insufficient_balance` | 余额不够。带 `balance`、`total`、`shortfall`、`donate_url` |
| 409 | `plan_changed` | 并发的另一次购买刚改了这个群，档位关系变了（比如刚被升级），重新报价再提交。没有扣币 |

### 3.5 `GET /conversations/{cid}/plus/records?limit=&offset=`（群主 / 管理员）

群的购买 / 赠送流水，新的在前：`{ items: [GroupPlusRecord], total }`。`limit` 1–100（默认 20）。非群主 / 管理员 403 `not_group_staff`。

### 3.6 `GroupPlusRecord`

```jsonc
{ "id": 3, "conversation_id": 7, "source": "purchase",   // purchase 购买 / grant 后台赠送 / revoke 后台取消
  "action": "upgrade",                                   // new / renew / upgrade / grant / revoke
  "tier": "max", "period": "year",                       // grant / revoke 的 period 为 null
  "days": 365, "amount": 20000,                          // 花掉的币；赠送 / 取消为 0
  "prev_tier": "pro", "prev_expires_at": "…",
  "new_tier": "max", "new_expires_at": "…",
  "permanent": false,                                    // 这条记录给出的是永久档位（后台赠送）
  "forfeited_days": 12,
  "user": { /* UserPublic，购买人；后台赠送时为 null */ },
  "note": null, "created_at": "…" }
```

## 4. 实时事件

| 事件 | data | 发给 |
| --- | --- | --- |
| `group.plus.updated` | `{ conversation_id, plus_tier, plus_expires_at, plus_permanent, reason }`，`reason`：`purchase` / `grant` / `revoke` / `member_limit`（后台改了人数上限，档位没变；需要的话重拉 `GET /conversations/{cid}/plus`）。不认识的 `reason` 照样按里面的档位字段更新 | 群里的在线成员。到期降级**没有事件**，客户端按 `plus_expires_at` 自己判断 |
| `shop.balance.changed` | `{ reason: "group_plus", change: "group_plus.purchased", balance, conversation_id }` | 只发给购买人 |
| `group.todo.updated` | 见第 6 节 | 群里的在线成员 |
| `group.file.updated` | `{ conversation_id, action, file_id }`，`action`：`uploaded`（另带 `folder_id`） / `deleted` / `updated`（转永久 / 转限时） / `moved`（另带 `file_ids`、`folder_id`，见 5.5） | 群里的在线成员 |
| `group.file.folders.changed` | `{ conversation_id, action, folder_id }`，`action`：`created` / `renamed` / `deleted`（另带 `moved_to_root`），见 5.5 | 群里的在线成员 |
| `message.featured.updated` | 现有事件，不变 | 群里的在线成员 |

## 5. 群文件（Pro 起解锁）

- 所有成员都能上传（被群禁言 / 群开了全员禁言 / 被平台禁言的不行，群主 / 管理员不受群内禁言限制）。文件类型不限。
- 文件分两种：
  - **空间文件**：占群文件空间。默认 24 小时后过期；群设置里的 `file_default_ttl_hours`（24 / 168 / 720）决定上传时用多久。群主 / 管理员可以把一个文件**转为永久**，也可以**转回限时**（从现在起算，用群当前的默认时长）。群主 / 管理员上传时可以带 `permanent=true` 直接传成永久。**没有单独的单文件上限**，放得进剩余空间就行。
  - **临时文件**（`temporary = true`）：剩余空间放不下时传成临时文件，**固定 24 小时**后过期，**不占空间配额**；上传时的 `permanent` 不管用。大小上限按档位：Pro 10MB、Pro+ 30MB、MAX 100MB。Pro 没有空间（`file_quota_bytes = 0`），传的都是临时文件。
- 上传怎么定：`size <= free_bytes` → 空间文件；否则 `size <= temp_max_bytes` → 临时文件；否则 413 `file_too_large`。上传**不会**返回 `quota_exceeded`。
- 临时文件**转永久**（`PATCH {"permanent": true}`）：剩余空间放得下才行，转完就是普通的永久空间文件（`temporary = false`，开始占配额）；放不下 413 `quota_exceeded`（Pro 没有空间，一定是这个结果）。临时文件 `PATCH {"permanent": false}` 什么都不做，原样返回。
- **空间配额**只算未过期的空间文件的字节数（永久的也算，临时文件不算）。
- 档位降低（比如 Pro+ 到期后买了 Pro）：已有的空间文件不删，照常到期，过期前继续占配额；剩余空间按新档位算（可能是 0）。
- 文件名清洗：去掉路径、控制字符、零宽字符，首尾空格和点，最长 120 个字（保留扩展名），空的叫 `file`。
- 安全：下载一律 `Content-Disposition: attachment`，`X-Content-Type-Options: nosniff`，不执行、不内联渲染；`Content-Type` 对外一律 `application/octet-stream`（`GroupFileRead.content_type` 是上传时声明的类型，只用来显示图标）。文件存放在非公开的目录（`group-files/`，不可通过 `/uploads/` 直链访问），**下载必须走下面的下载接口**，要求是群成员。
- 过期的文件：列表默认不返回；下载 410 `file_expired`。后台 worker 每小时一次删库行和存储里的对象。
- **直传直下**（`FileQuota.direct = true` 时可用，见 5.4）：上传、下载都用预签名 URL 直接和对象存储传，不经过后端；`direct = false` 时用 multipart 上传和 `/download`。
- **文件夹**（Pro+ / MAX，见 5.5）：一层文件夹，群主 / 管理员管理；列表默认只看根目录。

### 5.1 接口

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `POST /conversations/{cid}/files` | 群成员（未被禁言） | multipart：`file`（必填）、`announce`（可选，默认 `true`：在群里落一条文件消息）、`permanent`（可选，仅群主 / 管理员有效，默认 `false`）、`folder_id`（可选，传进这个文件夹，见 5.5）。201，返回 `{ file: GroupFileRead, message: MessageRead \| null }` |
| `GET /conversations/{cid}/files?folder_id=&q=&uploader_id=&limit=&offset=` | 群成员 | `{ items: [GroupFileRead], total, quota: FileQuota, folders, folder, folder_id, search, can_manage_folders, can_tidy_folders, folder_limit }`，新的在前。不带 `folder_id` 是根目录，带了是这个文件夹（5.5）。`q` 按文件名包含匹配，`uploader_id` 按上传人筛选，带了任一个就是跨文件夹搜索；`limit` 1–100（默认 30） |
| `GET /conversations/{cid}/files/usage` | 群成员 | `FileQuota` |
| `GET /conversations/{cid}/files/{file_id}/download` | 群成员 | 带鉴权下载（`Authorization` 头，和别的接口一样），`attachment`。直传开着时也能用（后端从私有桶读出来转发） |
| `POST /conversations/{cid}/files/uploads` | 群成员（未被禁言） | 直传开单，见 5.4。201，返回上传单 |
| `POST /conversations/{cid}/files/uploads/{upload_id}/complete` | 开单的人 | 直传完成，见 5.4。201，返回和 multipart 上传一样的 `{ file, message }` |
| `GET /conversations/{cid}/files/{file_id}/link` | 群成员 | 直连下载链接，见 5.4。`{ url, expires_at }` |
| `DELETE /conversations/{cid}/files/{file_id}` | 上传人本人、群主、管理员 | 204；删库行和存储里的对象，释放配额。已经落在群里的文件消息不动（点开显示文件已不存在） |
| `PATCH /conversations/{cid}/files/{file_id}` | 群主、管理员 | `{ "permanent": true }` 转永久 / `{ "permanent": false }` 转回限时（临时文件见上面的规则）。返回 `GroupFileRead` |

```jsonc
// GroupFileRead
{ "id": 5, "conversation_id": 7, "name": "设计稿.zip", "size": 1048576, "content_type": "application/zip",
  "uploader": { /* UserPublic，上传人账号已删除为 null */ }, "uploader_id": 42,
  "created_at": "…", "expires_at": "…",       // 永久文件 expires_at 为 null
  "permanent": false,
  "temporary": false,                          // 临时文件（空间放不下时传的，24 小时，不占配额）
  "expired": false,
  "message_id": 901,                           // 落了消息才有，否则 null；消息被修剪后指向的消息可能已经不在
  "download_path": "/api/v1/conversations/7/files/5/download",
  "folder_id": 3,                              // 所在的文件夹，根目录为 null（5.5）
  "folder_name": "设计稿" }                     // 根目录为 null

// FileQuota
{ "state": "active", "tier": "pro_plus", "grace_until": null,
  "quota_bytes": 209715200,       // 群文件空间；Pro 为 0，未解锁为 null
  "used_bytes": 1048576,          // 只算没过期的空间文件，不含临时文件
  "file_count": 3,                // 没过期的文件数，含临时文件
  "default_ttl_hours": 24,
  "free_bytes": 208666624,        // max(0, quota_bytes - used_bytes)；未解锁为 0
  "temp_max_bytes": 31457280,     // 这一档临时文件的上限；未解锁为 null
  "temp_ttl_hours": 24,
  "max_file_bytes": 208666624,    // 现在能传的最大文件 = max(free_bytes, temp_max_bytes)；未解锁 / 宽限期为 0
  "direct": true,                 // 能不能直传直下（5.4）；/files/usage、/files 的 quota、/plus 的 files 都带
  "folders_enabled": true }       // 档位能不能用文件夹（有效档位是 Pro+ / MAX，5.5）
```

### 5.2 文件消息

上传时 `announce = true`（默认）会在群里落一条**普通文本消息**（`type = "text"`，不新增消息类型，所以旧客户端直接显示这行字）：

```jsonc
{ "type": "text",
  "content": "[文件] 设计稿.zip（1.0 MB）",
  "metadata": { "file": { "id": 5, "name": "设计稿.zip", "size": 1048576, "content_type": "application/zip",
                          "expires_at": "…", "permanent": false, "temporary": false } } }
```

- 走正常的发消息路径和广播（`message.created`，计入未读、会话预览、群活跃度，受发言限频约束）。发消息那一步被拒（限频等），**文件不保存**，整个上传回滚。
- 新客户端按 `metadata.file` 渲染文件卡片，用 `id` 调下载接口；用卡片里的 `expires_at` 自己显示「已过期」，不用等服务端事件。
- 不能通过普通的发消息接口伪造：`POST /conversations/{id}/messages` 的 `metadata` 里带 `file` 键，422 `file_not_sendable`。
- **文件消息不能转发**（合并转发里只要有一条，整个请求 422 `file_not_forwardable`）。
- 文件消息的机器审核只看那行文字（文件名）。

### 5.3 错误

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 404 | — | 会话不存在 / 我不是成员 |
| 400 | `not_group` | 私聊没有群文件 |
| 403 | `files_locked` | 这个群还没解锁群文件（需要 Pro），或者宽限期内只读（`detail.state = "grace"`，带 `grace_until`）。上传返回；下载 / 列表在 locked 且没有历史文件时返回空 |
| 403 | `muted` | 被禁言，不能上传（带 `muted_until`） |
| 413 | `file_too_large` | 上传：剩余空间放不下，也超过了临时文件上限。带 `max_file_bytes`、`temp_max_bytes`、`free_bytes` |
| 413 | `quota_exceeded` | 临时文件转永久：剩余空间放不下（上传不会返回这个）。带 `quota_bytes`、`used_bytes`、`size` |
| 422 | `empty_file` | 空文件 |
| 404 | `file_not_found` | 文件不存在（或属于别的群） |
| 410 | `file_expired` | 文件已过期（下载） |
| 403 | `not_group_staff` | 转永久 / 转限时 / 删别人的文件需要群主 / 管理员 |
| 429 | `rate_limited` | 撞上了发消息限频（落消息那一步），文件没有保存 |
| 409 | `direct_upload_unavailable` | 开单：服务器没开直传，改用 multipart 上传 |
| 404 | `upload_not_found` | complete：上传单不存在、已过期（1 小时）、属于别的群、不是你开的，或者已经 complete 过 |
| 409 | `upload_incomplete` | complete：桶里还没有这个对象（还没传完），上传单保留，传完再 complete |
| 422 | `upload_size_mismatch` | complete：传上去的大小和开单时的 `size` 不一致，对象和上传单都删掉。带 `size`、`uploaded_size` |
| 409 | `direct_download_unavailable` | `/link`：服务器没开直连下载，改用 `/download` |

### 5.4 直传直下（预签名 URL）

后端配置了 `STORAGE_BACKEND=s3` 和 `S3_PRIVATE_BUCKET`（群文件单独放的私有桶，和主桶共用 endpoint / 凭据）时打开，`FileQuota.direct = true`。
打开后 `group-files/` 前缀的对象全部存私有桶（multipart 上传、`/download`、删除、过期清理都按 key 自动选桶）；其它对象（头像、聊天图……）还在主桶。没配 `S3_PRIVATE_BUCKET` 时一切照旧。

**上传**：

1. 开单 `POST /conversations/{cid}/files/uploads`，body `{ "name": "设计稿.zip", "size": 1048576, "content_type": "application/zip", "permanent": false, "folder_id": null }`（`content_type` 可空，只用来显示图标；`permanent` 同 multipart，仅群主 / 管理员有效；`folder_id` 可选，见 5.5）。权限、禁言、群文件状态、空文件、大小 / 空间 / 临时文件的检查和 multipart 上传完全一样，错误码也一样（`files_locked`、`muted`、`file_too_large`、`empty_file`……）；都过了但直传没开，409 `direct_upload_unavailable`。201：

   ```jsonc
   { "upload_id": "…",                    // 随机串
     "url": "https://…",                  // 预签名 PUT，15 分钟内有效
     "method": "PUT",
     "headers": { "Content-Type": "application/octet-stream" },   // 必须原样带上，不能多也不能改
     "expires_at": "…",                   // url 的过期时间
     "temporary": false,                  // 按现在的剩余空间预估会不会是临时文件；以 complete 的结果为准
     "folder_id": null }                  // 预估放进的文件夹（预估是临时文件时为 null）；以 complete 的结果为准
   ```

2. 客户端 `PUT url`，请求体就是文件本身，带上 `headers`。签名里固定了 `Content-Length = size`，长度不对桶会拒（是否真的校验取决于服务商，complete 时还会再核对）。
3. 完成 `POST /conversations/{cid}/files/uploads/{upload_id}/complete`，body `{ "announce": true }`（可省略，默认 `true`）。只有开单的人能调。后端 head 一下对象：没有 → 409 `upload_incomplete`；大小不对 → 删对象，422 `upload_size_mismatch`。然后重新做一遍权限检查（开单之后被禁言 / 群升级到期了 → 对应的 403，对象删掉），再按**现在**的剩余空间定空间文件还是临时文件（可能已经被别的上传占了：放得进临时上限就变成临时文件，否则删对象、413 `file_too_large`），记录、落文件消息，和 multipart 上传是同一条路（限频被拒同样整个回滚、删对象，429 `rate_limited`）。201，返回和 multipart 上传一样的 `{ file: GroupFileRead, message: MessageRead | null }`，同样广播 `message.created` 和 `group.file.updated`。上传单随之删除，再 complete 一次 404。
4. 开了单 1 小时内没 complete 的上传单作废；后台 worker 每小时清理一次，连同桶里可能已经传上去的对象。

**下载**：`GET /conversations/{cid}/files/{file_id}/link`，检查和 `/download` 一样（群成员、过期 410 `file_expired`、宽限期可读）。直传开着 → `{ "url": "https://…", "expires_at": "…" }`：5 分钟内有效的预签名 GET，响应头被签成 `Content-Type: application/octet-stream`、`Content-Disposition: attachment; filename="ASCII 兜底名"; filename*=UTF-8''…`（和 `/download` 一样）；直传没开 → 409 `direct_download_unavailable`，改用 `/download`。链接不带 cookie、不用 `Authorization` 头，可以直接交给浏览器 / 系统下载器。

**雨云配置**（或别的 S3 兼容服务）：

- 新建一个桶专门放群文件（例如 `kukechat-file`），**公开访问关闭**（私有读写）；主桶照旧开着公开读，图片和头像要用。
- 给这个桶配 CORS（浏览器里直传要用）：AllowedOrigins `*`，AllowedMethods `GET` `PUT` `HEAD`，AllowedHeaders `*`，ExposeHeaders `ETag`，MaxAge `3600`。`*` 是安全的：预签名 URL 自己带着授权（签名、过期时间、限定的 key 和长度），不靠 cookie 或来源；拿不到签名的页面什么也做不了。控制台里没有 CORS 设置的话，用下面脚本的 `--apply-cors` 写入。
- 后端环境变量加 `S3_PRIVATE_BUCKET=kukechat-file`（endpoint、密钥、region 和主桶共用）。`S3_ENDPOINT_URL` 必须是 `https://`，否则预签名 URL 是 http 的，https 页面里会被浏览器当成混合内容拦掉。
- 上线前在服务器上带着生产环境变量跑一遍 `python scripts/check_s3_direct.py`（加 `--apply-cors` 先写 CORS 规则）：实测预签名 PUT / 长度校验 / head / 预签名 GET 的下载头 / CORS 预检 / 不带签名读被拒，最后删掉测试对象，打印 PASS / FAIL。不打印密钥。
- 已有的群文件在主桶里：配了私有桶之后后端会去私有桶找，旧文件就读不到了。上线时先把主桶 `group-files/` 下的对象迁到私有桶同名 key（或者在群文件还没真正用起来时上线）。

### 5.5 文件夹（Pro+ / MAX）

**规则**

- **档位**：只有当前**有效档位**是 Pro+（`pro_plus`）或 MAX（`max`）的群能新建、重命名文件夹，以及把文件放进文件夹（移入、上传进去）。永久档位按它的档位算（永久 MAX 可以）。免费、Pro 没有空间，不能用，返回 403 `folders_require_plus`（带 `tier`：现在的有效档位）。`FileQuota.folders_enabled` 就是这个判断（`/files/usage`、`/files` 的 `quota`、`/plus` 的 `files` 都带）。
- **谁能操作**：群主、管理员能新建、重命名、删除文件夹，能批量移动文件（移入 / 移出）。普通成员只能浏览；上传权限照旧（能上传的成员都能直接传进文件夹，被禁言的照样不能传）。不是群管：403 `not_group_staff`。
- **结构**：只有一层，文件夹里不能再建文件夹。一个文件同一时间只在一个地方（根目录或某一个文件夹）。
- **名字**：去掉首尾空白后 1–40 个字；不能有换行（含 ` ` / ` `）、控制字符、零宽等不可见格式字符、`/`、`\`，否则 422 `invalid_folder_name`。同一个群里**不区分大小写**不能重名（按 NFKC + casefold 比较，全角 / 半角也算同名），重名 409 `folder_name_taken`；重命名时只改大小写可以。名字**不过文字审核**（群文件名、群名现在也都不审，和它们保持一致）。
- **数量**：每个群最多 50 个文件夹，满了 409 `folder_limit`（带 `limit`）。
- **删除文件夹**：里面的文件全部移回根目录，**不删文件本身**。
- **临时文件不进文件夹**：24 小时临时文件、已过期的文件不能移进文件夹（422 `file_not_movable`）；上传时变成临时文件的（空间满了），**忽略 `folder_id`、放根目录**，返回的 `file.folder_id` 为 `null`，客户端可以提示「空间不够，已作为临时文件放在根目录」。移回根目录不受限制。
- **降档后**（到期进宽限期、或者换成了 Pro）：文件夹和里面的文件照常浏览、下载、删除；**不能**新建、重命名文件夹，不能移入、不能上传进文件夹（都是 403 `folders_require_plus`）；**可以**删除文件夹（文件回根目录）和把文件移回根目录，方便收拾。过了宽限期群文件被清空时，文件夹也一起删掉。
- **旧客户端**：不传 `folder_id` 的列表只返回根目录的文件，所以放进文件夹的文件在旧客户端的列表里看不到（文件消息卡片、下载不受影响，搜索也能搜到）。

**列表** `GET /conversations/{cid}/files?folder_id=&q=&uploader_id=&limit=&offset=`（群成员）

- 不带 `folder_id`、不带 `q` / `uploader_id`：**根目录**的文件。
- 带 `folder_id`：这个文件夹里的文件；文件夹不存在 / 属于别的群：404 `folder_not_found`。
- 带了 `q` 或 `uploader_id` 就是**搜索**：跨根目录和所有文件夹搜，`folder_id` 被忽略（`search: true`）；每个文件带 `folder_id`、`folder_name` 告诉客户端它在哪。
- 不管哪种，都返回这个群的**全部**文件夹（`folders`），排序是**按创建先后**（`created_at` / `id` 升序，新建的在最后；想按名字排客户端自己排）。

```jsonc
{ "items": [GroupFileRead], "total": 2, "quota": FileQuota,
  "folder_id": null,               // 现在看的文件夹；根目录 / 搜索为 null
  "folder": null,                  // 现在看的文件夹的 GroupFileFolderRead；根目录 / 搜索为 null
  "folders": [GroupFileFolderRead],
  "folder_limit": 50,
  "search": false,
  "can_manage_folders": true,      // 我能新建、重命名文件夹、把文件移进去（群管 + 档位够）→ 显示「新建文件夹」「移动到…」
  "can_tidy_folders": true }       // 我能删文件夹、把文件移回根目录（群管，降档后也是 true）→ 显示「删除文件夹」「移回根目录」

// GroupFileFolderRead
{ "id": 3, "conversation_id": 7, "name": "设计稿",
  "file_count": 2,                 // 里面没过期的文件数
  "total_size": 1048576,           // 里面没过期的文件的总字节数
  "created_by": { /* UserPublic，账号已删除为 null */ }, "created_by_id": 42,
  "created_at": "…", "updated_at": "…" }   // 重命名会更新 updated_at
```

`GroupFileRead` 新增 `folder_id`（根目录为 `null`）、`folder_name`（根目录为 `null`），所有返回文件的接口都带。

**接口**

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /conversations/{cid}/files/folders` | 群成员 | `{ items: [GroupFileFolderRead], limit: 50, can_manage_folders, can_tidy_folders }`，排序同上 |
| `POST /conversations/{cid}/files/folders` | 群主、管理员；Pro+ / MAX | body `{ "name": "设计稿" }`。201，返回 `GroupFileFolderRead` |
| `PATCH /conversations/{cid}/files/folders/{folder_id}` | 群主、管理员；Pro+ / MAX | body `{ "name": "新名字" }`，重命名。返回 `GroupFileFolderRead` |
| `DELETE /conversations/{cid}/files/folders/{folder_id}` | 群主、管理员（降档后也行） | 删文件夹，里面的文件移回根目录。200，`{ "folder_id": 3, "moved_to_root": 2 }` |
| `POST /conversations/{cid}/files/move` | 群主、管理员 | body `{ "file_ids": [5, 6], "folder_id": 3 }`，`folder_id: null` = 移回根目录（降档后也行）；移进文件夹要 Pro+ / MAX。`file_ids` 1–100 个（重复的去重），超出 / 为空 422（FastAPI 的校验错误）。**要么全部移动，要么一个都不动**。返回 `{ "folder_id": 3, "moved": 2, "items": [GroupFileRead] }`（按请求里的顺序） |

**上传到文件夹**

- multipart `POST /conversations/{cid}/files` 多一个可选的表单字段 `folder_id`。
- 直传开单 `POST /conversations/{cid}/files/uploads` 的 body 多一个可选的 `folder_id`；返回的上传单多一个 `folder_id`（预估：预估是临时文件时为 `null`）。complete 不用再传，用开单时的。
- 检查顺序：先做原来的上传检查（`files_locked`、`muted`……），再查文件夹：档位不够 403 `folders_require_plus`，文件夹不是这个群的 404 `folder_not_found`。
- 最后落库时（multipart 的写入、直传的 complete）：变成了临时文件、文件夹在这期间被删了、或者群已经降档了，就**放根目录**，不报错（文件已经传上去了，不让它白传）。以返回的 `file.folder_id` 为准。

**错误**

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 403 | `folders_require_plus` | 档位不是 Pro+ / MAX：新建、重命名、移入、上传进文件夹。带 `tier` |
| 403 | `not_group_staff` | 新建 / 重命名 / 删除文件夹、移动文件需要群主 / 管理员 |
| 404 | `folder_not_found` | 文件夹不存在或属于别的群（列表、重命名、删除、移入、上传） |
| 409 | `folder_name_taken` | 同一个群里已经有同名（不区分大小写）的文件夹 |
| 409 | `folder_limit` | 已经有 50 个文件夹了。带 `limit` |
| 422 | `invalid_folder_name` | 名字为空、超过 40 个字、有换行 / `/` / `\` / 不可见字符 |
| 404 | `file_not_found` | 移动：有文件不存在或属于别的群。带 `file_ids`（不存在的那些），一个都没动 |
| 422 | `file_not_movable` | 移进文件夹：有临时文件或已过期的文件。带 `temporary_ids`、`expired_ids`，一个都没动 |

**实时事件**（发给群里的在线成员）

- `group.file.folders.changed`：`{ conversation_id, action, folder_id }`，`action`：`created` / `renamed` / `deleted`；`deleted` 另带 `moved_to_root`（移回根目录的文件数）。收到后重拉 `GET /files/folders`（或当前列表）；正在看被删的文件夹时回到根目录。
- `group.file.updated` 新增 `action: "moved"`：`{ conversation_id, action: "moved", file_id, file_ids, folder_id }`（`file_id` 是第一个，给只认 `file_id` 的旧逻辑；`folder_id` 是目标，`null` = 根目录）。`uploaded` 事件也多带 `folder_id`。上传、删除、移动都会改变文件夹的 `file_count` / `total_size`，收到任何 `group.file.updated` 时顺手刷新文件夹列表。

## 6. 精华消息（永久）

精华不再只引用 `message_id`：**设为精华时把消息的内容、类型、metadata、发送人快照（昵称、头像、群头衔）、发送时间固化进精华表**。消息之后被修剪（每会话只留最近 N 条）、发送人改名，精华都原样保留。

- 修剪不会连带删精华；精华引用的上传文件（图片等）不会被修剪删除（修剪本来就不删上传对象）。
- **被撤回的消息**：精华保留，但标记 `recalled = true` 并隐藏内容（`message.content = ""`、`metadata = null`；服务端也把库里的快照内容清掉）。撤回、机器审核撤回、群管撤回、后台按消息撤回都一样。案件改判把消息恢复之后，精华仍显示已撤回，需要重新设置。
- **数量上限**按档位（100 / 300 / 800 / 3000）。超过上限设精华：409 `featured_limit`，`detail` 带 `limit`、`used`、`tier`。**权益过期后已有精华保留**，用量超过当前上限也不删，只是不能再添加。取消精华不受限制。
- 已有精华在升级时回填：按当时还在的消息补快照；已经没了的只留空壳并标记 `snapshot_missing = true`（`message.content = ""`）。

### 6.1 接口

| 接口 | 说明 |
| --- | --- |
| `POST /conversations/{cid}/messages/{mid}/feature` | 切换精华（群主 / 管理员）。响应不变 `{ featured }`。新增 409 `featured_limit`。消息要在库里（被修剪掉的消息不能设精华） |
| `GET /conversations/{cid}/featured-messages?limit=&offset=` | 群成员。返回 `FeaturedMessageRead[]`（**仍是数组**，老客户端不受影响），新的在前。`limit` 1–200（默认 50），`offset` ≥ 0。分页信息在响应头：`X-Total-Count`（总条数）、`X-Has-More`（`true` / `false`） |
| `DELETE /conversations/{cid}/featured-messages/{featured_id}` | 群主 / 管理员。按精华 id 取消精华（原消息已被修剪时用它）。返回 `{ featured: false }` |

```jsonc
// FeaturedMessageRead（旧字段都在）
{ "id": 3, "conversation_id": 7,
  "message_id": 901,              // 原消息 id；原消息已被修剪后仍保留这个数字，但消息已经不存在（message_exists = false）
  "set_by_id": 42, "set_by": { /* UserPublic | null */ },
  "created_at": "…",              // 设为精华的时间
  "message": { /* MessageRead：由快照构造；sender 是发送人快照，id 是原消息 id；撤回的 content = "" */ },
  "message_exists": true,         // 原消息现在还在库里（可以跳转）
  "recalled": false, "snapshot_missing": false }
```

「查看全文」就用 `message.content`（快照含完整文本）。`GET /conversations/{cid}/plus` 的 `usage.featured_count` / `limits.featured_limit` 用来显示「已用 / 上限」。`message.featured`（消息读取里的精华标志）仍然按 `message_id` 匹配。

## 7. 群待办（免费功能）

每个群**最多一条**群待办，群主 / 管理员可以对某条消息设置。设置会把消息快照固化（含完整文本），并给全群发一条「@全体成员」式的提醒。

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /conversations/{cid}/todo` | 群成员 | `{ todo: GroupTodo \| null }` |
| `PUT /conversations/{cid}/todo` | 群主 / 管理员 | body `{ "message_id": 901 }`。已有待办就**替换**（始终只有一条）。返回 `{ todo, message }`，`message` 是落下的提醒消息（`MessageRead`） |
| `DELETE /conversations/{cid}/todo` | 群主 / 管理员 | 取消。204 |

```jsonc
// GroupTodo（也是 ConversationRead.group_todo）
{ "conversation_id": 7,
  "message_id": 901,
  "message_exists": true,     // 原消息还在库里且没被撤回：true 就跳转，false 就打开「查看全文」
  "recalled": false,          // 原消息被撤回：content / summary 已隐藏
  "summary": "周六晚上八点开会",       // 前 60 个字；图片是 [图片]；撤回是 [消息已撤回]
  "snapshot": { "type": "text", "content": "完整文本…", "metadata": null,
                "sender": { "id": 42, "display_name": "阿夸", "avatar_url": "…", "title": "管理员" },
                "created_at": "…" },
  "set_by": { "id": 5, "display_name": "…", "avatar_url": "…" },
  "set_at": "…" }
```

- **提醒**：落一条普通文本消息，`content = "[群待办] {summary}"`，`metadata = { "mention_all": true, "group_todo": { "message_id": 901 } }`，发送人是设置人。它走现有的 @全体成员 链路：会话列表 `mention_pending = "all"`、`message.created`、通知预览里带「[群待办]」。**不占用也不受限于** @全体成员的每日次数（20 次 / 天）。旧客户端看到的就是一条 @全体 的文本。
- 只能对群聊里、没被撤回、对我可见的**文本 / 图片 / 语音 / 合并转发 / 投票 / 文件**消息设置；系统消息、红包、礼物 422 `not_todoable`。
- 取消不发提醒消息。
- 原消息被撤回：待办保留，标记 `recalled = true` 并清掉快照内容。原消息被修剪：`message_exists = false`，快照照常。
- `group.todo.updated`：`{ conversation_id, todo: GroupTodo | null }`，发给群里的在线成员（设置、替换、取消都发；提醒消息另有 `message.created`）。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 400 | `not_group` | 私聊没有群待办 |
| 403 | `not_group_staff` | 不是群主 / 管理员 |
| 404 | — | 消息不存在 / 不可见 |
| 422 | `not_todoable` | 消息类型不支持，或已被撤回 |
| 404 | `todo_not_found` | 取消时本来就没有待办 |

## 8. 聊天记录搜索（QQ 式）

搜的是**当前用户自己**的聊天记录：只在自己是成员的会话里、自己可见的消息中搜（尊重「清空聊天记录」`cleared_before_message_id` 和「仅自己删除」`MessageVisibility`；已撤回的、已被修剪的搜不到）。私聊群聊都搜。**这不是管理员入口**：后台没有任何搜索消息的接口，也不会有。

被索引的消息：文本（含文件消息的文件名、群待办提醒）、合并转发（摘要 + 里面每条消息的文字）、投票（标题和选项）、图片 / 表情（只用于分类筛选，没有文字可搜）。系统消息、红包、礼物、语音不进索引。

### 8.1 `GET /search/messages`

| 参数 | 说明 |
| --- | --- |
| `q` | 关键词，≤100 字。空格分隔多个词，**全部命中**才算（AND），不区分大小写，按子串匹配（中文不需要分词） |
| `conversation_id` | 可选，限定一个会话（必须是我的会话，否则 404） |
| `sender_id` | 可选，按发送人筛选 |
| `category` | `all`（默认）/ `media`（图片，不含表情）/ `sticker`（表情）/ `file`（文件消息）/ `link`（含链接的文本） |
| `date_from` / `date_to` | 可选。日期 `2026-10-01`（按 UTC+8 的整天，`date_to` 含当天）或完整 ISO 时间（`date_from <= 时间 < date_to`） |
| `limit` | 1–50，默认 20 |
| `offset` | 默认 0 |
| `before_id` | 游标：只要 id 小于它的消息（和 `offset` 二选一，翻页用 `next_before_id` 最省） |

`q`、`conversation_id`、`sender_id`、`category != all`、日期至少要有一项，否则 422 `query_required`。排序：消息 id 从新到旧。

```jsonc
{ "items": [
    { "message": { /* MessageRead */ },
      "conversation": { "id": 7, "type": "group", "title": "群名", "display_title": "我的备注或群名",
                        "avatar_url": "…", "plus_tier": "max", "plus_expires_at": "…",
                        "direct_user": null },        // 私聊：对方 UserPublic，title 为 null
      "category": "text",                             // text / media / sticker / file / link
      "snippet": "…今天下午三点开会…",                  // 命中处前后各约 36 字，带省略号
      "snippet_highlights": [[3, 5]],                  // 在 snippet 里的命中区间 [start, end)，按 UTF-16 码元（JS 字符串下标）
      "highlights": [[10, 12]] }                       // 在 message.content 里的命中区间，同样口径
  ],
  "total": 37,                  // 命中总数；超过 10000 时封顶为 10000，total_capped = true
  "total_capped": false,
  "limit": 20, "offset": 0, "has_more": true, "next_before_id": 880 }
```

- 点击一条结果跳转：用 `GET /conversations/{cid}/messages/{mid}/context`（现有的）。
- 没有关键词时（只按分类 / 日期 / 发送人）`snippet` 是消息开头，`highlights` 为空。

### 8.2 `GET /search/messages/summary?q=`

「按会话分组的命中数」：先列会话，再点进去。参数 `q`（必填，1–100 字）、`category`、`sender_id`、`date_from`、`date_to`（含义同上）、`limit`（1–100，默认 50）。

```jsonc
{ "total": 124,       // 命中的消息总数（所有会话）
  "items": [ { "conversation": { /* 同上的会话摘要 */ }, "count": 52, "latest_message_id": 901, "latest_at": "…" } ] }
```

按 `latest_message_id` 从新到旧。点进某个会话后用 `GET /search/messages?q=…&conversation_id=…`。

### 8.3 实现说明（不影响契约）

- SQLite 上用 FTS5 + trigram 分词器建独立的索引表（`message_search_index` + `message_search_fts`），用触发器随 `messages` 的新增 / 撤回 / 删除（含修剪）同步；首次启动自动回填。
- 关键词少于 3 个字（trigram 的最小单位）时退回 `LIKE`，仍然限定在用户自己的会话里，用 `(conversation_id, message_id)` 索引。
- SQLite 不支持 FTS5 / trigram 时整体退回 `LIKE`（接口行为不变，只是慢）。

## 9. 后台（`/admin/group-plus/*`，管理员）

| 接口 | 说明 |
| --- | --- |
| `GET /admin/group-plus/settings` | `{ enabled, prices: { pro, pro_plus, max }, year_months_paid, defaults: { pro, pro_plus, max }, tiers: [同 /group-plus/plans 的 tiers，只读] , file_max_bytes, file_grace_days }`（`file_max_bytes` 同 3.1，是旧字段）。`prices` 是每月价格 |
| `PUT /admin/group-plus/settings` | body 可以只传要改的：`{ enabled?, price_pro?, price_pro_plus?, price_max? }`。返回完整设置。不合法 422 `invalid_settings`（`detail.field`）。有变化才写审计日志 `group_plus.settings.update` |
| `GET /admin/group-plus/groups?tier=&expiring_within_days=&q=&include_expired=&include_free=&limit=&offset=` | 有档位的群（还没到期的；`include_expired=true` 连已过期的也列；`include_free=true` 列所有群，含免费群，按群 id 排序 —— 找群设人数上限用）。`tier` 筛选档位；`expiring_within_days` 只看 N 天内到期的；`q` 按群名 / 群 id。返回 `{ items, total }`，每项 `{ conversation_id, title, avatar_url, member_count, owner: {id, display_name} \| null, tier, recorded_tier, expires_at, days_left, expired, permanent, member_limit, member_limit_unlimited, member_limit_override, files_state, featured_count }`（`member_limit` 为 null 表示不限）。永久档位的 `days_left` 是到 2999 年的天数，界面应显示「永久」。**不含任何消息内容** |
| `GET /admin/group-plus/groups/{cid}` | 任意一个群（含免费群）的同一个对象。不是群 404 `group_not_found` |
| `PUT /admin/group-plus/groups/{cid}/member-limit` | body `{ member_limit_override }`：`null` 跟随档位 / `0` 不限 / `1–100000` 自定义。返回 `{ group: 同上, conversation: { conversation_id, plus_tier, plus_expires_at, plus_permanent }, changed, previous }`。见 9.2 |
| `POST /admin/group-plus/grant` | body `{ conversation_id, tier, days, permanent?, note? }`：赠送。`tier` 为 `pro` / `pro_plus` / `max`，`days` 1–3650；`permanent: true` 是送成**永久**（`days` 不用传，见 9.2）。同档位在原到期时间上加；不同档位直接替换成新档位（后台可以降档），到期 = 现在 + days。`tier = "free"` 表示**取消**（`days` 不用传）：立即降回免费，文件进入 30 天宽限。写 `group_plus_records`（`source = grant` / `revoke`，不收费），广播 `group.plus.updated` |
| `GET /admin/group-plus/records?conversation_id=&user_id=&source=&limit=&offset=` | 全站流水，新的在前：`{ items: [GroupPlusRecord + { conversation_title }], total, summary: { total_amount, total_count } }`（summary 是全站累计，不随筛选变） |

审计日志（`admin_audit_logs`）：

| `action` | `target_type` / `target_id` | `details` |
| --- | --- | --- |
| `group_plus.settings.update` | `system_setting` / 空 | `{ 改了的项: { old, new } }` |
| `group_plus.grant` | `conversation` / 群 id | `{ tier, days, permanent, prev_tier, new_expires_at, note }` |
| `group_plus.revoke` | `conversation` / 群 id | `{ prev_tier, prev_expires_at, note }` |
| `group_plus.member_limit` | `conversation` / 群 id | `{ old, new }`（`member_limit_override` 改前 / 改后；没变化不写） |

### 9.1 对账

`POST /admin/shop/reconcile` 的返回多了 `group_plus`：

```jsonc
{ "total_amount": 5400, "total_count": 9, "granted_count": 2, "revoked_count": 1,
  "balanced": true, "problems": [] }
```

`balanced` 为 true 表示每个有档位的群，它现在的档位 / 到期时间都等于它**最后一条记录**记下的结果（`problems` 列出对不上的群，最多 50 个：`{ conversation_id, tier, expires_at, record_tier, record_expires_at }`）；「余额为负」的检查（`mismatched`）已经把群升级算进去了。

`GET /admin/shop/accounts` 的每一项多了 `group_plus_spent`，`balance` 按新公式算；`shared_spent` 里包含这个 CCW 账号在别的账号上买群升级花掉的。

### 9.2 永久档位与人数上限

**永久档位**：`POST /admin/group-plus/grant` 带 `permanent: true`。到期时间写成哨兵值 `2999-12-31T00:00:00Z`，所以现有的到期判断、消息修剪、群文件宽限都不用改；判断「是不是永久」一律看到期年份 >= 2999（服务端 `core/group_plus_tiers.is_permanent`），不要和这个值做相等比较。记录里 `days = 0`、`permanent = true`。

- 永久的群：购买 / 报价 409 `already_permanent`（第 1 节、3.4）。
- 给已经是永久的**同档位**再送天数：保持永久，不变；送**不同档位**的天数：按原规则直接替换成「现在 + days」（永久没了，后台可以降档）。
- 取消（`tier = free`）照常，取消之后可以正常购买。
- 对外：会话对象 `plus_permanent`、`GET /conversations/{cid}/plus` 的 `permanent`、`GroupPlusRecord.permanent`、后台群列表的 `permanent`、`group.plus.updated` 的 `plus_permanent`。

**人数上限单独设置**：`conversations.member_limit_override`（INTEGER，可空）。`null` 跟随档位（默认，老群都是 null）；`0` 不限；正数就是这个上限（1–100000，可以比档位的大或小）。

- 所有加人的入口（加群、邀请、拉人、审批入群申请、邀请链接、机器人、游戏模式）都经过同一个检查，满了是 409 `group_full`（`detail.limit` 是这个群现在的上限）；不限时不检查。已有成员不踢：自定义的上限比现有人数小，只是不能再加。
- `GET /conversations/{cid}/plus` 的 `limits.member_limit` 按它算；不限时 `member_limit: null` + `member_limit_unlimited: true`。旧客户端：web 端把 null 当 0、安卓端把 null 当默认值 200，显示的上限会不对但不会崩；新客户端显示「不限」。
- `GET /group-plus/plans` 里各档位的 `member_limit` 是档位本身的规格，不受它影响。
- 改了之后广播 `group.plus.updated`（`reason = "member_limit"`）。

### 9.3 服务器上的运维命令

`python -m app.cli.admin_group_tools`（`scripts/admin_group_tools.py` 是同一个东西的外壳；发布只带 `app/`，服务器上用 `-m` 的写法）。在后端的 `WorkingDirectory` 里、用和服务一样的用户和 Python 跑（`.env` 在当前目录）。**默认只预览，加 `--apply` 才写库**；写操作要 `--admin <用户名>`（必须是平台管理员），审计日志和记录都记在他名下。

| 参数 | 做什么 | 走的服务函数 |
| --- | --- | --- |
| `--find "<群名片段>"` | 列出群名包含它的群：id、人数、档位、人数上限（最多 50 个，只读） | — |
| `--permanent-max <群 id>` | 设为永久 MAX | `group_plus.admin_grant`（`permanent: true`） |
| `--unlimited <群 id>` | 人数上限设为不限（`member_limit_override = 0`） | `group_plus.admin_set_member_limit` |
| `--grant-coins <用户 id 或用户名> <金额> --reason "…"` | 加 / 扣金币（负数是扣） | `shop.admin_grant_coins` |

新列 / 新表要等新版本的后端启动过一次（启动时自动迁移）才有；没有时命令直接报错退出，不写任何东西。实时事件尽力而为：配了 `REDIS_URL` 时经 Redis 转给在线的人；没配时这个进程推不到在线用户，客户端刷新 / 重连后看到新状态。

## 10. 实现要点

- **表**：`group_plus_records`（每次购买 / 赠送 / 取消一行，带购买时绑定的 CCW 账号）、`group_files`（`temporary` 列：临时文件；`folder_id` 列：所在文件夹，空 = 根目录，索引 `(conversation_id, folder_id)`）、`group_file_folders`（文件夹，`(conversation_id, name_key)` 唯一，`name_key` = 名字的 NFKC + casefold）、`group_file_uploads`（直传的上传单，complete 或过期后删除；`folder_id` 列）、`group_todos`（每群一行，`conversation_id` 唯一）、`featured_messages`（重建：`message_id` 可空、无外键，加快照列）、`message_search_index` / `message_search_fts`；`conversations` 新增列 `plus_tier`、`plus_expires_at`、`plus_files_tier`、`plus_files_until`、`file_default_ttl_hours`、`member_limit_override`（9.2）。启动时自动迁移（`featured_messages` 重建并回填快照）；稳妥起见上线前先单进程启动一次。
- **并发**：写事务第一条语句是带条件的 `UPDATE shop_accounts`（余额够才往下写，和礼物 / 红包用同一个余额表达式），之后在同一个事务里写记录、改群、提交；写事务先过 `shop._write_lock()`。同一个 CCW 账号换绑后不能再花一遍（记录上有 `ccw_oid`）。
- **存储**：群文件放在 `group-files/{群 id}/{随机名}`。本地存储时 `/uploads/group-files/...` 直链返回 404；S3 存储时桶里这个前缀**不要**开公开读（下载接口用服务端凭据读取再转发）；配了 `S3_PRIVATE_BUCKET` 时这个前缀整个放私有桶，直传直下见 5.4。
- **旧客户端**：精华列表仍是数组、字段只增不减；文件消息、群待办提醒都是普通文本消息；`plus_tier` 等新字段旧客户端忽略；旧的会话内搜索 `GET /conversations/{id}/messages/search` 不变。
