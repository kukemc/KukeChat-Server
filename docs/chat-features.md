# 投票、正在输入、群表情、戳一戳、公告回应、资料点赞、会话列表上的提及提示、乐观发送、图片直传

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
- **群的开关 `typing_indicator`**：`auto`（默认）只在 200 人以内的群转发；`on` 大群也转发；`off` 不转发。群主 / 管理员用群设置接口（`GroupSettingsUpdate`，和 `tasks_enabled` 同一个）改，`ConversationRead.typing_indicator` 返回当前值。私聊恒转发。
- 大群（超过 200 人）设成 `on` 时：只发给在线成员，同一个人的上报至少隔 4 秒（小群 2 秒），成员名单缓存 60 秒；改设置立刻生效。
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
- 每个群最多 60 个；单张最终不超过 1MB；png / jpg / webp / gif。**非 GIF 图片会被转成 JPEG**（透明底会变成不透明），GIF 保留动画原样保存 —— 这是上传链路现有的行为。
- 图片会过图片审核，明确违规的拒绝。
- 排序：`PUT /conversations/{cid}/emojis/order`，群主 / 管理员，body `{ emoji_ids: [...] }` 必须正好是当前全部表情的 id（多了、少了、重复都返回 409 `emoji_order_mismatch`，客户端刷新后重试），返回排好的 `GroupEmojiRead[]`，并推 `conversation.emojis.updated`。没排过序的和排序之后新加的排在后面，按添加顺序。
- 只做群聊；私聊调用返回 400。

错误：

| HTTP | code | 含义 |
| --- | --- | --- |
| 403 | `emoji_forbidden` | 不是群主 / 管理员 |
| 422 | `invalid_emoji_name` | 名称长度不对 |
| 409 | `emoji_name_taken` | 同群已有同名表情 |
| 409 | `emoji_limit_reached` | 已达 60 个上限 |
| 422 | `invalid_emoji_image` | `image_url` 不是已上传的图片 |
| 413 | `emoji_too_large` | 超过 1MB |
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
- 每个会话只保留最近 1000 条消息（私聊和免费群；付费群按档位 5000 / 20000 / 50000，见 [`group-plus.md`](group-plus.md)），戳一戳也占名额，所以上面的频率限制比较紧。
  被修剪掉的消息不会连带删掉精华和群待办：它们设置时就固化了消息快照。

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

### 5.5 公告长图上传

公告正文里的图走 `POST /uploads/announcement-image`（发布公告用的编辑器上传，和聊天图片分开）。更新日志、信息图这类长图是一整张 1000+ × 7000+ 的图，按普通图片的规矩缩到 2048 高会把字压糊，所以**只有这一条路径**放宽：

| 限制 | 普通上传（头像 / 聊天图 / 作品图 / 组队图 / 资料背景 / 表情包） | `announcement-image` |
| --- | --- | --- |
| 字节上限 | 3 MB | 20 MB（`IMAGE_ANNOUNCEMENT_MAX_BYTES`） |
| 最长边 | 2048（`IMAGE_MAX_DIMENSION`） | 8192（`IMAGE_ANNOUNCEMENT_MAX_DIMENSION`），等于不缩 |
| 解码像素上限 | 1200 万（`MAX_IMAGE_PIXELS`） | 4000 万（`IMAGE_ANNOUNCEMENT_MAX_PIXELS`） |
| 重编码质量 | `IMAGE_QUALITY`（82） | `IMAGE_ANNOUNCEMENT_QUALITY`（90） |

其它路径的限制一个字没动。实现上是按目标目录取一组限制（`app/services/storage.py` 的 `image_upload_limits()`），非 `announcements` 目录拿到的就是原来那组全局常量；老图迁移、表情包压缩等内部路径固定按默认限制走。

`thumbnail_url` 仍然是小图（`IMAGE_THUMBNAIL_MAX_DIMENSION`，360），后台列表用它当预览；正文里渲染的是 `url` 原图。因为存的是高清长图，客户端展示时不要给它套固定高度。

## 6. 资料点赞

给别人的个人空间 / 资料卡点赞，赞数累计，参照 QQ 的名片赞。

### 6.1 规则

- 每人每天（UTC+8 自然日）可以给同一个人点 1 个赞；第二天可以再点。赞数只增不减，点了不能取消。
- 不能给自己点赞；不能给机器人点赞。
- 每人每天最多给 `targets_per_day`（默认 50）个不同的人点赞。
- 被平台禁言 / 封禁的人不能点赞。

