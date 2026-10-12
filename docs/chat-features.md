# 投票、正在输入、群表情、戳一戳、公告回应

路径前缀 `/api/v1`。带 `code` 的错误，`detail` 是 `{ code, message }`，`message` 是可直接展示的中文。

## 1. 投票

投票是一条 `type = "poll"` 的消息，群聊和私聊都能发。消息正文 `content` 是纯文本摘要（`[投票] 标题` 加编号选项），给不认识这个类型的旧客户端、搜索和通知预览用。

### 1.1 消息里的投票对象

`message.metadata.poll`：

```jsonc
{
  "id": 12,                       // 投票 id，后面的接口都用它
  "title": "周末做什么",
  "options": [
    { "id": 1, "text": "写游戏", "votes": 3 },
    { "id": 2, "text": "画画",   "votes": 1 }
  ],
  "multiple": false,              // 是否多选
  "anonymous": false,             // 是否匿名
  "closes_at": null,              // 截止时间（ISO，UTC）；null 表示手动结束前一直有效
  "closed": false,                // 已手动结束，或已过截止时间
  "closed_at": null,
  "total_voters": 4,              // 参与人数（多选时不等于票数之和）
  "my_option_ids": [1]            // 我投了哪些
}
```

- 消息列表、单条消息、`message.created` 事件里都带这个对象。`message.created` 是发给全会话的同一份数据，其中 `my_option_ids` 恒为空、票数恒为 0（刚创建）。
- 到了 `closes_at` 服务端不会推事件。客户端自己按 `closes_at` 把投票显示为已结束即可；之后任何读取都会返回 `closed: true`。
- 消息被撤回（包括被审核撤回）= 投票作废，之后的投票 / 结束 / 查看都返回 409 `poll_void`。

### 1.2 接口

| 接口 | 说明 |
| --- | --- |
| `POST /conversations/{cid}/polls` | 发起投票，返回 `MessageRead`（和发普通消息一样，其它成员收到 `message.created`） |
| `GET /conversations/{cid}/polls/{poll_id}` | 当前状态 `PollState` |
| `PUT /conversations/{cid}/polls/{poll_id}/vote` | 投票 / 改票 / 撤票，返回 `PollState` |
| `POST /conversations/{cid}/polls/{poll_id}/close` | 提前结束，返回 `PollState` |
| `GET /conversations/{cid}/polls/{poll_id}/options/{option_id}/voters?limit=50&offset=0` | 某个选项的投票人 `{ items: UserPublic[], total }`；`limit` 最大 100 |

发起：

```jsonc
{ "title": "周末做什么", "options": ["写游戏", "画画"], "multiple": false, "anonymous": false, "closes_at": null }
```

- 标题 1–80 字；2–10 个选项，每个 1–40 字，不能重复（首尾空格会被去掉）；`closes_at` 必须在一分钟之后、30 天之内。
- 不能通过普通的发消息接口发 `type = "poll"`（422）。

投票：`{ "option_ids": [1] }`。提交的是**完整的选项集合**，会替换之前的选择；`[]` 表示撤票。截止前可以随便改。

`PollState`：

```jsonc
{ "conversation_id": 5, "message_id": 901, "poll_id": 12,
  "options": [{ "id": 1, "votes": 3 }, { "id": 2, "votes": 1 }],
  "total_voters": 4, "closed": false, "closed_at": null, "my_option_ids": [1] }
```

权限：

- 投票、查看：会话成员。**被禁言的人不能发起投票，但可以投票**（投票不算发布内容）。
- 提前结束：发起人；群聊里群主和管理员也可以。私聊里只有发起人。
- 查看投票人：非匿名投票的会话成员。

错误：

| HTTP | code | 含义 |
| --- | --- | --- |
| 422 | `invalid_poll` | 标题 / 选项 / 截止时间不合规（`message` 说明具体哪条） |
| 422 | `poll_rejected` | 标题或选项未通过文本审核 |
| 403 | `account_muted` | 被禁言，不能发起投票 |
| 422 | `invalid_poll_option` | 选项 id 不存在 |
| 422 | `poll_single_choice` | 单选投票提交了多个选项 |
| 409 | `poll_closed` | 投票已结束 |
| 409 | `poll_void` | 投票消息已被撤回 |
| 403 | `poll_forbidden` | 无权结束这个投票 |
| 403 | `poll_anonymous` | 匿名投票不能查看投票人 |
| 404 | — | 投票不存在，或你不是会话成员 |

### 1.3 实时事件