### 6.2 用户对象

`UserPublic` 新增 `profile_like_count`（整数，累计收到的赞）。旧客户端不认识，不受影响。

### 6.3 接口

`GET /users/{user_id}/likes` → 我和这个人之间的点赞状态：

```jsonc
{
  "user_id": 42,
  "like_count": 6024,        // 累计
  "today_count": 12,         // 今天收到的
  "liked_today": false,      // 我今天给他点过没有；看自己时恒为 false
  "can_like": true,          // 我现在能不能点（不是自己、不是机器人、今天没点过、没到每日上限、没被禁言）
  "reason": null,            // 不能点的原因：self / bot / muted / liked_today / limit；能点时为 null
  "left_today": 37           // 我今天还能给多少个不同的人点赞
}
```

`POST /users/{user_id}/likes` → 点一个赞，返回同上的对象（`liked_today: true`）。

| HTTP | `detail.code` | 含义 |
| --- | --- | --- |
| 404 | `user_not_found` | 用户不存在 |
| 400 | `cannot_like_self` / `cannot_like_bot` | |
| 409 | `already_liked_today` | 今天已经给他点过了 |
| 429 | `like_limit` | 今天点赞的人数到上限了。`detail.resets_at` 是明天 0 点（UTC+8） |
| 403 | `account_muted` | 被禁言期间不能点赞 |

`GET /users/me/likes/received?limit=20&offset=0` → 谁赞了我，按时间倒序：

```jsonc
{ "items": [ { "user": { /* UserPublic */ }, "created_at": "…", "today": true } ],
  "total": 6024, "today_count": 12 }
```

同一个人每天一条。`limit` 1–50。

`GET /users/likes/options` → `{ "targets_per_day": 50 }`。

### 6.4 实时事件

`user.profile.liked`：`{ "from_user": { /* UserPublic */ }, "like_count": 6025, "today_count": 13 }`，发给被赞的人本人。不写通知中心（太频繁）；同一个被赞者 3 秒内的多次点赞合并成一次事件，`from_user` 是最后一个。

## 7. 会话列表上的「[有人@我]」/「[@全体成员]」

会话对象（`GET /conversations` 的每一项，以及所有返回单个会话的接口）新增：

```jsonc
"mention_pending": "me"     // "me" | "all" | null
```

- 看的是我在这个会话里的**未读**消息：有点名 @我 的 → `"me"`；没有 @我 但有 @全体成员 → `"all"`；都没有 → `null`。两种都有时是 `"me"`。
- 自己发的不算（点名自己、自己发的 @全体）。
- 「未读」和未读数是同一个口径：已读位置（`last_read_message_id`）之后的消息。已读位置越过那条消息就自动变回 `null`；清空聊天记录等于都读过了。
- 哪些算提及：
  - 普通发消息：`metadata.mentions`（`[{ user_id, name }]`）点名了我；`metadata.mention_all`（或者 `mentions` 里有 `all`）是 @全体，仍然只有群主 / 管理员能发、每天有次数上限（原有规则）。
  - 机器人接口：`at` / `mention` 元素（或 markdown 里的 `<at id="…"/>`）算点名，`at_all` / `mention_all` 算 @全体——它们被解析成同样的 `metadata`，走的是同一条路径。
  - 不算的：引用回复、合并转发里夹着的提及、系统消息。
- 一条消息最多给 50 个人记「有人@我」。
- 旧客户端不认识这个字段，不受影响。

实时：没有新事件。

- 置上：`message.created` 里带着消息的 `metadata`，客户端看到点名了自己 / @全体（且不是自己发的、会话不在前台）就在本地置上。
- 清掉：标记已读之后清。`POST /conversations/{id}/read` 的返回（`ReadReceipt`）新增 `mention_pending`，就是读到这里之后这个会话的值（一般是 `null`；只读到一半时可能还是 `"me"` / `"all"`）。同一个人的所有设备会收到 `conversation.read`，其中也带 `mention_pending`（`null` 表示清掉）。以前另外推一次 `conversation.updated`，2026-11 起不再推：安卓 2.0.10 收到 `conversation.*` 会整份重拉会话列表，PC 上每读一次，手机就重拉一次。
- 撤回：被撤回的消息不再触发提示（用户撤回、群管理撤回、后台按消息撤回、机器审核、案件结案都一样）。没有专门的事件，客户端收到 `message.recalled` 时如果那条正是本地记着的提及，就重拉这个会话（或者整个列表）。

实现上的细节：

- 不扫消息。发消息时把「最近一条点名 @某人的消息 id」记在那个人的成员行上（`conversation_members.last_mention_message_id`，只更新被点名的那几行，一条语句），把「最近一条 @全体 的消息 id」记在会话上（`conversations.last_mention_all_message_id`，不逐个成员写）。读会话时拿这两个数和我的已读位置比大小，都在本来就要加载的行上，**不多任何查询**。
- 所以它只记得「最近一条」：撤回时如果被撤回的正是记着的那一条，就把记号清掉，而不是退回到更早的一条——更早的那条提及如果也还没读，提示会跟着消失。案件改判把消息恢复之后，提示不会恢复。
- 已读位置是客户端报上来的，可以往回退；退回去之后那条提及又成了未读，提示会跟着回来。
- 刚进群、还没读过任何消息的人，进群之前的 @全体 也算未读（和未读数一致）。
- 这两列是加在已有的表上的：启动时自动 `ALTER TABLE … ADD COLUMN`（可空整数，不回填、不建索引）。两个进程同时启动、都去加同一列时，后到的那个会把「列已存在」当成成功；稳妥起见上线前仍然先单进程启动一次。

### 7.1 未读提及列表（跳到 @我 的消息）

`GET /conversations/{cid}/mentions/unread` → 200

```jsonc
{
  "items": [{ "message_id": 123, "kind": "me", "created_at": "2026-10-19T08:00:00Z" }],  // kind: "me" | "all"
  "total": 1
}
```

- 已读位置（`last_read_message_id` / 清空位置，取大的）之后提到我的消息，**旧的在前**，`items` 最多 50 条；`total` 是一共几条（最多数到 1000）。
- 口径和 `mention_pending` 一样：点名我 → `"me"`；@全体（只算群聊）→ `"all"`；两者都有 → `"me"`。自己发的、撤回的、对自己隐藏的不算。
- 不是成员 404。客户端跳到某条后照常 `POST /conversations/{id}/read`，再拉一次就是剩下的。
- 实现：先拿上面那两个记号挡 —— 都不比已读位置新就直接返回空，**不查消息表**（绝大多数情况）。否则只扫 `(已读位置, 两个记号里大的]` 这一段（走 `(conversation_id, id)` 索引），而且只取 `metadata` 里带 `mention` 字样的行，在内存里按 `metadata.mentions` / `mention_all` 认。
- 同样只是尽力而为：最近一条提及被撤回时记号被清掉，更早那条未读的提及也就不列了（和角标一致）。

## 8. 生日

用户可以填自己的生日。公开资料上只显示月日，年份要本人选择公开才给；生日当天别人能看到「今天生日」。「今天」一律按 UTC+8。

### 8.1 用户对象

`UserPublic` 新增两个字段（旧客户端不认识，忽略即可）：

```jsonc
"birthday": { "month": 10, "day": 12, "year": null },   // 没设置为 null；year 只有本人选择公开年份时才给
"is_birthday_today": true                                // 今天（UTC+8）是不是这个人的生日
```

- 2 月 29 日出生的人在平年按 2 月 28 日过（闰年只有 29 日那天算）。
- 这两个字段出现在所有带用户对象的地方（消息的发送者、成员列表、搜索、好友……），都只读用户这一行上的列，不多查任何表。服务端按当前时间现算，所以零点一过就变；没有专门的实时事件，客户端按拿到用户对象时的值显示即可（会话列表、在线名单这些有几秒缓存的接口会晚几秒）。
- 生日当天收到的礼物，记录上 `on_birthday = true`（[`gifts.md`](gifts.md)）。

### 8.2 接口

都只动自己的，路径在 `/users/me/birthday` 下。

| 接口 | 说明 |
| --- | --- |
| `GET /users/me/birthday` | `{ birthday: { year, month, day } \| null, show_year, next_change_at, celebrate_today }`。本人看自己总是带年份 |
| `PUT /users/me/birthday` | body `{ year, month, day, show_year }`。返回和 GET **同样的形状** |
| `DELETE /users/me/birthday` | 清空。返回和 GET 同样的形状（`birthday` 为 null） |
| `POST /users/me/birthday/celebrated` | 标记今年的庆生动画已经播过。返回和 GET 同样的形状（`celebrate_today` 变成 false）。今天不是生日时什么都不改 |