`poll.updated`，发给会话的在线成员：

```jsonc
{ "conversation_id": 5, "message_id": 901, "poll_id": 12,
  "options": [{ "id": 1, "votes": 3 }, { "id": 2, "votes": 1 }],
  "total_voters": 4, "closed": false, "closed_at": null }
```

- 只有汇总，没有 `my_option_ids`（自己的选择以投票接口的返回为准，不要用事件覆盖）。按 `message_id` 修补缓存里的 `metadata.poll`。
- 票数变化是合并推送的：同一个投票一秒内不管来多少票只推一次最新汇总。提前结束会立刻推一次。

## 2. 正在输入

客户端在已有的用户 WebSocket（`/ws`）上发一条 JSON 文本帧：

```json
{ "type": "typing", "conversation_id": 5 }
```

服务端转发给这个会话的其他在线成员，事件 `conversation.typing`：

```jsonc
{ "conversation_id": 5, "user_id": 42, "name": "小明" }   // name：群昵称 > 昵称 > 用户名
```

- 没有「停止输入」事件。客户端输入时每 3 秒左右发一次；收到后显示约 5 秒，没有新的就自己隐藏。
- 服务端兜底限速：同一个人在同一个会话里 2 秒内的重复上报直接丢弃。不落库，不回任何应答；格式不对的帧被忽略。
- **超过 200 人的群不转发**：大群里「谁在输入」没有意义，还会变成对全群的持续广播。私聊和 200 人以内的群正常工作。
- 不会发给把这个会话设为屏蔽（`message_setting = "ignore"`）的成员，也不会发给自己。
- 服务端不知道谁把会话开在前台，所以是发给所有在线成员，客户端只在对应会话可见时展示即可。

隐私开关：`PATCH /users/me` 新增 `share_typing: boolean`（默认 `true`），`GET /auth/me` 等返回的用户对象（`UserRead`）里带同名字段。关掉后别人看不到「我正在输入」；自己仍然能看到别人的。

## 3. 群表情

每个群一套带名字的表情图片。群主和管理员管理，全体成员可用。

| 接口 | 权限 | 说明 |
| --- | --- | --- |
| `GET /conversations/{cid}/emojis` | 群成员 | `GroupEmojiRead[]`，按添加顺序 |
| `POST /conversations/{cid}/emojis` | 群主 / 管理员 | `{ name, image_url }` → `GroupEmojiRead` |
| `PATCH /conversations/{cid}/emojis/{emoji_id}` | 群主 / 管理员 | `{ name }` → `GroupEmojiRead` |
| `DELETE /conversations/{cid}/emojis/{emoji_id}` | 群主 / 管理员 | |

```jsonc
// GroupEmojiRead
{ "id": 3, "conversation_id": 5, "name": "狗头", "url": "https://…/stickers/xxxx.jpg",
  "content_type": "image/jpeg", "created_by_id": 42, "created_at": "…" }
```

上传分两步：先用现有的 `POST /uploads/message-image` 上传图片，再把返回的地址作为 `image_url` 提交。服务端会把图片另存一份（和收藏表情同一套机制），之后与原图无关。

- 名称 1–12 字，同群不重复，空格会被去掉。
- 每个群最多 60 个；单张最终不超过 512KB；png / jpg / webp / gif。**非 GIF 图片会被转成 JPEG**（透明底会变成不透明），GIF 保留动画原样保存 —— 这是上传链路现有的行为。
- 图片会过图片审核，明确违规的拒绝。
- 没有排序接口，顺序就是添加顺序。
- 只做群聊；私聊调用返回 400。

错误：

| HTTP | code | 含义 |
| --- | --- | --- |
| 403 | `emoji_forbidden` | 不是群主 / 管理员 |
| 422 | `invalid_emoji_name` | 名称长度不对 |
| 409 | `emoji_name_taken` | 同群已有同名表情 |
| 409 | `emoji_limit_reached` | 已达 60 个上限 |
| 422 | `invalid_emoji_image` | `image_url` 不是已上传的图片 |
| 413 | `emoji_too_large` | 超过 512KB |
| 422 | `emoji_rejected` | 图片未通过审核 |

实时事件 `conversation.emojis.updated`：`{ "conversation_id": 5 }`，新增 / 改名 / 删除后发给群里的在线成员，收到后重拉列表。

使用：发一条现有的图片消息，`type = "image"`，`content` 填表情的 `url`，`metadata.sticker = true`（和收藏表情完全一样）。`metadata` 里可以再带 `group_emoji_id` 等来源字段，服务端原样保存和下发，不做校验。删除群表情不影响已经发出去的消息。

## 4. 戳一戳

戳一戳会在聊天记录里留下一行字（一条系统消息），群聊和私聊都能用。文案是：

```text
发起人 + 动作了动作 + 对方 + 对方设置的后缀        例：阿夸 拍了拍 薄荷糖 的小脑袋
```

动作和后缀都由**被戳的人**在自己的资料里设置；发起人可以临时换一个动作，后缀始终用对方的。

### 4.1 资料字段

`UserPublic`（所有返回用户对象的地方，包括别人的公开资料、成员列表）新增：

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `poke_action` | string | `"戳"` | 别人戳我时用的动作，预设里的一个字 |
| `poke_suffix` | string | `""` | 接在我名字后面的文案，最多 16 个字 |

两个字段永远是字符串，不会是 `null`。`PATCH /users/me` 可以改它们，传 `null` 或空串表示恢复默认。

- `poke_action` 只能从预设里选：`戳 拍 摸 亲 抱 捏 揉 贴 蹭 敲 挠 咬`。不开放自定义 —— 单个字没法做有意义的文本审核，而任意动词套进「X了X」很容易拼出不该出现的话。
- `poke_suffix`：首尾空格去掉，换行和连续空白折成一个空格，零宽字符删掉，之后最多 16 个字。保存时过文本审核，明确违规的拒绝；内容没变就不重复送审。
- 预设和长度上限由 `GET /users/poke-options` 下发：`{ "actions": ["戳", "拍", …], "default_action": "戳", "suffix_max_length": 16 }`。客户端用它渲染选择器，不要写死列表。
- 被平台禁言的账号仍然可以改这两个字段（和改昵称、签名一个口径）。

| HTTP | code | 含义 |
| --- | --- | --- |
| 422 | `invalid_poke_action` | 动作不在预设里 |
| 422 | `invalid_poke_suffix` | 后缀超过 16 个字 |
| 422 | `poke_suffix_rejected` | 后缀未通过文本审核 |

### 4.2 发起

`POST /conversations/{cid}/members/{uid}/poke`，请求体可选：

```jsonc
{ "action": "摸" }   // 不传、传 null 或空串：用对方资料里的 poke_action
```

返回这条消息的 `MessageRead`（以前返回 `{ "detail": "Poked" }`，旧客户端不读返回值，不受影响）。

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | — | 不能戳自己（`detail` 是字符串） |
| 404 | — | 会话不存在 / 你不是成员 / 对方不是成员 |
| 422 | `invalid_poke_action` | 动作不在预设里 |
| 403 | `account_muted` | 被平台禁言（处罚引擎的统一错误，带 `muted_until`） |
| 403 | `poke_muted` | 群里全员禁言或你被群禁言（群主、管理员不受限） |
| 429 | `poke_rate_limited` | 太频繁，`message` 说明是哪一种 |

频率限制（任何一条超了都是 429 `poke_rate_limited`）：

- 同一个人戳同一个人：10 秒一次。
- 同一个人在同一个会话：每分钟 6 次。
- 同一个会话里所有人加起来：每分钟 12 次。

### 4.3 落下来的消息

```jsonc
{
  "type": "system",
  "sender_id": 42,                              // 发起人
  "content": "阿夸 拍了拍 薄荷糖 的小脑袋",      // 拼好的完整文案，旧客户端直接显示这行
  "metadata": {
    "system_event": "conversation.member.poked",
    "actor_id": 42, "user_id": 77,
    "action": "拍",                             // 最终生效的动作
    "suffix": "的小脑袋",                       // 可能是空串
    "actor_name": "阿夸", "user_name": "薄荷糖"  // 当时的名字快照：群昵称 > 昵称 > 用户名
  }
}
```

- 用 `type = "system"` 而不是新类型：所有旧客户端本来就把系统消息渲染成居中的一行灰字，不会报错或留白。新客户端按 `metadata.system_event` 识别，用结构化字段自己拼（把自己换成「你」、名字可点击等）。
- 正常走 `message.created` 下发和历史分页，换设备、翻记录都在。
- 和其它系统消息同一个口径：**计入未读数、会成为会话列表的最后一条预览**；不计发言限频、不加群等级经验、不走机器审核（后缀在保存时已经审过）。要不要响提示音由客户端决定。
- 撤回沿用现有规则：发起人可以撤回，群主 / 管理员可以撤回别人的；每个人也可以对自己隐藏。
- **不能被举报，也不能选作证据消息**（对所有系统消息生效）：`POST /reports` 返回 422 `not_reportable`。这行字的后缀是被戳的人写的，算到发起人头上就罚错了人，被戳的人甚至可以先改好后缀再去举报对方。有人用戳一戳骚扰，或者后缀本身违规，都走「举报用户」，写明情况或附截图。客户端在系统消息上不要出举报入口，选证据时也别让选。
- 每个会话只保留最近 1000 条消息，戳一戳也占名额，所以上面的频率限制比较紧。