- `celebrate_today`：今天是我的生日，而且今年（UTC+8 的年份）还没标记过「播过了」。到了明年生日又是 true。
- `next_change_at`：**已废弃，恒为 null**。以前用来表示「下次允许改日期的时间」；改日期的限制取消后只为兼容旧客户端保留，新代码不要读它。
- **改日期不受限制**：随时可以设置、修改、清空，次数不限，不会再返回 409 `birthday_locked`（以前的「365 天只能改一次」已取消）。`show_year` 不传就保持原样。
- 庆生按年记：改日期、清空都不会重置「今年已经播过」。所以把生日改成今天，今年还没播过就会 `celebrate_today = true`；今年已经播过（哪怕是按改之前的日期播的），反复改日期也不会再播。
- 日期不合法（不存在的日子、2 月 29 日配了平年）、在未来（按 UTC+8 的今天算）、早于 1900 年、或者不是整数：422 `invalid_birthday`。

### 8.3 后台

`GET /admin/users` / `GET /admin/users/{id}` 的用户对象里，`birthday` **总是带完整生日（含年份）**，不管本人选没选公开；另有 `is_birthday_today` 和 `charm`。后台不提供修改生日的入口，改生日也不写审计日志（是用户自己的操作）。

### 8.4 实现上的细节

- 生日存在 `users` 表自己身上：`birthday_year` / `birthday_month` / `birthday_day` / `birthday_show_year`，加上 `birthday_celebrated_year`（哪一年的庆生动画播过了）。`birthday_set_at` / `birthday_changed_at` 是以前「365 天只能改一次」用的两列，限制取消后不再读写，列保留在表里（不删列、不改迁移）。启动时自动 `ALTER TABLE … ADD COLUMN`（都是可空列或带默认值，不回填）。
- 两个进程同时启动、都去加同一列时，后到的那个会把「列已经有了」当成正常情况；稳妥起见上线前仍然先单进程启动一次。

## 9. 群升级、群文件、永久精华、群待办、聊天记录搜索

见 [`group-plus.md`](group-plus.md)。和本文件有关的变化：

- 精华不再随消息被修剪而消失（设置时固化快照），数量有上限（按群档位）；`GET .../featured-messages` 仍返回数组，分页信息在响应头。
- 群待办的提醒走第 7 节的 @全体成员 链路，但不占 @全体成员 的每日次数。
- 文件消息（`metadata.file`）和群待办提醒（`metadata.group_todo`）都是普通文本消息，只能由各自的接口创建。


## 10. 好友申请「忽略」、通知只提醒一次

### 10.1 忽略好友申请

| 接口 | 说明 |
| --- | --- |
| `POST /friends/requests/{id}/ignore` | 204。只有接收方能调（申请方 403，无关的人 404），申请必须还是 pending（否则 409）。重复忽略无害 |
| `POST /friends/requests/ignore-all` | `{ "ignored": <int> }`：把自己收到的、还没忽略的 pending 申请全部忽略，返回这次忽略了几条 |

- 忽略只在接收方这边生效：`friend_requests.ignored_at` 有值，**状态仍是 `pending`**，`updated_at` 不动。申请方看到的一切照旧（仍显示「等待验证」），不给他推任何事件。
- 被忽略的申请不出现在接收方的 `GET /friends/requests/incoming` 和 `GET /notifications` 里。`FriendRequestRead` 不带 `ignored_at`。
- 申请方再发一次：已有 pending 申请时照旧 409，那条申请继续保持忽略，不会重新冒出来。接收方自己反过来加对方，照旧直接成为好友。
- 实时事件只推给接收方自己（其他设备刷新列表 / 角标）：`friend.request.ignored`，`{ id, ids }`。单条忽略 `id` 是那条申请、`ids` 是 `[id]`；全部忽略 `id` 为 null、`ids` 是这次忽略的全部（一条都没有就不推）。

### 10.2 通知只提醒一次

`users.notifications_seen_at` 记「上次打开通知中心」的时间。