### 4.4 实时事件

除了 `message.created`，仍然会推一次 `conversation.member.poked`（发给会话的在线成员）：

```jsonc
{ "conversation_id": 5, "message_id": 901,
  "actor_id": 42, "actor_name": "阿夸", "user_id": 77, "user_name": "薄荷糖",
  "action": "拍", "suffix": "的小脑袋" }
```

新客户端：这个事件**只用来触发头像抖动**这类即时效果，聊天里的那行字以 `message.created` 为准，不要再用它弹临时提示（否则会显示两遍）。旧客户端不认识新字段，会照旧弹一下「A 戳了戳 B」，同时聊天里多出一行系统消息。

## 5. 公告回应

用户可以用 emoji 回应全站公告（后台发布的那种，不是群公告）。只统计数量，不展示是谁回应的。

### 5.1 公告对象

`GET /announcements`、`GET /announcements/latest` 返回的 `AnnouncementRead` 新增：

```jsonc
"reactions": [
  { "emoji": "👀", "count": 37, "reacted_by_me": true },
  { "emoji": "🔥", "count": 12, "reacted_by_me": false }
]
```

按数量从多到少排，数量相同的先出现的在前。没有回应时是空数组。旧客户端不认识这个字段，不受影响。

### 5.2 回应与取消

`POST /announcements/{announcement_id}/reactions`，body `{ "emoji": "👀" }` → `{ "id": 8, "reactions": [...] }`

- 同一个 emoji 再发一次就是取消（开关语义，和消息回应一致）。
- emoji 的校验规则和消息回应相同（只接受 emoji，不接受文字）。

`DELETE /announcements/{announcement_id}/reactions/{emoji}` → 同上的返回。没回应过也返回 200。

限制（数值在 `GET /announcements/reaction-options` 里下发）：

```jsonc
{ "quick": ["👍", "❤️", "😂", "😮", "😢", "🎉", "👀", "🔥"],   // 建议放在公告下面的快捷回应
  "per_user": 5,            // 每人每条公告最多用几种不同的 emoji
  "per_announcement": 30,   // 每条公告最多出现几种不同的 emoji
  "per_minute": 20 }        // 每人每分钟最多操作几次
```

错误码（`detail` 是 `{ code, message }`）：

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 404 | `announcement_not_found` | 公告不存在或已下线 |
| 403 | `account_muted` | 被平台禁言期间不能回应（取消自己的回应不受限） |
| 409 | `reaction_limit` | 自己已经用满 `per_user` 种，或这条公告已经有 `per_announcement` 种而你要加一种新的 |
| 422 | — | 不是合法的 emoji |
| 429 | `reaction_rate_limited` | 操作太快 |

实现上的细节：

- emoji 不做归一化：`❤️`（带变体选择符）和 `❤` 是两个不同的回应。取消时要用服务端在 `reactions[].emoji` 里返回的原串。
- 下线的公告既不能回应也不能取消（都是 404）。
- `DELETE` 路径里的 emoji 由浏览器做百分号编码，客户端证明头按**未编码**的路径计算。带 `#` 的 keycap emoji（如 #️⃣）不能走 `DELETE`（`#` 会被当成 URL 片段的开头，证明头对不上），取消它请用 `POST` 开关。
- 「先出现的在前」看的是现存回应里最早的那一条。
- 频率限制只计真正改动了回应的操作。

### 5.3 实时事件

`announcement.reaction.updated`：`{ "id": 8, "reactions": [{ "emoji": "👀", "count": 38 }] }`，发给所有在线用户。事件里没有 `reacted_by_me`——客户端保留本地已知的「我回应过哪些」，只用事件更新数量和顺序；自己的操作以接口返回为准。同一条公告的事件在服务端会合并：变化发生后等一秒，推一次最新的汇总（每个服务进程各自合并，所以偶尔会一秒收到两次）。

### 5.4 后台

`GET /admin/announcements` 的每一项同样带 `reactions`（`reacted_by_me` 恒为 false）。