| 接口 | 说明 |
| --- | --- |
| `POST /notifications/seen` | `{ "seen_at": "<iso>" }`，seen_at = 服务器当前时间（UTC）。同时给本人所有连接推 `notification.seen`（同样的 body），其他设备据此清角标 |
| `GET /notifications` | 新增 `seen_at`（从没打开过为 null）和 `new_count` |

- 除动态（`category = "post"`，仍按各自的已读标记）以外，每条的 `unread = time > seen_at`（seen_at 为 null 时全部算新）。`pending` 和这个无关：看过但没处理的申请仍是 `pending = true`，只是不再算新。
- 例外：自己刚处理掉的申请（收到的好友申请已通过 / 已拒绝，自己审核的入群申请）虽然 `updated_at` 变新了，也不算新通知。
- `new_count` = `unread` 为 true 的条数，客户端角标用它。`unread_count` 与 `new_count` 相同，留着兼容旧客户端；`pending_count`、`counts` 不变。
- 两列都是启动时 `ALTER TABLE … ADD COLUMN` 补的可空列，不回填。

## 11. 乐观发送（`client_nonce`）

客户端点发送后先在本地显示一条「发送中」的消息，等服务器确认后原地换成真消息。为了把两者对上，发消息时可以带一个本地标记：

- `POST /conversations/{cid}/messages` 的请求体加可选字段 `client_nonce`：1–64 位，只允许 `[A-Za-z0-9_-]`，不合规返回 422。
- 原样回显：这次 POST 的返回 `MessageRead.client_nonce`，以及这条消息的 `message.created` 事件里的 `client_nonce`（发给全会话的同一份数据，不含敏感信息）。
- 不落库：历史分页、单条消息、别的事件里恒为 `null`。不写进 `metadata`，也不受 `metadata` 的任何规则影响。
- 客户端：POST 返回和 `message.created` 谁先到都行，按 `client_nonce` 找到本地那条替换，另一份按 `id` 去重。

## 12. 语音转文字

`POST /conversations/{cid}/messages/{mid}/transcribe`（无请求体）→ 200 `{ "text": "今天下午三点开会", "cached": false }`

- 能看到这条消息的会话成员都能转（没清空到它之前、没对自己隐藏）；只认 `type = "voice"` 的消息，语音红包领取时发出的那条语音也是。
- 第一次交给识别服务（`VOICE_ASR_URL` 的 `/asr/transcribe`，和语音红包同一个 SenseVoice 模型）识别，结果存进 `voice_transcripts`（每条消息一行），之后谁来点都直接读库，`cached = true`。
- `text` 可能是 `""`：录音里没识别出话。空串也存，不会每点一次识别一次；客户端显示「未识别到文字」。数字是汉字（「三点」不是「3点」），和口令识别同一个设置。
- 消息对象 `MessageRead` 新增 `voice_transcript`：这条语音已经转过就是那段文字（可能是 `""`），没转过、不是语音、撤回了是 `null`。历史分页 / 上下文 / 搜索等返回消息列表的接口都带（一页一条查询）；`message.created` 里恒为 `null`。转完不推实时事件，别人的设备下次拉到消息时才看得到。

| 状态 | `detail.code` | 什么时候 |
| --- | --- | --- |
| 404 | `message_not_found` | 消息不存在、不在这个会话、我看不到（不是成员、清空了、隐藏了）、已撤回 |
| 422 | `not_voice` | 不是语音消息 |
| 422 | `voice_too_long` | 超过 60 秒（`metadata.voice.duration_ms` 有就先按它挡，没有由识别服务判） |
| 429 | `transcribe_rate_limited` | 每人每小时最多真正识别 60 次；命中缓存的、识别失败的不算 |
| 503 | `transcribe_unavailable` | 没配识别服务、连不上、出错；录音文件取不到；不是我们上传的录音（机器人发的外链音频） |

错误体和红包接口一样是 `{ "detail": { "code", "message" } }`。

- 次数按 `voice_transcripts.requested_by_id`（第一个点、真正花了一次识别的人）+ `created_at` 数，几个进程共用一个口径。
- 等对象存储和识别服务之前把数据库连接还回去（`release_connection`）。两个人同时点同一条：后存的撞唯一约束，直接用先存的那份。
- 新表 `voice_transcripts`（`message_id` 唯一、外键 CASCADE —— 消息被修剪时跟着删；索引 `(requested_by_id, created_at)`）由启动时的 `create_all` 建，不用手动迁移。

## 13. 群禁言不随退群失效

退群、被移出时还没到期的群禁言存进 `group_mute_carryovers`，重新进群（任何路径：链接、邀请、审批、官方群……）时恢复，到期时间不变；永久禁言恢复后还是永久。不在群里的这段时间到期的就不再恢复。重新进群后群主、管理员照常可以解除。接口和字段没有变化，客户端不用改。

## 14. 群邀请链接、入群申请（2026-11-07）

`POST /invites/group/{id}` 返回的 token 是 `<群的公共 token>-<邀请人 id>-<签名>`（公共 token 里没有 `-`；2.0.10 客户端只认 `[A-Za-z0-9_-]`，所以不用点号。解析时点号分隔也认）。每个群在 `invite_links` 里仍只有一行；邀请人身份放在签名后缀里（HMAC，密钥是 `JWT_SECRET_KEY`），改了后缀就是 404。没有后缀的老链接，邀请人按这一行的 `created_by_id` 算。

点「加入」会怎样，看邀请人**此刻**在群里的身份（`GET /invites/{token}` 返回 `join_action`）：

| 邀请人 | 群的入群方式 | `join_action` | `POST /invites/{token}/accept` |
| --- | --- | --- | --- |
| 群主 / 管理员 | 任意 | `join` | 直接进群，`status: "joined"` |
| 普通成员 | `open`，或开了 `auto_approve` | `join` | 直接进群 |
| 普通成员 | `approval` | `request` | 提交入群申请，`status: "pending"`、`join_request_id`；通知和自己申请一样 |
| 普通成员 | `question` | `answer` | 请求体要带 `{"answer": "..."}`（可选 `message`），不带返回 422；之后同上 |
| 普通成员 | `invite_only` | `forbidden` | 403；普通成员在仅邀请的群里也生成不了链接（403） |

以前任何成员拿到的都是公共链接，点开直接进群，入群方式全被绕过。老客户端接受邀请不带请求体：要审核的群照样能提交申请，要答题的群会收到 422。

`GET /conversations/join-requests/incoming` 待审核的排在最前（以前写反了，待审核的沉在最底下）。拒绝时可以带 `{"decision_note": "..."}`（最多 200 字，可以不填），申请人在 `group.join_request.rejected` 和自己的申请列表里看得到。

## 15. 图片直传（2026-11-16）

生产机到对象存储的带宽很小、时快时慢。`POST /uploads/message-image` 要后端收下整张图、压缩、再把主图和缩略图传进桶，1–2 MB 的图常常超时，客户端显示「发送失败」。直传时客户端自己压缩、生成缩略图，用预签名 URL 直接传到桶里，后端只做校验。只对聊天图片（`messages/` 目录，`/uploads/message-image` 的用法：消息图片、群表情、公告配图）开放；头像、动态图等仍走 multipart。

开关：`STORAGE_BACKEND=s3` 且 `DIRECT_IMAGE_UPLOADS` 没关（默认开）。本地存储时开单返回 409。

**1. 开单** `POST /uploads/message-image/direct`

```jsonc
{ "content_type": "image/jpeg",   // 静态图一律 image/jpeg；动图（GIF，含转成 GIF 的）image/gif。别的 422 unsupported_image_type
  "size": 523144,                 // 主图字节数，不能超过普通上传的上限（3 MB），超了 413 image_too_large
  "thumbnail_size": 18311 }       // 缩略图字节数，JPEG，最多 256 KB，超了 413 thumbnail_too_large
```

201：

```jsonc
{ "upload_id": "…",
  "image":     { "method": "PUT", "url": "https://…", "headers": { "Content-Type": "image/jpeg", "Cache-Control": "public, max-age=3600" } },
  "thumbnail": { "method": "PUT", "url": "https://…", "headers": { "Content-Type": "image/jpeg", "Cache-Control": "public, max-age=3600" } },
  "expires_at": "…" }             // 两条 URL 的过期时间（15 分钟）
```

**2. 上传**：两条都 `PUT url`，请求体就是文件本身，`headers` 必须原样带上，不能多也不能改（签名里固定了 Content-Type、Cache-Control 和 Content-Length）。

**3. 完成** `POST /uploads/message-image/direct/{upload_id}/complete`（无请求体，只有开单的人能调）。201，返回和 `/uploads/message-image` 一样的 `{ url, thumbnail_url, filename, content_type }`，之后照旧把 `url` / `thumbnail_url` 放进消息。

后端 head 两个对象核对大小，主图只读开头 256 KB、缩略图整张读，检查：

- 主图：格式和开单时的 `content_type` 一致；JPEG 的最长边不超过 `IMAGE_MAX_DIMENSION`（2048）、是 RGB / 灰度、除了 JFIF / ICC / Adobe 段外**没有任何元数据段**（EXIF、XMP、IPTC、注释……）。客户端用 canvas 重新编码出来的 JPEG 天然满足。
- 缩略图：JPEG、最长边不超过 `IMAGE_THUMBNAIL_MAX_DIMENSION`（360）、能完整解码、同样没有元数据段。

都合格就原样使用，后端不上传任何东西。不合格的（带 EXIF 定位的原图、尺寸超了、缩略图不对……）后端把主图整张拉回来，按 multipart 上传那条路重新处理、写回同一个 key，结果和 multipart 上传一样，只是慢一些；只是缩略图不对时只重做缩略图。

| 状态 | code | 说明 |
| --- | --- | --- |
| 409 | `direct_upload_unavailable` | 开单：服务器没开直传，改用 `/uploads/message-image` |
| 422 | `unsupported_image_type` | 开单：`content_type` 不是 `image/jpeg` / `image/gif` |
| 413 | `image_too_large` / `thumbnail_too_large` | 开单：大小超限，带 `max_bytes` |
| 400 | `empty_file` | 开单：大小是 0 |
| 429 | `too_many_pending_uploads` | 开单：同时开着、还没完成的上传单超过 30 个 |
| 409 | `upload_incomplete` | 完成：有对象还没传上去。上传单保留，传完可以再调 |
| 422 | `upload_size_mismatch` | 完成：大小和开单时不一致。对象删掉、上传单作废 |
| 400 | （`detail` 是字符串） | 完成：不是能用的图片。对象删掉、上传单作废 |
| 404 | `upload_not_found` | 完成：上传单不存在、已经用过，或不是自己开的 |
| 410 | `upload_expired` | 完成：开单超过 1 小时 |

- 客户端的兜底：开单 409（服务器没开直传）改走下面的中转；PUT 失败（多半是网页的 CSP 或跨域拦下了对象存储的地址）也改走中转，这次运行里之后都走中转；中转接口不存在（老后端，404 / 405）或别的原因失败，退回 `/uploads/message-image`。
- 开单 1 小时内没完成的上传单，后台 worker 每 10 分钟清理一次，连同桶里可能已经传上去的对象。
- 对象直接传到最终的 key（主桶公开读）：完成之前它们已经能按地址读到，但 key 是随机的，没完成的会被清理。
- 新表 `image_uploads`（索引 `expires_at`、`user_id`）由启动时的 `create_all` 建，不用手动迁移。
- 主桶要允许浏览器跨域 PUT：雨云默认对所有来源放行；别的服务商要给主桶配和私有桶一样的 CORS（`docs/group-plus.md` 5.4）。

**中转**（2026-11-20）`POST /uploads/message-image/relay`，multipart：`image`（主图，`image/jpeg` 或 `image/gif`）、`thumbnail`（JPEG）。

有的网页用 CSP 限制了能连的域名（共创世界只放行我们自己的域名），嵌在里面的 KukeChat 连不上对象存储，预签名 PUT 会被浏览器直接拦下。
这时客户端照样在本地压缩好主图和缩略图，一个请求交给后端。后端在内存里按上面同一套规则检查主图和缩略图，另外确认主图能完整解码
（截断、损坏的不收）：

- 合格就原样写进桶：不重新编码、不回读对象存储，不开上传单。服务器到对象存储只走压缩好的那点字节。
- 不合格的按 `/uploads/message-image` 处理，结果一样；不是图片 → 400，太大 → 413。

返回和 `/uploads/message-image` 一样的 `{ url, thumbnail_url, filename, content_type }`。

客户端在 `*.ccw.site` 上一开始就走中转，并用更省流量的压缩档：最长边 1600 / 1280 / 1080、质量 0.78 / 0.66 / 0.56，
取第一个不超过 900 KB 的结果（大多数照片 200–500 KB）；压不进去再退回直传的默认档（最长边 2048、上限 3 MB）。
